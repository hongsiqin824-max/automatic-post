from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import repository as repo
from app.config import AppConfig
from app.db import get_db
from app.services import balance_alert as ba


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


STATUS = {"success": True, "data": {"quota_per_unit": 500000, "usd_exchange_rate": 7.3}}


def _user(quota: int) -> dict:
    return {
        "success": True,
        "data": {
            "display_name": "tester",
            "quota": quota,
            "used_quota": 280361573,
            "request_count": 188679,
        },
    }


def _config(database: str = ":memory:", **overrides) -> AppConfig:
    base = {
        "database_path": database,
        "scheduler_enabled": False,
        "tokenhub_base_url": "https://tokenhub.invalid",
        "tokenhub_access_token": "test-token",
        "balance_alert_enabled": True,
        "balance_alert_webhook_url": "https://example.invalid/alert",
        "balance_alert_threshold_cny": 100.0,
        "balance_alert_cooldown_seconds": 21600,
    }
    base.update(overrides)
    return AppConfig(**base)


def _mock_tokenhub(monkeypatch, user_payload, gets: list | None = None):
    def fake_get(url, headers=None, timeout=None):
        if gets is not None:
            gets.append((url, headers))
        if url.endswith("/api/status"):
            return _Response(STATUS)
        status_code = 401 if user_payload.get("success") is False else 200
        return _Response(user_payload, status_code)

    monkeypatch.setattr("app.services.balance_alert.requests.get", fake_get)


def _capture_posts(monkeypatch, calls: list, payload: dict | None = None):
    monkeypatch.setattr(
        "app.services.balance_alert.requests.post",
        lambda url, **kwargs: (calls.append((url, kwargs)), _Response(payload or {"code": 0}))[1],
    )


def test_fetch_balance_converts_quota_with_platform_rate(monkeypatch):
    gets: list = []
    _mock_tokenhub(monkeypatch, _user(62104381), gets)
    balance = ba.fetch_balance(_config())
    assert balance["balance_cny"] == pytest.approx(906.72)
    assert balance["used_cny"] == pytest.approx(4093.28)
    assert gets[1] == (
        "https://tokenhub.invalid/api/user/self",
        {"Authorization": "Bearer test-token"},
    )


def test_balance_above_threshold_does_not_alert(app, monkeypatch):
    calls: list = []
    _mock_tokenhub(monkeypatch, _user(62104381))
    _capture_posts(monkeypatch, calls)
    with app.app_context():
        result = ba.check_and_alert(_config(app.config["DATABASE"]), get_db())
    assert result["reason"] == "ok" and result["sent"] is False
    assert calls == []


def test_low_balance_sends_once_then_cools_down_and_rearms_after_topup(app, monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    config = _config(app.config["DATABASE"])
    now = datetime.now(timezone.utc)
    with app.app_context():
        conn = get_db()
        _mock_tokenhub(monkeypatch, _user(5000000))  # ¥73
        first = ba.check_and_alert(config, conn, now=now)
        assert first["sent"] is True and first["reason"] == "low_balance"
        text = calls[0][1]["json"]["content"]["text"]
        assert "¥73.00" in text and "低于 ¥100.00" in text
        assert "https://tokenhub.invalid/wallet" in text

        second = ba.check_and_alert(config, conn, now=now + timedelta(hours=1))
        assert second["cooldown"] is True and len(calls) == 1

        _mock_tokenhub(monkeypatch, _user(62104381))  # 充值后恢复
        recovered = ba.check_and_alert(config, conn, now=now + timedelta(hours=2))
        assert recovered["reason"] == "ok"
        assert repo.get_setting(ba.LAST_SENT_SETTING, default="", connection=conn) == ""

        _mock_tokenhub(monkeypatch, _user(5000000))
        again = ba.check_and_alert(config, conn, now=now + timedelta(hours=3))
        assert again["sent"] is True and len(calls) == 2


def test_invalid_token_alerts_fetch_failed(app, monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    _mock_tokenhub(
        monkeypatch, {"success": False, "message": "Unauthorized, invalid access token"}
    )
    with app.app_context():
        result = ba.check_and_alert(_config(app.config["DATABASE"]), get_db())
    assert result["sent"] is True and result["reason"] == "fetch_failed"
    assert "invalid access token" in calls[0][1]["json"]["content"]["text"]


def test_send_failure_clears_cooldown(app, monkeypatch):
    calls: list = []
    _mock_tokenhub(monkeypatch, _user(5000000))
    _capture_posts(monkeypatch, calls, payload={"code": 19001, "msg": "bad"})
    config = _config(app.config["DATABASE"])
    with app.app_context():
        conn = get_db()
        result = ba.check_and_alert(config, conn)
        assert result["failed"] is True
        assert repo.get_setting(ba.LAST_SENT_SETTING, default="", connection=conn) == ""


def test_not_configured_without_token_or_webhook(monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    no_token = _config(tokenhub_access_token="")
    assert ba.check_and_alert(no_token, connection=object())["reason"] == "not_configured"
    no_webhook = AppConfig(
        tokenhub_access_token="t",
        balance_alert_webhook_url="",
        publish_alert_webhook_url="",
        source_report_webhook_url="",
    )
    assert no_webhook.balance_alert_configured is False
    fallback = AppConfig(
        tokenhub_access_token="t",
        balance_alert_webhook_url="",
        publish_alert_webhook_url="",
        source_report_webhook_url="https://x",
    )
    assert fallback.balance_alert_resolved_webhook_url == "https://x"
    assert calls == []


def _aigw_config(database: str = ":memory:", **overrides) -> AppConfig:
    return _config(
        database,
        aigw_base_url="https://aigw.invalid",
        aigw_access_token="aigw-token",
        aigw_balance_alert_webhook_url="https://example.invalid/aigw",
        aigw_balance_alert_threshold_usd=100.0,
        **overrides,
    )


def test_aigw_low_balance_alerts_in_usd_to_its_own_robot(app, monkeypatch):
    calls: list = []
    gets: list = []
    _capture_posts(monkeypatch, calls)
    _mock_tokenhub(monkeypatch, _user(40000000), gets)  # $80，¥584 —— 按美元判断才会告警
    config = _aigw_config(app.config["DATABASE"])
    with app.app_context():
        conn = get_db()
        result = ba.check_and_alert(config, conn, wallet=ba.aigw_wallet(config))
        assert result["sent"] is True and result["wallet"] == "AIGW"
        assert repo.get_setting(ba.AIGW_LAST_SENT_SETTING, default="", connection=conn)
        assert repo.get_setting(ba.LAST_SENT_SETTING, default="", connection=conn) == ""
    url, kwargs = calls[0]
    text = kwargs["json"]["content"]["text"]
    assert url == "https://example.invalid/aigw"
    assert "AIGW 余额 $80.00，低于 $100.00" in text
    assert "https://aigw.invalid/wallet" in text
    assert gets[1] == ("https://aigw.invalid/api/user/self", {"Authorization": "Bearer aigw-token"})


def test_aigw_requires_its_own_webhook():
    config = _config(aigw_access_token="t", aigw_balance_alert_webhook_url="")
    assert config.aigw_balance_alert_configured is False
    assert config.any_balance_alert_configured is True


def test_controller_checks_every_configured_wallet(app, monkeypatch):
    calls: list = []
    _capture_posts(monkeypatch, calls)
    _mock_tokenhub(monkeypatch, _user(5000000))  # ¥73 / $10，两边都低
    controller = ba.BalanceAlertController(_aigw_config(app.config["DATABASE"]))
    controller._run()
    assert [r["wallet"] for r in controller.last_result] == ["TokenHub", "AIGW"]
    assert [url for url, _ in calls] == ["https://example.invalid/alert", "https://example.invalid/aigw"]

