from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import repository as repo
from app.config import AppConfig
from app.db import get_db
from app.services import publish_alert as pa


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _config(database: str, **overrides) -> AppConfig:
    base = {
        "database_path": database,
        "scheduler_enabled": False,
        "publish_alert_enabled": True,
        "publish_alert_webhook_url": "https://example.invalid/alert",
        "publish_alert_threshold": 3,
        "publish_alert_lookback_seconds": 900,
        "publish_alert_cooldown_seconds": 1800,
    }
    base.update(overrides)
    return AppConfig(**base)


def _fail_article(conn, index: int, error: str) -> int:
    article = repo.upsert_material(
        {
            "source": "marca",
            "source_url": f"https://example.com/alert-{index}",
            "translate_title": f"告警测试文章 {index}",
            "translate_body": "<p>正文</p>",
            "archive_id": 0,
            "channels": [],
        },
        conn,
    )["article"]
    repo.transition_status(article["id"], "PUBLISH_FAILED", conn, message=error)
    return int(article["id"])


def _capture_posts(monkeypatch, calls: list, payload: dict | None = None):
    monkeypatch.setattr(
        "app.services.publish_alert.requests.post",
        lambda url, **kwargs: (calls.append((url, kwargs)), _Response(payload or {"code": 0}))[1],
    )


def test_below_threshold_does_not_alert(app, monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    with app.app_context():
        conn = get_db()
        _fail_article(conn, 1, "Token Service 未授权")
        _fail_article(conn, 2, "Token Service 未授权")
        result = pa.check_and_alert(_config(app.config["DATABASE"]), conn)
    assert result["skipped"] is True and result["reason"] == "below_threshold"
    assert result["count"] == 2
    assert calls == []


def test_threshold_reached_sends_once_then_cools_down(app, monkeypatch):
    """达阈值发一条；冷却期内再检查不重复发；冷却过了持续故障再发。"""

    calls: list = []
    _capture_posts(monkeypatch, calls)
    config = _config(app.config["DATABASE"])
    now = datetime.now(timezone.utc)
    with app.app_context():
        conn = get_db()
        for index in range(1, 5):
            _fail_article(conn, index, "Token Service 未授权，请访问 https://x/auth/start")
        _fail_article(conn, 9, "直接发布请求失败: 创建文章接口返回 HTTP 502")

        first = pa.check_and_alert(config, conn, now=now)
        assert first["sent"] is True and first["count"] == 5
        assert len(calls) == 1
        text = calls[0][1]["json"]["content"]["text"]
        assert "5 篇提交懂球帝失败" in text
        assert "Token Service 未授权" in text and "×4" in text
        assert "HTTP 502" in text and "×1" in text
        assert "retry_publish_failed.py" in text
        assert calls[0][0] == "https://example.invalid/alert"

        second = pa.check_and_alert(config, conn, now=now + timedelta(minutes=5))
        assert second["skipped"] is True and second["reason"] == "cooldown"
        assert len(calls) == 1

        third = pa.check_and_alert(config, conn, now=now + timedelta(minutes=31))
        # 窗口是 15 分钟，31 分钟后这批已经出窗，不该再发。
        assert third["reason"] == "below_threshold"
        assert len(calls) == 1


def test_lookback_window_ignores_old_failures(app, monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    config = _config(app.config["DATABASE"])
    with app.app_context():
        conn = get_db()
        for index in range(1, 4):
            _fail_article(conn, index, "旧故障")
        # 把它们的失败时间推到 1 小时前。
        conn.execute(
            "UPDATE articles SET updated_at=? WHERE status='PUBLISH_FAILED'",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),),
        )
        conn.commit()
        result = pa.check_and_alert(config, conn)
    assert result["reason"] == "below_threshold" and result["count"] == 0
    assert calls == []


def test_send_failure_clears_cooldown_so_next_tick_retries(app, monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls, payload={"code": 19001, "msg": "bad"})
    config = _config(app.config["DATABASE"])
    with app.app_context():
        conn = get_db()
        for index in range(1, 4):
            _fail_article(conn, index, "x")
        result = pa.check_and_alert(config, conn)
        assert result["failed"] is True
        assert repo.get_setting(pa.LAST_SENT_SETTING, default="", connection=conn) == ""

        _capture_posts(monkeypatch, calls)
        retry = pa.check_and_alert(config, conn)
        assert retry["sent"] is True


def test_webhook_falls_back_to_source_report_bot():
    config = AppConfig(
        publish_alert_enabled=True,
        publish_alert_webhook_url="",
        source_report_webhook_url="https://example.invalid/source",
    )
    assert config.publish_alert_configured is True
    assert config.publish_alert_resolved_webhook_url == "https://example.invalid/source"
    disabled = AppConfig(publish_alert_enabled=False, source_report_webhook_url="https://x")
    assert disabled.publish_alert_configured is False


def test_not_configured_skips_without_touching_db(monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    config = AppConfig(
        publish_alert_enabled=True, publish_alert_webhook_url="", source_report_webhook_url=""
    )
    result = pa.check_and_alert(config, connection=object())
    assert result["reason"] == "not_configured"
    assert calls == []
