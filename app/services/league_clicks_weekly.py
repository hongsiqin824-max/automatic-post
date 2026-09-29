"""联赛点击数据周报：每周六自动汇总前 7 天数据并写入飞书表格。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect


BEIJING = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


class FeishuAPIError(Exception):
    """飞书 API 调用失败。"""


def get_tenant_access_token(app_id: str, app_secret: str, timeout: int = 10) -> str:
    """
    获取飞书 tenant_access_token。

    文档: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
    """
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": app_id, "app_secret": app_secret}

    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise FeishuAPIError(f"获取 token 失败: {data.get('msg')}")

    return data["tenant_access_token"]


def create_sheet(
    spreadsheet_token: str,
    sheet_title: str,
    access_token: str,
    timeout: int = 10,
) -> str:
    """
    在飞书表格中创建一个新的 sheet。

    文档: https://open.feishu.cn/document/server-docs/docs/sheets-v3/spreadsheet-sheet/batch-operation

    返回新创建的 sheet_id。
    """
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": sheet_title,
                        "index": 0,  # 插入到最前面
                    }
                }
            }
        ]
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise FeishuAPIError(f"创建 sheet 失败: {data.get('msg')}")

    replies = data.get("data", {}).get("replies", [])
    if not replies or "addSheet" not in replies[0]:
        raise FeishuAPIError("创建 sheet 响应格式不符合预期")

    sheet_id = replies[0]["addSheet"]["properties"]["sheetId"]
    return sheet_id


def write_sheet_data(
    spreadsheet_token: str,
    sheet_id: str,
    values: list[list[Any]],
    access_token: str,
    timeout: int = 10,
) -> None:
    """
    向飞书表格的指定 sheet 写入数据。

    文档: https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/write-data
    """
    if not values:
        return

    rows = len(values)
    cols = max(len(row) for row in values) if values else 0

    # 将列索引转换为字母（A, B, C, ...）
    def col_letter(n: int) -> str:
        result = ""
        while n >= 0:
            result = chr(65 + n % 26) + result
            n = n // 26 - 1
        return result

    end_col = col_letter(cols - 1)
    range_notation = f"{sheet_id}!A1:{end_col}{rows}"

    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "valueRanges": [
            {
                "range": range_notation,
                "values": values,
            }
        ]
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise FeishuAPIError(f"写入数据失败: {data.get('msg')}")


def build_weekly_report_data(
    start_date: str,  # YYYY-MM-DD
    end_date: str,    # YYYY-MM-DD
    connection: sqlite3.Connection,
    *,
    boundary_hour: int = 19,
) -> list[list[Any]]:
    """
    构造周报表格数据：每个栏目一行，列为区间内每一天的直接发布篇数 + 合计。

    数据源与每天 19:00 推送到飞书群的「直接发布统计」完全一致，都走
    ``repo.direct_publish_report``，只是这里按天各查一次再横向铺开。
    每一天的统计窗口沿用群消息的口径：前一天 boundary_hour 到当天
    boundary_hour（北京时间），右开区间，所以两个相邻日期不会重复计数。

    行只包含 ``repo.REPORT_TAB_NAMES`` 里的栏目（与群消息口径一致），
    顺序固定，某天没有发布就填 0。

    返回格式:
    [
        ["栏目", "2026-09-22", ..., "2026-09-28", "合计"],
        ["国家队", 195, 201, 197, 302, 190, 182, 233, 1500],
        ...
        ["总计", 369, 401, 341, 459, 289, 307, 364, 2530],
    ]
    """
    dates: list[str] = []
    cursor = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    while cursor <= last:
        dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    def window_end(day: date) -> str:
        """当天统计窗口的结束时刻（北京 boundary_hour）转成 UTC 文本。"""
        moment = datetime.combine(day, time(hour=boundary_hour), tzinfo=BEIJING)
        return (
            moment.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    # 每个栏目一行，按 REPORT_TAB_NAMES 的固定顺序，值按日期列对齐
    tab_counts: dict[str, list[int]] = {
        name: [0] * len(dates) for name in repo.REPORT_TAB_NAMES
    }
    total_by_date = [0] * len(dates)

    for idx, value in enumerate(dates):
        day = date.fromisoformat(value)
        report = repo.direct_publish_report(
            window_end(day - timedelta(days=1)),
            window_end(day),
            connection,
        )
        # article_count 是去重后的篇数，栏目明细之和会偏大（一篇可进多个栏目），
        # 所以总计行直接用它，而不是把下面各行加起来。
        total_by_date[idx] = int(report.get("article_count") or 0)
        for item in report.get("tab_counts") or []:
            name = str(item.get("name") or "")
            if name in tab_counts:
                tab_counts[name][idx] = int(item.get("count") or 0)

    rows_out: list[list[Any]] = [["栏目", *dates, "合计"]]
    for name in repo.REPORT_TAB_NAMES:
        counts = tab_counts[name]
        rows_out.append([name, *counts, sum(counts)])

    rows_out.append(["总计", *total_by_date, sum(total_by_date)])

    return rows_out


def latest_due_saturday(now: datetime, *, hour: int, minute: int) -> date:
    """返回最近一个「已经到点」的周六（北京时间）。

    只看「今天是不是周六」会让服务在周六 19:00 之后启动时整周漏发，所以这里
    改成回溯：找到最近一个已过 hour:minute 的周六。周六当天还没到点时，回退
    到上一个周六，这样重启也能把上一期补上；配合 last_sent 去重不会重复发。
    """
    local = now.astimezone(BEIJING)
    # 周六 weekday()==5，往前回溯到最近的周六
    candidate = local.date() - timedelta(days=(local.weekday() - 5) % 7)
    boundary = datetime.combine(
        candidate, time(hour=hour, minute=minute), tzinfo=BEIJING
    )
    if local < boundary:
        candidate -= timedelta(days=7)
    return candidate


def send_weekly_report(
    config: AppConfig,
    connection: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    每周六晚上 7 点发送周报到飞书表格。

    返回字典包含:
    - sent: 是否发送
    - reason: 未发送的原因（如果未发送）
    - sheet_id: 新创建的 sheet ID（如果发送成功）
    - sheet_title: sheet 标题
    - error: 错误信息（如果发送失败）
    """
    if not config.feishu_spreadsheet_configured:
        return {"sent": False, "reason": "feishu_spreadsheet_not_configured"}

    if now is None:
        now = datetime.now(BEIJING)

    # 最近一个已到点的周六。服务在周六 19:00 之后才启动（或之后重启）时，
    # 这里仍然指向那个周六，所以这一期不会被跳过。
    report_saturday = latest_due_saturday(
        now,
        hour=config.league_clicks_weekly_hour,
        minute=config.league_clicks_weekly_minute,
    )

    # 按「该周六」去重，而不是按当天日期：否则周日补发时 today_str 变了，
    # 会把同一期再发一遍。
    period_key = report_saturday.isoformat()
    last_sent = repo.get_setting("league_clicks_weekly_last_sent", "", connection)
    if last_sent == period_key:
        return {"sent": False, "reason": "already_sent", "period": period_key}

    # 统计区间以该周六收尾（含当天），共 7 天：上周日 ~ 本周六。
    # 周六那一列对应的窗口正是群消息在周六 19:00 发出的那一期。
    end_date = report_saturday
    start_date = end_date - timedelta(days=6)

    sheet_title = f"{start_date.isoformat()} ~ {end_date.isoformat()}"

    try:
        # 1. 获取 access token
        access_token = get_tenant_access_token(
            config.feishu_app_id,
            config.feishu_app_secret,
            timeout=config.feishu_api_timeout_seconds,
        )

        # 2. 创建新 sheet
        sheet_id = create_sheet(
            config.feishu_spreadsheet_token,
            sheet_title,
            access_token,
            timeout=config.feishu_api_timeout_seconds,
        )

        # 3. 构造数据
        from ..db import get_db
        conn = connection or get_db()
        data = build_weekly_report_data(
            start_date.isoformat(),
            end_date.isoformat(),
            conn,
        )

        # 4. 写入数据
        write_sheet_data(
            config.feishu_spreadsheet_token,
            sheet_id,
            data,
            access_token,
            timeout=config.feishu_api_timeout_seconds,
        )

        # 5. 记录已发送
        repo.set_setting("league_clicks_weekly_last_sent", period_key, conn)

        logger.info(f"周报已发送到飞书表格: {sheet_title}, sheet_id={sheet_id}")

        return {
            "sent": True,
            "sheet_id": sheet_id,
            "sheet_title": sheet_title,
            "period": period_key,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    except Exception as e:
        logger.exception("发送周报失败")
        return {
            "sent": False,
            "reason": "send_failed",
            "error": str(e),
        }


class LeagueClicksWeeklyController:
    """联赛点击数据周报控制器：后台线程定期检查并发送。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> bool:
        """启动后台线程。如果已在运行则返回 False。"""
        if self._thread is not None and self._thread.is_alive():
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="league-clicks-weekly")
        self._thread.start()
        logger.info("联赛点击数据周报控制器已启动")
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        """停止后台线程。"""
        if self._thread is None or not self._thread.is_alive():
            return True

        self._stop_event.set()
        self._thread.join(timeout=timeout)
        is_stopped = not self._thread.is_alive()
        if is_stopped:
            logger.info("联赛点击数据周报控制器已停止")
        return is_stopped

    def _run(self) -> None:
        """后台线程的主循环：定期检查并发送周报。

        线程里没有 Flask app context，所以连接必须在这里自己开好传下去，
        不能让 send_weekly_report 回落到依赖 ``g`` 的 get_db()。
        """
        while not self._stop_event.is_set():
            try:
                connection = _connect(self.config.database_path)
                try:
                    result = send_weekly_report(self.config, connection)
                    if result["sent"]:
                        logger.info(f"联赛点击数据周报发送成功: {result}")
                finally:
                    connection.close()
            except Exception:
                logger.exception("联赛点击数据周报循环出错")

            # 休眠到下一次检查
            self._stop_event.wait(self.config.league_clicks_weekly_check_interval_seconds)


class LeagueClicksWeeklyScheduler:
    """调度器包装器，用于与 Flask 应用集成。"""

    def __init__(self, controller: LeagueClicksWeeklyController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = interval_seconds
        self._started = False

    def start(self) -> bool:
        """启动调度器。"""
        if self._started:
            return False
        self._started = self.controller.start()
        return self._started

    def shutdown(self, timeout: float = 5.0) -> bool:
        """停止调度器。"""
        return self.controller.shutdown(timeout=timeout)
