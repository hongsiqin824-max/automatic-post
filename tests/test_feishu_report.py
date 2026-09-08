from __future__ import annotations

import threading
from datetime import datetime, timezone

from app import repository as repo
from app.config import AppConfig
from app.db import get_db
from app.services.feishu_report import (
    build_report_text,
    FeishuReportController,
    FeishuReportResultUnknown,
    report_period,
    send_feishu_report,
    send_due_report,
)


def test_report_period_uses_beijing_19_boundary() -> None:
    before = datetime(2026, 9, 8, 10, 59, tzinfo=timezone.utc)  # 18:59 Beijing
    assert report_period(before) == (
        "2026-09-06T11:00:00.000Z",
        "2026-09-07T11:00:00.000Z",
    )

    after = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)  # 19:00 Beijing
    assert report_period(after) == (
        "2026-09-07T11:00:00.000Z",
        "2026-09-08T11:00:00.000Z",
    )


def test_direct_publish_report_counts_published_articles_by_snapshot_tabs(app) -> None:
    with app.app_context():
        first = repo.create_tab("统计栏目一", 910001)
        second = repo.create_tab("统计栏目二", 910002)
        article_one = repo.upsert_material({
            "source": "report-source",
            "source_url": "https://example.com/report/1",
            "translate_title": "统计文章一",
            "translate_body": "<p>正文一</p>",
            "archive_id": 0,
        })["article"]
        article_two = repo.upsert_material({
            "source": "report-source",
            "source_url": "https://example.com/report/2",
            "translate_title": "统计文章二",
            "translate_body": "<p>正文二</p>",
            "archive_id": 0,
        })["article"]
        conn = get_db()
        repo.assign_article_tabs(article_one["id"], [first["id"], second["id"]], conn)
        repo.assign_article_tabs(article_two["id"], [second["id"]], conn)
        with conn:
            conn.execute(
                "UPDATE articles SET publish_mode=1 WHERE id IN (?, ?)",
                (article_one["id"], article_two["id"]),
            )
        repo.transition_status(article_one["id"], "PUBLISHED", conn)
        repo.transition_status(article_two["id"], "PUBLISHED", conn)
        with conn:
            conn.execute(
                "UPDATE articles SET published_at=? WHERE id IN (?, ?)",
                ("2026-09-07T12:00:00Z", article_one["id"], article_two["id"]),
            )
        repo.update_tab(first["id"], name="已改名栏目", connection=conn)
        report = repo.direct_publish_report(
            "2026-09-07T11:00:00.000Z", "2026-09-08T11:00:00.000Z", conn
        )
        assert report["article_count"] == 2
        assert report["tab_counts"] == [
            {"name": "统计栏目一", "count": 1},
            {"name": "统计栏目二", "count": 2},
        ]


def test_direct_publish_report_uses_half_open_millisecond_boundaries(app) -> None:
    timestamps = (
        "2026-09-07T10:59:59.999Z",
        "2026-09-07T11:00:00.000Z",
        "2026-09-07T11:00:00.123Z",
        "2026-09-08T11:00:00.000Z",
    )
    with app.app_context():
        conn = get_db()
        with conn:
            conn.executemany(
                """
                INSERT INTO articles
                (source, source_url, status, publish_mode, published_at)
                VALUES ('boundary-test', ?, 'PUBLISHED', 1, ?)
                """,
                [(f"https://example.com/boundary/{index}", value) for index, value in enumerate(timestamps)],
            )
        report = repo.direct_publish_report(
            "2026-09-07T11:00:00.000Z",
            "2026-09-08T11:00:00.000Z",
            conn,
        )
    assert report["article_count"] == 2
    assert report["tab_counts"] == [{"name": "未配置栏目", "count": 2}]


def test_confirmed_direct_publish_gets_time_and_tab_snapshot(app) -> None:
    with app.app_context():
        tab = repo.create_tab("确认发布栏目", 910003)
        article = repo.upsert_material({
            "source": "report-confirmed-source",
            "source_url": "https://example.com/report/confirmed",
            "translate_title": "确认后直接发布文章",
            "translate_body": "<p>正文</p>",
            "archive_id": 0,
        })["article"]
        conn = get_db()
        repo.assign_article_tabs(article["id"], [tab["id"]], conn)
        with conn:
            conn.execute("UPDATE articles SET publish_mode=1 WHERE id=?", (article["id"],))
        repo.transition_status(article["id"], "DRAFT_CONFIRMING", conn)
        updated = repo.record_draft_confirmation_result(
            article["id"],
            conn,
            outcome="CREATED",
            dqd_archive_id=7654321,
            publish_mode=1,
            target_status="PUBLISHED",
        )
        assert updated["status"] == "PUBLISHED"
        assert updated["published_at"]
        assert updated["published_tab_names_json"] == '["确认发布栏目"]'


def test_send_due_report_is_idempotent_and_retries_failed_delivery(app, monkeypatch) -> None:
    config = AppConfig(
        database_path=app.config["DATABASE"],
        feishu_report_webhook_url="https://example.invalid/hook",
        feishu_report_enabled=True,
        feishu_report_retry_seconds=60,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.feishu_report.send_feishu_report",
        lambda _config, text: calls.append(text) or {"code": 0},
    )
    now = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
    with app.app_context():
        first = send_due_report(config, get_db(), now=now)
        second = send_due_report(config, get_db(), now=now)
    assert first["sent"] is True
    assert second["skipped"] is True
    assert len(calls) == 1
    assert "直接发布合计：0 篇" in calls[0]


def test_first_report_waits_until_beijing_19(app, monkeypatch) -> None:
    config = AppConfig(
        database_path=app.config["DATABASE"],
        feishu_report_webhook_url="https://example.invalid/hook",
        feishu_report_enabled=True,
    )
    monkeypatch.setattr(
        "app.services.feishu_report.send_feishu_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    with app.app_context():
        result = send_due_report(
            config,
            get_db(),
            now=datetime(2026, 9, 8, 10, 59, tzinfo=timezone.utc),
        )
    assert result == {"sent": False, "skipped": True, "reason": "first_period_not_due"}


def test_failed_report_is_retryable_after_backoff(app, monkeypatch) -> None:
    config = AppConfig(
        database_path=app.config["DATABASE"],
        feishu_report_webhook_url="https://example.invalid/hook",
        feishu_report_enabled=True,
        feishu_report_retry_seconds=60,
    )
    attempts = iter([RuntimeError("temporary outage"), {"code": 0}])

    def fake_send(_config, _text):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "app.services.feishu_report.send_feishu_report",
        fake_send,
    )
    now = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
    with app.app_context():
        first = send_due_report(config, get_db(), now=now)
        assert first["failed"] is True
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE report_deliveries SET next_attempt_at='2000-01-01T00:00:00Z'"
            )
        second = send_due_report(config, get_db(), now=now)
    assert second["sent"] is True


def test_service_catches_up_one_missing_period_at_a_time(app, monkeypatch) -> None:
    config = AppConfig(
        database_path=app.config["DATABASE"],
        feishu_report_webhook_url="https://example.invalid/hook",
        feishu_report_enabled=True,
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "app.services.feishu_report.send_feishu_report",
        lambda _config, text: messages.append(text) or {"code": 0},
    )
    with app.app_context():
        first = send_due_report(
            config, get_db(), now=datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc)
        )
        catchup = send_due_report(
            config, get_db(), now=datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
        )
        latest = send_due_report(
            config, get_db(), now=datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
        )
    assert first["period"] == ("2026-09-05T11:00:00.000Z", "2026-09-06T11:00:00.000Z")
    assert catchup["period"] == ("2026-09-06T11:00:00.000Z", "2026-09-07T11:00:00.000Z")
    assert latest["period"] == ("2026-09-07T11:00:00.000Z", "2026-09-08T11:00:00.000Z")
    assert len(messages) == 3


def test_ambiguous_send_result_is_not_retried(app, monkeypatch) -> None:
    config = AppConfig(
        database_path=app.config["DATABASE"],
        feishu_report_webhook_url="https://example.invalid/hook",
        feishu_report_enabled=True,
    )
    calls = 0

    def unknown_send(_config, _text):
        nonlocal calls
        calls += 1
        raise FeishuReportResultUnknown("read timeout")

    monkeypatch.setattr("app.services.feishu_report.send_feishu_report", unknown_send)
    now = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
    with app.app_context():
        first = send_due_report(config, get_db(), now=now)
        second = send_due_report(config, get_db(), now=now)
        row = get_db().execute("SELECT status, next_attempt_at FROM report_deliveries").fetchone()
        with get_db():
            get_db().execute(
                "UPDATE report_deliveries SET claimed_at='2000-01-01T00:00:00Z'"
            )
        third = send_due_report(config, get_db(), now=now)
    assert first["unknown"] is True
    assert second["skipped"] is True
    assert third["skipped"] is True
    assert calls == 1
    assert tuple(row) == ("SENDING", None)


def test_stale_sending_delivery_can_be_reclaimed_after_process_crash(app) -> None:
    with app.app_context():
        conn = get_db()
        claimed = repo.claim_report_delivery(
            "2026-09-07T11:00:00.000Z",
            "2026-09-08T11:00:00.000Z",
            conn,
            stale_after_seconds=60,
        )
        with conn:
            conn.execute(
                "UPDATE report_deliveries SET claimed_at='2000-01-01T00:00:00Z', next_attempt_at='2099-01-01T00:00:00Z'"
            )
        reclaimed = repo.claim_report_delivery(
            "2026-09-07T11:00:00.000Z",
            "2026-09-08T11:00:00.000Z",
            conn,
            stale_after_seconds=60,
        )
    assert reclaimed is not None
    assert reclaimed["id"] == claimed["id"]
    assert reclaimed["claim_token"] != claimed["claim_token"]


def test_report_text_contains_period_and_tab_counts() -> None:
    text = build_report_text(
        "2026-09-06T11:00:00.000Z",
        "2026-09-07T11:00:00.000Z",
        {"article_count": 3, "tab_counts": [{"name": "日职联", "count": 3}]},
    )
    assert "统计周期：2026-09-06 19:00 至 2026-09-07 19:00" in text
    assert "多栏目文章按每个栏目分别计数" in text
    assert "日职联：3 篇" in text


def test_feishu_response_must_explicitly_report_success(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"unexpected": "response"}

    monkeypatch.setattr(
        "app.services.feishu_report.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )
    config = AppConfig(feishu_report_webhook_url="https://example.invalid/hook")
    try:
        send_feishu_report(config, "report")
    except FeishuReportResultUnknown as exc:
        assert "结果无法识别" in str(exc)
    else:
        raise AssertionError("unexpected response must not be recorded as sent")


def test_report_controller_starts_single_flight_worker(app, monkeypatch) -> None:
    config = AppConfig(
        database_path=app.config["DATABASE"],
        feishu_report_webhook_url="https://example.invalid/hook",
    )
    called = threading.Event()
    controller = FeishuReportController(config)
    monkeypatch.setattr(controller, "_run", called.set)
    assert controller.start() is True
    assert called.wait(1)
