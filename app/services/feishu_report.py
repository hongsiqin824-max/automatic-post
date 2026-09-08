"""Daily direct-publish statistics and Feishu bot delivery."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect


logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")


class FeishuReportResultUnknown(RuntimeError):
    """The request may have reached Feishu, so automatic retry is unsafe."""


def report_period(now: datetime | None = None, *, hour: int = 19, minute: int = 0) -> tuple[str, str]:
    """Return the most recently completed Beijing report window in UTC.

    Before today's configured boundary, yesterday's boundary is used so a
    restarted service can catch up.  The half-open interval avoids counting an
    article exactly at the end boundary twice across consecutive reports.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(BEIJING)
    boundary = datetime.combine(
        local.date(), time(hour=max(0, min(23, int(hour))), minute=max(0, min(59, int(minute)))),
        tzinfo=BEIJING,
    )
    # Before today's boundary, yesterday's boundary is the latest completed
    # one.  This also lets a service restarted during the day catch up safely.
    period_end = boundary if local >= boundary else boundary - timedelta(days=1)
    period_start = boundary - timedelta(days=1)
    if period_end != boundary:
        period_start = period_end - timedelta(days=1)
    return (
        period_start.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        period_end.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )


def _today_boundary_reached(now: datetime | None, *, hour: int, minute: int) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(BEIJING)
    boundary = datetime.combine(local.date(), time(hour=hour, minute=minute), tzinfo=BEIJING)
    return local >= boundary


def _beijing_label(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")


def build_report_text(period_start: str, period_end: str, report: dict[str, Any]) -> str:
    lines = [
        "直接发布统计",
        f"统计周期：{_beijing_label(period_start)} 至 {_beijing_label(period_end)}（北京时间）",
        f"直接发布合计：{int(report.get('article_count') or 0)} 篇",
    ]
    tab_counts = report.get("tab_counts") or []
    if tab_counts:
        lines.append("栏目明细（多栏目文章按每个栏目分别计数）：")
        lines.extend(
            f"- {str(item.get('name') or '未配置栏目')}：{int(item.get('count') or 0)} 篇"
            for item in tab_counts
        )
    else:
        lines.append("栏目明细：无")
    return "\n".join(lines)


def send_feishu_report(config: AppConfig, text: str) -> dict[str, Any]:
    """Send plain text to a Feishu group bot and validate the bot response."""

    if not config.feishu_report_webhook_url:
        raise ValueError("未配置 FEISHU_REPORT_WEBHOOK_URL")
    try:
        response = requests.post(
            config.feishu_report_webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=config.feishu_report_timeout_seconds,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise FeishuReportResultUnknown(f"飞书请求结果未知: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"飞书机器人 HTTP {response.status_code}: {payload}")
    if not isinstance(payload, dict):
        raise FeishuReportResultUnknown("飞书请求已完成，但返回结果无法识别")
    if "code" in payload:
        successful = payload.get("code") == 0
    elif "StatusCode" in payload:
        successful = payload.get("StatusCode") == 0
    else:
        raise FeishuReportResultUnknown(f"飞书请求已完成，但返回结果无法识别: {payload}")
    if not successful:
        raise RuntimeError(f"飞书机器人返回失败: {payload}")
    return payload


def send_due_report(config: AppConfig, connection=None, *, now: datetime | None = None) -> dict[str, Any]:
    """Send the latest completed period once; failed attempts remain retryable."""

    if not config.feishu_report_configured:
        return {"sent": False, "skipped": True, "reason": "feishu_report_not_configured"}
    owns_connection = connection is None
    conn = connection or _connect(config.database_path)
    try:
        latest_period = report_period(
            now,
            hour=config.feishu_report_hour,
            minute=config.feishu_report_minute,
        )
        if (
            not repo.has_report_deliveries(conn)
            and not _today_boundary_reached(
                now,
                hour=config.feishu_report_hour,
                minute=config.feishu_report_minute,
            )
        ):
            return {"sent": False, "skipped": True, "reason": "first_period_not_due"}
        period = repo.next_report_period(latest_period[0], latest_period[1], conn)
        report = repo.direct_publish_report(period[0], period[1], conn)
        delivery = repo.claim_report_delivery(
            period[0],
            period[1],
            conn,
            retry_after_seconds=config.feishu_report_retry_seconds,
            stale_after_seconds=config.feishu_report_stale_seconds,
        )
        if delivery is None:
            return {"sent": False, "skipped": True, "reason": "already_sent_or_retry_not_due", "period": period}
        message = build_report_text(period[0], period[1], report)
        try:
            response = send_feishu_report(config, message)
        except FeishuReportResultUnknown as exc:
            repo.mark_report_delivery_unknown(
                int(delivery["id"]),
                str(delivery["claim_token"]),
                str(exc),
                conn,
            )
            logger.error("飞书统计发送结果未知，不自动重试 period=%s: %s", period, exc)
            return {"sent": False, "unknown": True, "error": str(exc)[:500], "period": period}
        except Exception as exc:  # noqa: BLE001 - explicit failures remain retryable
            repo.fail_report_delivery(
                int(delivery["id"]),
                str(delivery["claim_token"]),
                str(exc),
                conn,
                retry_after_seconds=config.feishu_report_retry_seconds,
            )
            logger.warning("飞书统计发送失败 period=%s: %s", period, exc)
            return {"sent": False, "failed": True, "error": str(exc)[:500], "period": period}
        recorded = repo.finish_report_delivery(
            int(delivery["id"]),
            str(delivery["claim_token"]),
            conn,
            response=response,
        )
        if not recorded:
            raise RuntimeError("飞书消息已发送，但本地发送记录写入失败")
        return {"sent": True, "period": period, "report": report, "message": message}
    finally:
        if owns_connection:
            conn.close()


class FeishuReportController:
    """Run one due-report check in a background worker."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> bool:
        if not self.config.feishu_report_configured:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(target=self._run, name="feishu-report-worker", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True

    def _run(self) -> None:
        conn = _connect(self.config.database_path)
        try:
            send_due_report(self.config, conn)
        except Exception:  # noqa: BLE001 - keep future scheduler ticks alive
            logger.exception("飞书统计任务失败")
        finally:
            conn.close()
            with self._lock:
                self._running = False


class FeishuReportScheduler:
    """Run report checks independently from the ingestion scheduler switch."""

    def __init__(self, controller: FeishuReportController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = max(15, int(interval_seconds))
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.controller.config.feishu_report_configured:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="feishu-report-scheduler",
                daemon=True,
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
