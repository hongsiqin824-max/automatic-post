"""模型平台钱包余额告警：TokenHub / AIGW 余额不足或令牌失效时推一条飞书消息。

设计取舍：
  - 两个平台是同一套接口（/api/status + /api/user/self），按 Wallet 参数化，
    各自的阈值、币种、机器人、冷却记录互不影响。
  - 余额换算用平台 /api/status 下发的 quota_per_unit 与 usd_exchange_rate，
    不写死汇率，和网页「当前余额」保持一致（TokenHub 显示人民币，AIGW 显示美元）。
  - 读不到余额（令牌过期、接口挂了）同样告警——静默失效比余额低更危险。
  - 发过之后 cooldown 内不再发；余额回到阈值以上时清掉冷却，下次跌破立即提醒。
  - 先记冷却再发：飞书结果未知时宁可少发，明确失败则撤回冷却下轮重试。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
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
AIGW_LAST_SENT_SETTING = "aigw_balance_alert_last_sent_at"


class TokenHubError(RuntimeError):
    """Balance could not be read from the wallet platform."""


class BalanceAlertResultUnknown(RuntimeError):
    """The request may have reached Feishu, so automatic retry is unsafe."""


@dataclass(frozen=True)
class Wallet:
    name: str
    base_url: str
    access_token: str
    token_env: str
    webhook_url: str
    threshold: float
    currency: str  # "CNY" | "USD"
    setting_key: str
    configured: bool

    @property
    def symbol(self) -> str:
        return "$" if self.currency == "USD" else "¥"


def tokenhub_wallet(config: AppConfig) -> Wallet:
    return Wallet(
        name="TokenHub",
        base_url=config.tokenhub_base_url,
        access_token=config.tokenhub_access_token,
        token_env="TOKENHUB_ACCESS_TOKEN",
        webhook_url=config.balance_alert_resolved_webhook_url,
        threshold=config.balance_alert_threshold_cny,
        currency="CNY",
        setting_key=LAST_SENT_SETTING,
        configured=config.balance_alert_configured,
    )


def aigw_wallet(config: AppConfig) -> Wallet:
    return Wallet(
        name="AIGW",
        base_url=config.aigw_base_url,
        access_token=config.aigw_access_token,
        token_env="AIGW_ACCESS_TOKEN",
        webhook_url=config.aigw_balance_alert_webhook_url,
        threshold=config.aigw_balance_alert_threshold_usd,
        currency="USD",
        setting_key=AIGW_LAST_SENT_SETTING,
        configured=config.aigw_balance_alert_configured,
    )


def all_wallets(config: AppConfig) -> list[Wallet]:
    return [tokenhub_wallet(config), aigw_wallet(config)]


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


def fetch_balance(config: AppConfig, wallet: Wallet | None = None) -> dict[str, Any]:
    """读取钱包：返回原始额度，以及人民币/美元两种口径的余额和总用量。

    ``balance`` / ``used`` 是该钱包自身币种（与网页显示一致）的数值。
    """

    wallet = wallet or tokenhub_wallet(config)
    if not wallet.access_token:
        raise TokenHubError(f"未配置 {wallet.token_env}")
    timeout = config.balance_alert_timeout_seconds
    status = _get_json(f"{wallet.base_url}/api/status", headers=None, timeout=timeout)
    user = _get_json(
        f"{wallet.base_url}/api/user/self",
        headers={"Authorization": f"Bearer {wallet.access_token}"},
        timeout=timeout,
    )
    try:
        quota_per_unit = float(status.get("quota_per_unit") or 500000)
        exchange_rate = float(status.get("usd_exchange_rate") or 1)
        quota = int(user["quota"])
        used_quota = int(user.get("used_quota") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenHubError(f"余额字段无法解析: {exc}") from exc
    usd_factor = 1 / quota_per_unit
    cny_factor = exchange_rate / quota_per_unit
    result = {
        "wallet": wallet.name,
        "currency": wallet.currency,
        "user": user.get("display_name") or user.get("username") or "",
        "quota": quota,
        "used_quota": used_quota,
        "balance_cny": round(quota * cny_factor, 2),
        "used_cny": round(used_quota * cny_factor, 2),
        "balance_usd": round(quota * usd_factor, 2),
        "used_usd": round(used_quota * usd_factor, 2),
        "request_count": int(user.get("request_count") or 0),
    }
    suffix = "usd" if wallet.currency == "USD" else "cny"
    result["balance"] = result[f"balance_{suffix}"]
    result["used"] = result[f"used_{suffix}"]
    return result


def build_low_balance_text(
    balance: dict[str, Any],
    *,
    threshold: float,
    base_url: str,
    now: datetime,
    wallet_name: str = "TokenHub",
    symbol: str = "¥",
) -> str:
    amount = balance.get("balance", balance.get("balance_cny"))
    used = balance.get("used", balance.get("used_cny"))
    return "\n".join(
        [
            f"【余额告警】{wallet_name} 余额 {symbol}{amount:,.2f}，低于 {symbol}{threshold:,.2f}",
            f"时间：{now.astimezone(BEIJING).strftime('%m-%d %H:%M')}",
            f"账号：{balance.get('user') or '-'}",
            f"总用量：{symbol}{used:,.2f}，累计请求 {balance['request_count']:,} 次",
            f"充值/兑换：{base_url}/wallet",
        ]
    )


def build_fetch_failed_text(
    error: str,
    *,
    base_url: str,
    now: datetime,
    wallet_name: str = "TokenHub",
    token_env: str = "TOKENHUB_ACCESS_TOKEN",
) -> str:
    return "\n".join(
        [
            f"【余额告警】{wallet_name} 余额读取失败",
            f"时间：{now.astimezone(BEIJING).strftime('%m-%d %H:%M')}",
            f"错误：{error[:200]}",
            f"请检查 {token_env} 是否失效：{base_url}/profile",
        ]
    )


def send_balance_alert(config: AppConfig, text: str, wallet: Wallet | None = None) -> dict[str, Any]:
    url = (wallet or tokenhub_wallet(config)).webhook_url
    if not url:
        raise ValueError("未配置余额告警飞书机器人")
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
    config: AppConfig,
    connection=None,
    *,
    now: datetime | None = None,
    wallet: Wallet | None = None,
) -> dict[str, Any]:
    """读一次余额；低于阈值或读取失败且不在冷却期就发告警。默认检查 TokenHub。"""

    wallet = wallet or tokenhub_wallet(config)
    if not wallet.configured:
        return {"wallet": wallet.name, "sent": False, "skipped": True, "reason": "not_configured"}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    threshold = wallet.threshold
    base_url = wallet.base_url

    try:
        balance = fetch_balance(config, wallet)
    except TokenHubError as exc:
        error = str(exc)
        logger.warning("%s 余额读取失败: %s", wallet.name, error)
        text = build_fetch_failed_text(
            error, base_url=base_url, now=current, wallet_name=wallet.name, token_env=wallet.token_env
        )
        result: dict[str, Any] = {"wallet": wallet.name, "reason": "fetch_failed", "error": error[:500]}
    else:
        result = {
            "wallet": wallet.name,
            "currency": wallet.currency,
            "balance": balance["balance"],
            "balance_cny": balance["balance_cny"],
            "threshold": threshold,
        }
        if balance["balance"] >= threshold:
            text = ""
        else:
            text = build_low_balance_text(
                balance,
                threshold=threshold,
                base_url=base_url,
                now=current,
                wallet_name=wallet.name,
                symbol=wallet.symbol,
            )
            result["reason"] = "low_balance"

    owns_connection = connection is None
    conn = connection or _connect(config.database_path)
    try:
        if not text:
            logger.info(
                "%s 余额检查正常：%s%.2f（阈值 %s%.2f）",
                wallet.name, wallet.symbol, result["balance"], wallet.symbol, threshold,
            )
            # 余额已恢复：清掉冷却，下次跌破时立即提醒。
            if repo.get_setting(wallet.setting_key, default="", connection=conn):
                repo.set_setting(wallet.setting_key, "", conn)
            return {"sent": False, "skipped": True, "reason": "ok", **result}
        last_sent = _parse(repo.get_setting(wallet.setting_key, default="", connection=conn))
        if last_sent is not None:
            if (current - last_sent).total_seconds() < config.balance_alert_cooldown_seconds:
                return {"sent": False, "skipped": True, **result, "cooldown": True}
        repo.set_setting(wallet.setting_key, _iso(current), conn)
        try:
            send_balance_alert(config, text, wallet)
        except BalanceAlertResultUnknown as exc:
            logger.error("%s 余额告警发送结果未知，不自动重试: %s", wallet.name, exc)
            return {"sent": False, "unknown": True, "send_error": str(exc)[:500], **result}
        except Exception as exc:  # noqa: BLE001 - 明确失败就撤回冷却，下轮再试
            repo.set_setting(wallet.setting_key, "", conn)
            logger.warning("%s 余额告警发送失败: %s", wallet.name, exc)
            return {"sent": False, "failed": True, "send_error": str(exc)[:500], **result}
        logger.warning("余额告警已推送：%s", result)
        return {"sent": True, "message": text, **result}
    finally:
        if owns_connection:
            conn.close()


class BalanceAlertController:
    """Check every configured wallet in a background worker (single flight)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: list[dict] | None = None

    @property
    def last_result(self) -> list[dict] | None:
        return self._last_result

    def start(self) -> bool:
        if not self.config.any_balance_alert_configured:
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
            results = []
            for wallet in all_wallets(self.config):
                if not wallet.configured:
                    continue
                try:
                    results.append(check_and_alert(self.config, conn, wallet=wallet))
                except Exception:  # noqa: BLE001 - 一个钱包出错不影响另一个
                    logger.exception("%s 余额告警任务失败", wallet.name)
            self._last_result = results
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
        if not self.controller.config.any_balance_alert_configured:
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
