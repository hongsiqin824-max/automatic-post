"""来源抓取量预警：按期统计并推送到飞书群机器人。

与 :mod:`app.services.feishu_report`（直接发布统计）是两条独立链路，各自的
webhook、投递记录表和调度线程都不共用。这条链路一天可以有多期：边界列表既
决定发送时刻，也决定统计区间怎么切分。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import requests

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect
from .source_monitor import compare_periods, detect_anomalies, generate_summary


logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
DELIVERY_TABLE = "source_report_deliveries"


class SourceReportResultUnknown(RuntimeError):
    """The request may have reached Feishu, so automatic retry is unsafe."""


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def beijing_label(value: str, *, fmt: str = "%m-%d %H:%M") -> str:
    return _parse_utc(value).astimezone(BEIJING).strftime(fmt)


def boundary_moments(
    local_date: Any,
    boundaries: Sequence[tuple[int, int]],
) -> list[datetime]:
    """Return one Beijing datetime per boundary on *local_date*, ascending."""

    return sorted(
        datetime.combine(local_date, time(hour=hour, minute=minute), tzinfo=BEIJING)
        for hour, minute in boundaries
    )


def report_period(
    now: datetime | None,
    boundaries: Sequence[tuple[int, int]],
) -> tuple[str, str] | None:
    """Return the most recently completed period as half-open UTC bounds.

    A period runs from the previous boundary to the one that just passed, so
    consecutive reports tile the day without gaps or double counting. With
    boundaries at 11:00 and 19:00 the periods are 19:00→11:00 (16h, overnight)
    and 11:00→19:00 (8h, afternoon).

    Returns ``None`` when no boundary has passed yet, which only happens before
    the first boundary of the very first day the feature is switched on.
    """

    if not boundaries:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(BEIJING)
    # 取昨天和今天的全部边界即可：最近一个已过的边界必定在今天，它的上一个边界
    # 最远也只是昨天最后一个边界。
    moments = boundary_moments(local.date() - timedelta(days=1), boundaries)
    moments += boundary_moments(local.date(), boundaries)
    passed = [moment for moment in moments if moment <= local]
    if len(passed) < 2:
        return None
    return _utc_text(passed[-2]), _utc_text(passed[-1])


def period_hours(period_start: str, period_end: str) -> float:
    delta = _parse_utc(period_end) - _parse_utc(period_start)
    return max(0.0, delta.total_seconds() / 3600.0)


def baseline_period_end(period_end: str) -> str:
    """The same boundary 24h earlier — the period this one is compared against."""

    return _utc_text(_parse_utc(period_end) - timedelta(days=1))


def _source_label(item: Any) -> str:
    """来源名 + code。

    库里有三个来源都叫「韩媒」、两个都叫「日媒」，只报显示名的话看到预警也不知
    道该去修哪一个。code 才是运维实际操作的标识。
    """

    name = str((item or {}).get("source_name") or "").strip()
    code = str((item or {}).get("source_code") or "").strip()
    if not name or name == code:
        return code or "未知来源"
    return f"{name}({code})"


def build_report_text(
    period_start: str,
    period_end: str,
    summary: dict[str, Any],
    anomalies: dict[str, list[dict[str, Any]]],
    *,
    disabled_sources: int = 0,
) -> str:
    """Render one period's alert message.

    Every time word is derived from the period bounds rather than written as
    「今日」/「昨日」: with more than one period a day those words would be wrong
    half the time, and two same-day messages would be indistinguishable.
    """

    hours = period_hours(period_start, period_end)
    lines = [
        f"📊 来源抓取预警 · {beijing_label(period_end)}",
        f"统计区间：{beijing_label(period_start)} → {beijing_label(period_end)}"
        f"（北京时间，{hours:.0f}小时）",
        "",
        "【本期概况】",
        f"总文章数：{int(summary.get('total_articles') or 0)}篇"
        f" ({summary.get('total_change') or '-'})",
        f"已发布：{int(summary.get('total_published') or 0)}篇"
        f" ({summary.get('published_rate') or 0}%)",
        f"待处理：{int(summary.get('total_draft') or 0)}篇"
        f" ({summary.get('draft_rate') or 0}%)",
        f"启用来源：{int(summary.get('active_sources') or 0)}个",
    ]

    for key, heading in (
        ("critical", "🚨 严重异常"),
        ("warning", "⚠️ 需要关注"),
        ("info", "📈 信息提示"),
    ):
        items = anomalies.get(key) or []
        if not items:
            continue
        lines += ["", f"{heading}（{len(items)}个）"]
        for index, item in enumerate(items, start=1):
            current = int((item.get("current") or {}).get("total_count") or 0)
            baseline = int((item.get("baseline") or {}).get("total_count") or 0)
            change = (item.get("change") or {}).get("display") or "-"
            lines.append(f"{index}. {_source_label(item)}")
            lines.append(f"   本期：{current}篇 | 昨日同期：{baseline}篇 ({change})")
            if item.get("anomaly_message"):
                lines.append(f"   {item['anomaly_message']}")

    normal = anomalies.get("normal") or []
    if normal:
        # 正常来源一个都不折叠：判断某个来源是不是出问题，靠的就是看到它本期的
        # 篇数和环比，被省略掉的来源等于没监控。三十来个来源的全量清单也就一千
        # 多字节，离飞书文本消息的上限差得远。
        lines += ["", f"📋 正常（共{len(normal)}个）"]
        for index, item in enumerate(normal, start=1):
            current = int((item.get("current") or {}).get("total_count") or 0)
            change = (item.get("change") or {}).get("display") or "-"
            lines.append(f"{index}. {_source_label(item)} - {current}篇 ({change})")

    if not (anomalies.get("critical") or anomalies.get("warning")):
        lines += ["", "✅ 本期无异常"]
    if not any(
        (item.get("change") or {}).get("type") != "no_baseline"
        for group in anomalies.values()
        for item in group
    ):
        # 首期（或中断后第一期）没有昨日同期快照，本期数字照旧有用，但任何环比
        # 判断都还谈不上。说清楚这一点，免得被当成「一切正常」。
        lines += ["ℹ️ 本期无昨日同期数据，仅记录现状，下一期起开始环比"]
    if disabled_sources:
        # 停用来源不参与统计，但数量要报出来：否则来源列表变短时，看不出是被
        # 关掉了还是抓取挂了。
        lines += [f"（另有 {disabled_sources} 个来源已停用，不计入统计）"]
    return "\n".join(lines)


def send_source_report(config: AppConfig, text: str) -> dict[str, Any]:
    """Send plain text to the alert bot and validate the bot response."""

    if not config.source_report_webhook_url:
        raise ValueError("未配置 SOURCE_REPORT_WEBHOOK_URL")
    try:
        response = requests.post(
            config.source_report_webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=config.source_report_timeout_seconds,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise SourceReportResultUnknown(f"飞书请求结果未知: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"飞书机器人 HTTP {response.status_code}: {payload}")
    if not isinstance(payload, dict):
        raise SourceReportResultUnknown("飞书请求已完成，但返回结果无法识别")
    if "code" in payload:
        successful = payload.get("code") == 0
    elif "StatusCode" in payload:
        successful = payload.get("StatusCode") == 0
    else:
        raise SourceReportResultUnknown(f"飞书请求已完成，但返回结果无法识别: {payload}")
    if not successful:
        raise RuntimeError(f"飞书机器人返回失败: {payload}")
    return payload


def send_due_source_report(
    config: AppConfig,
    connection=None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send the latest completed period once; failures stay retryable.

    Only the most recent period is ever sent. Unlike the publish report there
    is no catch-up walk: an alert about a window that closed two days ago tells
    nobody anything useful, and replaying them would flood the group on every
    restart after an outage.
    """

    if not config.source_report_configured:
        return {"sent": False, "skipped": True, "reason": "source_report_not_configured"}
    owns_connection = connection is None
    conn = connection or _connect(config.database_path)
    try:
        period = report_period(now, config.source_report_boundaries)
        if period is None:
            return {"sent": False, "skipped": True, "reason": "no_completed_period"}
        period_start, period_end = period

        # 已发出的期在这里就返回：一个八小时的期里，30 秒一轮的轮询绝大多数都落在
        # 这个分支。放到抢占之后才判断的话，每一轮都要先对全量文章做一次聚合、再覆盖
        # 写一遍快照，而这库用的是 rollback journal，写事务会挡住抓取管线。
        if repo.source_report_delivery_sent(period_start, period_end, conn):
            return {"sent": False, "skipped": True, "reason": "already_sent", "period": period}

        stats = repo.source_period_stats(period_start, period_end, conn)
        # 快照先落库再抢占：环比基准读的是快照，发送失败也不该让明天丢掉基准。
        repo.save_source_period_snapshot(period_start, period_end, stats, conn)

        delivery = repo.claim_source_report_delivery(
            period_start,
            period_end,
            conn,
            retry_after_seconds=config.source_report_retry_seconds,
            stale_after_seconds=config.source_report_stale_seconds,
        )
        if delivery is None:
            return {
                "sent": False,
                "skipped": True,
                "reason": "already_sent_or_retry_not_due",
                "period": period,
            }

        baseline = repo.get_source_period_snapshot(baseline_period_end(period_end), conn)
        comparison = compare_periods(stats, baseline)
        anomalies = detect_anomalies(
            comparison, period_hours=period_hours(period_start, period_end)
        )
        summary = generate_summary(comparison)
        message = build_report_text(
            period_start,
            period_end,
            summary,
            anomalies,
            disabled_sources=repo.count_disabled_sources(conn),
        )

        try:
            response = send_source_report(config, message)
        except SourceReportResultUnknown as exc:
            repo.mark_source_report_delivery_unknown(
                int(delivery["id"]), str(delivery["claim_token"]), str(exc), conn
            )
            logger.error("来源预警发送结果未知，不自动重试 period=%s: %s", period, exc)
            return {"sent": False, "unknown": True, "error": str(exc)[:500], "period": period}
        except Exception as exc:  # noqa: BLE001 - explicit failures remain retryable
            repo.fail_source_report_delivery(
                int(delivery["id"]),
                str(delivery["claim_token"]),
                str(exc),
                conn,
                retry_after_seconds=config.source_report_retry_seconds,
            )
            logger.warning("来源预警发送失败 period=%s: %s", period, exc)
            return {"sent": False, "failed": True, "error": str(exc)[:500], "period": period}

        recorded = repo.finish_source_report_delivery(
            int(delivery["id"]), str(delivery["claim_token"]), conn, response=response
        )
        if not recorded:
            raise RuntimeError("飞书消息已发送，但本地发送记录写入失败")
        return {
            "sent": True,
            "period": period,
            "summary": summary,
            "anomalies": anomalies,
            "message": message,
        }
    finally:
        if owns_connection:
            conn.close()


class SourceReportController:
    """Run one due-alert check in a background worker."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> bool:
        if not self.config.source_report_configured:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(target=self._run, name="source-report-worker", daemon=True)
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
            send_due_source_report(self.config, conn)
        except Exception:  # noqa: BLE001 - keep future scheduler ticks alive
            logger.exception("来源预警任务失败")
        finally:
            conn.close()
            with self._lock:
                self._running = False


class SourceReportScheduler:
    """Poll for a due period independently of the ingestion scheduler switch."""

    def __init__(self, controller: SourceReportController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = max(15, int(interval_seconds))
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.controller.config.source_report_configured:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop, name="source-report-scheduler", daemon=True
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
