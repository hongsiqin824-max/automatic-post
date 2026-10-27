"""TokenHub 余额告警：降级模型平台余额不足或令牌失效时推一条飞书消息。

设计取舍：
  - 余额换算用平台 /api/status 下发的 quota_per_unit 与 usd_exchange_rate，
    不写死汇率，管理员改了汇率也和网页「当前余额」保持一致。
  - 读不到余额（令牌过期、接口挂了）同样告警——静默失效比余额低更危险。
  - 发过之后 cooldown 内不再发；余额回到阈值以上时清掉冷却，下次跌破立即提醒。
  - 先记冷却再发：飞书结果未知时宁可少发，明确失败则撤回冷却下轮重试。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect


logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")

LAST_SENT_SETTING = "balance_alert_last_sent_at"


class TokenHubError(RuntimeError):
    """Balance could not be read from TokenHub."""


class BalanceAlertResultUnknown(RuntimeError):
    """The request may have reached Feishu, so automatic retry is unsafe."""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _get_json(url: str, *, headers: dict[str, str] | None, timeout: int) -> dict[str, Any]:
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise TokenHubError(f"请求 {url} 失败: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenHubError(f"{url} 返回非 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(payload, dict) or payload.get("success") is False or response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else payload
        raise TokenHubError(f"{url} 返回失败（HTTP {response.status_code}）: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TokenHubError(f"{url} 返回缺少 data 字段")
    return data


def fetch_balance(config: AppConfig) -> dict[str, Any]:
    """读取 TokenHub 钱包：返回原始额度和换算成人民币后的余额/总用量。"""

    if not config.tokenhub_access_token:
        raise TokenHubError("未配置 TOKENHUB_ACCESS_TOKEN")
    timeout = config.balance_alert_timeout_seconds
    status = _get_json(f"{config.tokenhub_base_url}/api/status", headers=None, timeout=timeout)
    user = _get_json(
        f"{config.tokenhub_base_url}/api/user/self",
        headers={"Authorization": f"Bearer {config.tokenhub_access_token}"},
        timeout=timeout,
    )
    try:
        quota_per_unit = float(status.get("quota_per_unit") or 500000)
        exchange_rate = float(status.get("usd_exchange_rate") or 1)
        quota = int(user["quota"])
        used_quota = int(user.get("used_quota") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenHubError(f"余额字段无法解析: {exc}") from exc
    factor = exchange_rate / quota_per_unit
    return {
        "user": user.get("display_name") or user.get("username") or "",
        "quota": quota,
        "used_quota": used_quota,
        "balance_cny": round(quota * factor, 2),
        "used_cny": round(used_quota * factor, 2),
        "request_count": int(user.get("request_count") or 0),
    }


def build_low_balance_text(balance: dict[str, Any], *, threshold: float, base_url: str, now: datetime) -> str:
    return "\n".join(
        [
            f"【余额告警】TokenHub 余额 ¥{balance['balance_cny']:,.2f}，低于 ¥{threshold:,.2f}",
            f"时间：{now.astimezone(BEIJING).strftime('%m-%d %H:%M')}",
            f"账号：{balance.get('user') or '-'}",
            f"总用量：¥{balance['used_cny']:,.2f}，累计请求 {balance['request_count']:,} 次",
            f"充值/兑换：{base_url}/wallet",
        ]
    )


def build_fetch_failed_text(error: str, *, base_url: str, now: datetime) -> str:
    return "\n".join(
        [
            "【余额告警】TokenHub 余额读取失败",
            f"时间：{now.astimezone(BEIJING).strftime('%m-%d %H:%M')}",
            f"错误：{error[:200]}",
            f"请检查 TOKENHUB_ACCESS_TOKEN 是否失效：{base_url}/profile",
        ]
    )


def send_balance_alert(config: AppConfig, text: str) -> dict[str, Any]:
    url = config.balance_alert_resolved_webhook_url
    if not url:
        raise ValueError("未配置 BALANCE_ALERT_WEBHOOK_URL / PUBLISH_ALERT_WEBHOOK_URL / SOURCE_REPORT_WEBHOOK_URL")
    try:
        response = requests.post(
            url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=config.balance_alert_timeout_seconds,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise BalanceAlertResultUnknown(f"飞书请求结果未知: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"飞书机器人 HTTP {response.status_code}: {payload}")
    if not isinstance(payload, dict):
        raise BalanceAlertResultUnknown("飞书请求已完成，但返回结果无法识别")
    if "code" in payload:
        successful = payload.get("code") == 0
    elif "StatusCode" in payload:
        successful = payload.get("StatusCode") == 0
    else:
        raise BalanceAlertResultUnknown(f"飞书请求已完成，但返回结果无法识别: {payload}")
    if not successful:
        raise RuntimeError(f"飞书机器人返回失败: {payload}")
    return payload


def check_and_alert(
    config: AppConfig, connection=None, *, now: datetime | None = None
) -> dict[str, Any]:
    """读一次余额；低于阈值或读取失败且不在冷却期就发告警。"""

    if not config.balance_alert_configured:
        return {"sent": False, "skipped": True, "reason": "not_configured"}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    threshold = config.balance_alert_threshold_cny
    base_url = config.tokenhub_base_url

    balance: dict[str, Any] | None = None
    try:
        balance = fetch_balance(config)
    except TokenHubError as exc:
        error = str(exc)
        logger.warning("TokenHub 余额读取失败: %s", error)
        text = build_fetch_failed_text(error, base_url=base_url, now=current)
        result: dict[str, Any] = {"reason": "fetch_failed", "error": error[:500]}
    else:
        result = {"balance_cny": balance["balance_cny"], "threshold_cny": threshold}
        if balance["balance_cny"] >= threshold:
            text = ""
        else:
            text = build_low_balance_text(balance, threshold=threshold, base_url=base_url, now=current)
            result["reason"] = "low_balance"

    owns_connection = connection is None
    conn = connection or _connect(config.database_path)
    try:
        if not text:
            logger.info("TokenHub 余额检查正常：¥%.2f（阈值 ¥%.2f）", result["balance_cny"], threshold)
            # 余额已恢复：清掉冷却，下次跌破时立即提醒。
            if repo.get_setting(LAST_SENT_SETTING, default="", connection=conn):
                repo.set_setting(LAST_SENT_SETTING, "", conn)
            return {"sent": False, "skipped": True, "reason": "ok", **result}
        last_sent = _parse(repo.get_setting(LAST_SENT_SETTING, default="", connection=conn))
        if last_sent is not None:
            if (current - last_sent).total_seconds() < config.balance_alert_cooldown_seconds:
                return {"sent": False, "skipped": True, **result, "cooldown": True}
        repo.set_setting(LAST_SENT_SETTING, _iso(current), conn)
        try:
            send_balance_alert(config, text)
        except BalanceAlertResultUnknown as exc:
            logger.error("余额告警发送结果未知，不自动重试: %s", exc)
            return {"sent": False, "unknown": True, "send_error": str(exc)[:500], **result}
        except Exception as exc:  # noqa: BLE001 - 明确失败就撤回冷却，下轮再试
            repo.set_setting(LAST_SENT_SETTING, "", conn)
            logger.warning("余额告警发送失败: %s", exc)
            return {"sent": False, "failed": True, "send_error": str(exc)[:500], **result}
        logger.warning("余额告警已推送：%s", result)
        return {"sent": True, "message": text, **result}
    finally:
        if owns_connection:
            conn.close()


class BalanceAlertController:
    """Run one balance check in a background worker (single flight)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict | None = None

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    def start(self) -> bool:
        if not self.config.balance_alert_configured:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(target=self._run, name="balance-alert-worker", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True

    def _run(self) -> None:
        conn = None
        try:
            conn = _connect(self.config.database_path)
            self._last_result = check_and_alert(self.config, conn)
        except Exception:  # noqa: BLE001 - keep future scheduler ticks alive
            logger.exception("余额告警任务失败")
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self._running = False


class BalanceAlertScheduler:
    """Poll on a fixed interval, independent of the ingestion scheduler switch."""

    def __init__(self, controller: BalanceAlertController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = max(60, int(interval_seconds))
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.controller.config.balance_alert_configured:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop, name="balance-alert-scheduler", daemon=True
            )
            self._thread.start()
        return True

    def shutdown(self, timeout: float | None = 2.0) -> bool:
        self._shutdown.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _loop(self) -> None:
        self.controller.start()
        while not self._shutdown.wait(self.interval_seconds):
            self.controller.start()
