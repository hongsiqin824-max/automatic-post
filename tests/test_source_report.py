from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import repository as repo
from app.config import _parse_boundaries
from app.db import get_db
from app.services.source_monitor import (
    compare_periods,
    detect_anomalies,
    generate_summary,
    scale_threshold,
)
from app.services.source_report import (
    SourceReportResultUnknown,
    baseline_period_end,
    build_report_text,
    period_hours,
    report_period,
    send_due_source_report,
)

BOUNDARIES = ((11, 0), (19, 0))


def test_parse_boundaries_sorts_and_drops_malformed_entries():
    assert _parse_boundaries("19:00,11:00") == ((11, 0), (19, 0))
    assert _parse_boundaries("11") == ((11, 0),)
    # 环境变量写错不能让整个应用起不来，坏的那项丢掉就行。
    assert _parse_boundaries("11:00,abc,25:00,19:61") == ((11, 0),)


def test_report_period_tiles_the_day_without_gaps():
    """两期必须首尾相接：有缝隙会漏稿，重叠会把同一篇算两次。"""

    # 北京 12:00（11:00 刚过）→ 上一期是前一天 19:00 到今天 11:00。
    morning = report_period(datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc), BOUNDARIES)
    assert morning == ("2026-09-17T11:00:00.000Z", "2026-09-18T03:00:00.000Z")
    # 北京 20:00（19:00 刚过）→ 上一期是今天 11:00 到 19:00。
    evening = report_period(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), BOUNDARIES)
    assert evening == ("2026-09-18T03:00:00.000Z", "2026-09-18T11:00:00.000Z")
    assert morning[1] == evening[0]
    assert period_hours(*morning) == 16
    assert period_hours(*evening) == 8


def test_report_period_waits_until_a_boundary_has_passed():
    """北京 09:00 时今天还没有边界过去，上一期是昨天 11:00→19:00。"""

    period = report_period(datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc), BOUNDARIES)
    assert period == ("2026-09-17T03:00:00.000Z", "2026-09-17T11:00:00.000Z")
    assert report_period(datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc), ()) is None


def test_baseline_period_end_is_the_same_boundary_yesterday():
    """基准必须是昨天的同一个边界，拿昨天全天比会让 8 小时那期全员报骤降。"""

    assert baseline_period_end("2026-09-18T11:00:00.000Z") == "2026-09-17T11:00:00.000Z"


def test_scale_threshold_shrinks_with_the_period():
    assert scale_threshold(10, 24) == 10
    assert scale_threshold(10, 16) == 7
    assert scale_threshold(10, 8) == 3
    # 阈值不能降到 0，否则每个没出稿的小来源都会报警。
    assert scale_threshold(5, 1) == 1


def _stats(code: str, total: int, *, published: int = 0, draft: int = 0,
           review: int = 0, name: str | None = None) -> dict:
    return {
        "source_code": code, "source_name": name if name is not None else code,
        "total_count": total,
        "published_count": published, "draft_count": draft, "review_count": review,
        "abandoned_count": 0, "duplicate_count": 0,
    }


def test_report_text_identifies_sources_that_share_a_display_name():
    """库里三个来源都叫「韩媒」，只报显示名就不知道该去修哪一个。"""

    anomalies = detect_anomalies(
        compare_periods(
            [_stats("naversp", 0, name="韩媒")], [_stats("naversp", 30, name="韩媒")]
        ),
        period_hours=8,
    )
    text = build_report_text(
        "2026-09-18T03:00:00.000Z", "2026-09-18T11:00:00.000Z",
        generate_summary([]), anomalies,
    )
    assert "韩媒(naversp)" in text


def test_missing_baseline_does_not_become_thirty_new_source_alerts():
    """首期没有昨日同期快照时，全员标「新增」会让第一条消息变成 30 条假预警。"""

    comparison = compare_periods(
        [_stats("a", 10), _stats("b", 3)], []
    )
    assert {item["change"]["type"] for item in comparison} == {"no_baseline"}

    anomalies = detect_anomalies(comparison, period_hours=8)
    assert not anomalies["info"]
    assert not anomalies["critical"]
    assert len(anomalies["normal"]) == 2

    text = build_report_text(
        "2026-09-18T03:00:00.000Z", "2026-09-18T11:00:00.000Z",
        generate_summary(comparison), anomalies,
    )
    assert "无昨日同期数据" in text
    assert "新增来源" not in text


def test_a_genuinely_new_source_is_still_reported():
    """基准里有数据、只有这个来源是新出现的，才叫新增来源。"""

    comparison = compare_periods(
        [_stats("old", 10), _stats("fresh", 4)], [_stats("old", 9)]
    )
    anomalies = detect_anomalies(comparison, period_hours=8)
    assert [item["source_code"] for item in anomalies["info"]] == ["fresh"]


def test_detect_anomalies_flags_a_stopped_source_against_the_same_period():
    comparison = compare_periods([_stats("naversp", 0)], [_stats("naversp", 30)])
    anomalies = detect_anomalies(comparison, period_hours=8)
    assert len(anomalies["critical"]) == 1
    assert anomalies["critical"][0]["anomaly_message"] == "🚨 本期0篇，昨日同期30篇，疑似故障"


def test_detect_anomalies_stays_quiet_when_a_short_period_is_simply_small():
    """8 小时期里 3 篇对 4 篇是正常波动，不该占用预警位。"""

    comparison = compare_periods([_stats("skj", 3)], [_stats("skj", 4)])
    assert detect_anomalies(comparison, period_hours=8)["normal"]


def test_generate_summary_counts_drafts_and_reviews_as_pending():
    """项目里没有 DRAFT/REJECTED 状态，早先按它们统计导致这行常年是 0。"""

    comparison = compare_periods(
        [_stats("a", 10, published=4, draft=3, review=2)], [_stats("a", 8)]
    )
    summary = generate_summary(comparison)
    assert summary["total_articles"] == 10
    assert summary["total_draft"] == 5
    assert summary["total_change"] == "+25% vs 昨日同期"


def test_build_report_text_derives_every_time_word_from_the_period():
    """一天多期，写死「今日/昨日」会有一半时候是错的，两条消息也会长得一样。"""

    summary = generate_summary(
        compare_periods([_stats("a", 10, published=4, draft=1)], [_stats("a", 8)])
    )
    anomalies = detect_anomalies(
        compare_periods([_stats("a", 0)], [_stats("a", 30)]), period_hours=8
    )
    text = build_report_text(
        "2026-09-18T03:00:00.000Z", "2026-09-18T11:00:00.000Z", summary, anomalies
    )
    assert "📊 来源抓取预警 · 09-18 19:00" in text
    assert "统计区间：09-18 11:00 → 09-18 19:00（北京时间，8小时）" in text
    assert "【本期概况】" in text
    assert "本期：0篇 | 昨日同期：30篇" in text
    assert "今日" not in text
    assert "昨日：" not in text


def _seed_source(conn, code: str, created_at: list[str] = ()) -> None:
    # 启用的来源必须绑定至少一个启用栏目，所以借用种子数据里的「日职联」。
    tab = next(tab for tab in repo.list_tabs(conn) if tab["name"] == "日职联")
    if repo.get_source(code, conn) is None:
        repo.create_source(code, code, tab_ids=[tab["id"]], enabled=True, connection=conn)
    else:
        repo.update_source(code, conn, tab_id=tab["id"], enabled=True)
    for index, moment in enumerate(created_at):
        with conn:
            conn.execute(
                "INSERT INTO articles (source, source_url, title_final, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (code, f"https://e.com/{code}/{index}", f"t{index}",
                 "PUBLISHED", moment, moment),
            )


def test_source_period_stats_uses_utc_bounds_not_a_local_date(app):
    """按北京日期匹配 UTC 时间戳会把凌晨那批稿子算进前一天，实测漏掉两百多篇。"""

    with app.app_context():
        conn = get_db()
        _seed_source(conn, "alert-src", [
            "2026-09-17T11:30:00.000Z",  # 北京 9/17 19:30 — 属于 19:00→11:00 那期
            "2026-09-17T20:00:00.000Z",  # 北京 9/18 04:00 — 旧口径会算成 9/17
            "2026-09-18T03:30:00.000Z",  # 北京 9/18 11:30 — 属于下一期
        ])
        rows = repo.source_period_stats(
            "2026-09-17T11:00:00.000Z", "2026-09-18T03:00:00.000Z", conn
        )

    counts = {row["source_code"]: row["total_count"] for row in rows}
    assert counts["alert-src"] == 2


def test_source_period_stats_keeps_sources_with_no_articles(app):
    """抓取挂掉的来源是最该预警的，那一行不能因为 0 篇而整行消失。"""

    with app.app_context():
        conn = get_db()
        _seed_source(conn, "dead-src")
        rows = repo.source_period_stats(
            "2026-09-17T11:00:00.000Z", "2026-09-18T03:00:00.000Z", conn
        )

    assert any(row["source_code"] == "dead-src" and row["total_count"] == 0 for row in rows)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _configure(app, monkeypatch, calls: list):
    config = app.extensions["config"] if "config" in app.extensions else None
    monkeypatch.setattr(
        "app.services.source_report.requests.post",
        lambda url, **kwargs: (calls.append((url, kwargs)), _Response({"code": 0}))[1],
    )
    return config


def test_send_due_source_report_is_idempotent_per_period(app, monkeypatch):
    """同一期只发一次，30 秒一轮的轮询不能把群刷爆。"""

    from app.config import AppConfig

    calls: list = []
    _configure(app, monkeypatch, calls)
    config = AppConfig()
    object.__setattr__(config, "database_path", app.config["DATABASE"])
    object.__setattr__(config, "source_report_enabled", True)
    object.__setattr__(config, "source_report_webhook_url", "https://example.com/hook")
    object.__setattr__(config, "source_report_boundaries", BOUNDARIES)

    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    with app.app_context():
        conn = get_db()
        _seed_source(conn, "alert-src", ["2026-09-18T04:00:00.000Z"])
        first = send_due_source_report(config, conn, now=now)
        second = send_due_source_report(config, conn, now=now)

    assert first["sent"] is True
    assert second == {
        "sent": False,
        "skipped": True,
        "reason": "already_sent",
        "period": ("2026-09-18T03:00:00.000Z", "2026-09-18T11:00:00.000Z"),
    }
    assert len(calls) == 1


def test_send_due_source_report_stops_recomputing_once_the_period_is_sent(app, monkeypatch):
    """已发出的期不再碰写入路径：30 秒一轮的重复聚合与快照覆盖会和抓取抢写锁。"""

    from app.config import AppConfig

    calls: list = []
    _configure(app, monkeypatch, calls)
    config = AppConfig()
    object.__setattr__(config, "database_path", app.config["DATABASE"])
    object.__setattr__(config, "source_report_enabled", True)
    object.__setattr__(config, "source_report_webhook_url", "https://example.com/hook")
    object.__setattr__(config, "source_report_boundaries", BOUNDARIES)

    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    with app.app_context():
        conn = get_db()
        _seed_source(conn, "alert-src", ["2026-09-18T04:00:00.000Z"])
        assert send_due_source_report(config, conn, now=now)["sent"] is True

        touched: list[str] = []
        monkeypatch.setattr(
            repo, "source_period_stats", lambda *a, **k: touched.append("stats") or []
        )
        monkeypatch.setattr(
            repo, "save_source_period_snapshot", lambda *a, **k: touched.append("write") or 0
        )
        monkeypatch.setattr(
            repo, "claim_source_report_delivery", lambda *a, **k: touched.append("claim")
        )
        second = send_due_source_report(config, conn, now=now)

    assert second["reason"] == "already_sent"
    assert touched == []


def test_send_due_source_report_saves_the_snapshot_even_when_sending_fails(app, monkeypatch):
    """环比基准读的是快照；发送失败不该让明天丢掉基准。"""

    from app.config import AppConfig

    monkeypatch.setattr(
        "app.services.source_report.requests.post",
        lambda url, **kwargs: _Response({"code": 99, "msg": "boom"}),
    )
    config = AppConfig()
    object.__setattr__(config, "database_path", app.config["DATABASE"])
    object.__setattr__(config, "source_report_enabled", True)
    object.__setattr__(config, "source_report_webhook_url", "https://example.com/hook")
    object.__setattr__(config, "source_report_boundaries", BOUNDARIES)

    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    with app.app_context():
        conn = get_db()
        _seed_source(conn, "alert-src", ["2026-09-18T04:00:00.000Z"])
        result = send_due_source_report(config, conn, now=now)
        snapshot = repo.get_source_period_snapshot("2026-09-18T11:00:00.000Z", conn)

    assert result["failed"] is True
    assert any(row["source_code"] == "alert-src" for row in snapshot)


def test_send_due_source_report_skips_when_not_configured(app):
    from app.config import AppConfig

    config = AppConfig()
    object.__setattr__(config, "source_report_webhook_url", "")
    with app.app_context():
        result = send_due_source_report(config, get_db())
    assert result == {"sent": False, "skipped": True, "reason": "source_report_not_configured"}
