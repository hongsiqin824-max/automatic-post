"""提交懂球帝失败告警：批量失败时推一条飞书消息，不再靠人看列表发现。

背景：2026-10-19 夜间上游 Token Service 授权过期，11 小时内 198 篇全部
PUBLISH_FAILED，没有任何告警，第二天早上用户翻列表才发现。

设计取舍：
  - 不在每次落 PUBLISH_FAILED 时同步发消息——单篇失败常见（502 抖动、标题查重
    撞上），发了只会刷屏。改为定时窗口计数：最近 lookback 秒内 ≥ threshold 篇才发。
  - 发过之后 cooldown 内不再发，但窗口计数照常算；上游持续故障每个 cooldown
    提醒一次，恢复后自然停。
  - 不用游标：窗口是「现在往前 lookback 秒」，两次检查之间漏不掉也不会重复算错，
    重启后也不需要恢复状态。
  - webhook 不单独配时沿用来源预警机器人（SOURCE_REPORT_WEBHOOK_URL），这样不加
    新密钥就能开。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect


logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")

LAST_SENT_SETTING = "publish_alert_last_sent_at"
# 同一类错误的消息往往只在末尾带文章 ID 或请求 ID，按前缀归并统计。
_ERROR_GROUP_PREFIX = 60
_MAX_ERROR_GROUPS = 4
_MAX_EXAMPLES = 3


class PublishAlertResultUnknown(RuntimeError):
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


def build_alert_text(
    failures: list[dict[str, Any]],
    *,
    lookback_seconds: int,
    now: datetime,
    base_url: str = "http://127.0.0.1:8890",
) -> str:
    """把一批失败归并成一条可读的群消息：数量、主要错误、示例、处理入口。"""

    groups: Counter[str] = Counter()
    for row in failures:
        groups[str(row.get("error") or "（无错误信息）")[:_ERROR_GROUP_PREFIX]] += 1
    local = now.astimezone(BEIJING)
    minutes = max(1, int(lookback_seconds // 60))
    lines = [
        f"【发布告警】最近 {minutes} 分钟 {len(failures)} 篇提交懂球帝失败",
        f"时间：{local.strftime('%m-%d %H:%M')}",
        "主要错误：",
    ]
    for message, count in groups.most_common(_MAX_ERROR_GROUPS):
        lines.append(f"- {message} ×{count}")
    lines.append("示例：")
    for row in failures[:_MAX_EXAMPLES]:
        title = str(row.get("title_final") or row.get("title_original") or "")[:30]
        lines.append(f"- #{row.get('id')} {title}")
    lines.append(f"查看：{base_url}/articles?status=publish_failed")
    lines.append(
        "恢复后批量重试：python3 scripts/retry_publish_failed.py "
        "--match \"<错误关键词>\" --limit 1（先试一篇）"
    )
    return "\n".join(lines)


def send_publish_alert(config: AppConfig, text: str) -> dict[str, Any]:
    """Send plain text to the alert bot and validate the bot response."""

    url = config.publish_alert_resolved_webhook_url
    if not url:
        raise ValueError("未配置 PUBLISH_ALERT_WEBHOOK_URL / SOURCE_REPORT_WEBHOOK_URL")
    try:
        response = requests.post(
            url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=config.publish_alert_timeout_seconds,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise PublishAlertResultUnknown(f"飞书请求结果未知: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"飞书机器人 HTTP {response.status_code}: {payload}")
    if not isinstance(payload, dict):
        raise PublishAlertResultUnknown("飞书请求已完成，但返回结果无法识别")
    if "code" in payload:
        successful = payload.get("code") == 0
    elif "StatusCode" in payload:
        successful = payload.get("StatusCode") == 0
    else:
        raise PublishAlertResultUnknown(f"飞书请求已完成，但返回结果无法识别: {payload}")
    if not successful:
        raise RuntimeError(f"飞书机器人返回失败: {payload}")
    return payload


def check_and_alert(
    config: AppConfig, connection=None, *, now: datetime | None = None
) -> dict[str, Any]:
    """窗口内失败数达阈值且不在冷却期就发一条告警。"""

    if not config.publish_alert_configured:
        return {"sent": False, "skipped": True, "reason": "not_configured"}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    owns_connection = connection is None
    conn = connection or _connect(config.database_path)
    try:
        since = _iso(current - timedelta(seconds=config.publish_alert_lookback_seconds))
        failures = repo.list_recent_publish_failures(since, conn)
        count = len(failures)
        if count < config.publish_alert_threshold:
            return {"sent": False, "skipped": True, "reason": "below_threshold", "count": count}
        last_sent = _parse(repo.get_setting(LAST_SENT_SETTING, default="", connection=conn))
        if last_sent is not None:
            elapsed = (current - last_sent).total_seconds()
            if elapsed < config.publish_alert_cooldown_seconds:
                return {"sent": False, "skipped": True, "reason": "cooldown", "count": count}
        text = build_alert_text(
            failures, lookback_seconds=config.publish_alert_lookback_seconds, now=current
        )
        # 先记再发：结果未知时宁可少发一条，也不要在飞书那边刷出重复告警。
        repo.set_setting(LAST_SENT_SETTING, _iso(current), conn)
        try:
            send_publish_alert(config, text)
        except PublishAlertResultUnknown as exc:
            logger.error("发布告警发送结果未知，不自动重试: %s", exc)
            return {"sent": False, "unknown": True, "error": str(exc)[:500], "count": count}
        except Exception as exc:  # noqa: BLE001 - 明确失败就撤回冷却，下轮再试
            repo.set_setting(LAST_SENT_SETTING, "", conn)
            logger.warning("发布告警发送失败: %s", exc)
            return {"sent": False, "failed": True, "error": str(exc)[:500], "count": count}
        logger.warning("发布告警已推送：最近窗口 %d 篇提交失败", count)
        return {"sent": True, "count": count, "message": text}
    finally:
        if owns_connection:
            conn.close()


class PublishAlertController:
    """Run one alert check in a background worker (single flight)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict | None = None

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    def start(self) -> bool:
        if not self.config.publish_alert_configured:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(target=self._run, name="publish-alert-worker", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True

    def _run(self) -> None:
        # _connect 必须在 try 里，连接失败也要复位 _running，否则线程永久卡死。
        conn = None
        try:
            conn = _connect(self.config.database_path)
            self._last_result = check_and_alert(self.config, conn)
        except Exception:  # noqa: BLE001 - keep future scheduler ticks alive
            logger.exception("发布告警任务失败")
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self._running = False


class PublishAlertScheduler:
    """Poll on a fixed interval, independent of the ingestion scheduler switch."""

    def __init__(self, controller: PublishAlertController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = max(15, int(interval_seconds))
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.controller.config.publish_alert_configured:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop, name="publish-alert-scheduler", daemon=True
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
