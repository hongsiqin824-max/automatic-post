"""联赛 tab 点击数据：每天从 StarRocks 拉前一天的结果，存进本地累积表。

为什么要本地累积（2026-09-24 实测）：
  上游 sr_prod.dwd.dwd_flow_sensor_s 只保留约 30~34 天就滚动删除
  （30 天前的窗口还有 173 万条，35 天前直接 0 条）。看板要看更长周期的
  变化，只能每天把当日结果抄一份存下来。漏掉的天在 30 天内还能补，
  超过就永久丢失了。

口径（同样是实测核实的，改之前先看清楚）：
  只统计 $app_id = com.data4sports.app，即「懂球帝Pro iOS 版」。
  安卓/鸿蒙主 App 把字段名拼成了 thrid_tab_id 且值为空、也不上报
  third_tab_name；iOS 主 App 压根不上报 tab_switch 事件。所以目前
  只有 Pro iOS 版拿得到联赛 tab 名。埋点 2026-09-23 才开始有数据。

性能约束：
  sr_prod 是 JDBC catalog，查询会下推到后端。跨天范围查询会被后端掐断
  （7 天范围实测 51 秒就报错），所以只能按天逐日查。proc_time 必须带
  范围否则走不动；properties 是超大 VARCHAR，先用 LIKE 粗筛再解析 JSON
  能把单日耗时从 ~160s 压到 ~70s。
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect


logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")

# StarRocks 服务器对本部署是固定的，与 dqd-bigdata-sr 技能保持一致
DEFAULT_HOST = "172.21.0.148"
DEFAULT_PORT = 9030
# 技能已经把账号密码配在这里了，没配项目级环境变量时直接复用
SKILL_ENV_PATH = Path.home() / ".trae-cn" / "skills" / "dqd-bigdata-sr" / ".env"

# 埋点上线日，再往前查也是 0 条，白等一分钟
EARLIEST_DAY = date(2026, 9, 23)
# 懂球帝Pro iOS 版
PRO_IOS_APP_ID = "com.data4sports.app"
TARGET_TAB_NAME = "头条"
TARGET_SUB_TAB_NAME = "彩经"
# 单日查询要 70~120 秒，一次跑太多天会让后台线程挂很久
MAX_DAYS_PER_RUN = 5
# 拉取时间距统计日不足这么多天，就认为还可能有延迟入库的事件，过几天再拉一次覆盖
FINALIZE_LAG_DAYS = 2

DAILY_SQL = f"""
SELECT get_json_string(properties, 'third_tab_name') AS third_tab_name,
       count(*)                                      AS switch_cnt
FROM sr_prod.dwd.dwd_flow_sensor_s
WHERE proc_time >= %s AND proc_time < %s
  AND event = 'tab_switch'
  AND properties LIKE '%%{TARGET_SUB_TAB_NAME}%%'
  AND get_json_string(properties, '$.$app_id') = '{PRO_IOS_APP_ID}'
  AND get_json_string(properties, 'tab_name') = '{TARGET_TAB_NAME}'
  AND get_json_string(properties, 'sub_tab_name') = '{TARGET_SUB_TAB_NAME}'
  AND get_json_string(properties, 'third_tab_name') IS NOT NULL
GROUP BY 1
ORDER BY switch_cnt DESC
"""


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析，只够读技能配好的那几个键。"""
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


def resolve_credentials(config: AppConfig) -> dict[str, Any]:
    """项目级环境变量优先，缺失的回退到技能的 .env。"""
    fallback = _parse_env_file(SKILL_ENV_PATH)
    return {
        "host": config.starrocks_host or fallback.get("STARROCKS_HOST", "") or DEFAULT_HOST,
        "port": config.starrocks_port or int(fallback.get("STARROCKS_PORT") or DEFAULT_PORT),
        "user": config.starrocks_user or fallback.get("STARROCKS_USER", ""),
        "password": config.starrocks_password or fallback.get("STARROCKS_PASSWORD", ""),
    }


def credentials_configured(config: AppConfig) -> bool:
    return bool(resolve_credentials(config).get("user"))


def _connect_starrocks(config: AppConfig):
    import pymysql  # 延迟导入：没装 PyMySQL 也不该拖垮整个应用启动

    creds = resolve_credentials(config)
    timeout = config.league_clicks_query_timeout_seconds
    return pymysql.connect(
        host=creds["host"],
        port=int(creds["port"]),
        user=creds["user"],
        password=creds["password"],
        charset="utf8mb4",
        connect_timeout=15,
        read_timeout=timeout + 30,
        init_command=f"SET query_timeout = {int(timeout)}",
    )


def fetch_day(config: AppConfig, day: date) -> list[tuple[str, int]]:
    """拉取某一天的联赛 tab 切换次数。按 proc_time（入库时间）单日切分。"""
    conn = _connect_starrocks(config)
    try:
        cursor = conn.cursor()
        cursor.execute(DAILY_SQL, (f"{day} 00:00:00", f"{day + timedelta(days=1)} 00:00:00"))
        return [(str(name), int(cnt)) for name, cnt in cursor.fetchall() if name]
    finally:
        conn.close()


def pending_dates(config: AppConfig, connection=None, now: datetime | None = None) -> list[date]:
    """算出还需要拉哪几天，按日期从早到晚。

    「昨天」要等今天的配置时刻过了才拉，避免白天业务高峰期跑重查询；更早的
    历史缺口不受时刻限制，随时可补（开机后就能自动追平）。
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(BEIJING)
    boundary = time(hour=config.league_clicks_hour, minute=config.league_clicks_minute)

    yesterday = local.date() - timedelta(days=1)
    # 今天的时刻还没到，就先不碰昨天的数据
    latest = yesterday if local.time() >= boundary else yesterday - timedelta(days=1)
    earliest = max(EARLIEST_DAY, local.date() - timedelta(days=config.league_clicks_backfill_days))
    if latest < earliest:
        return []

    runs = repo.list_league_click_runs(connection, since=earliest.isoformat())
    today = local.date()
    result: list[date] = []
    day = earliest
    while day <= latest:
        run = runs.get(day.isoformat())
        if run is None or str(run.get("status", "")).upper() != "SUCCESS":
            result.append(day)
        else:
            # 拉得太早可能漏了延迟入库的事件，过 FINALIZE_LAG_DAYS 天再覆盖一次。
            # 必须同时要求「上次拉取不是今天」，否则刚拉完就又满足重拉条件，
            # 轮询会把同一天反复拉个不停。
            fetched_day = _parse_day(str(run.get("fetched_at") or "")[:10])
            if (fetched_day - day).days < FINALIZE_LAG_DAYS and fetched_day < today:
                result.append(day)
        day += timedelta(days=1)
    return result


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        # 解析不出来就当作很久以前，让它走重拉逻辑而不是被永久跳过
        return EARLIEST_DAY


def sync_due_days(config: AppConfig, connection=None, now: datetime | None = None) -> dict:
    """把所有待拉取的日期补齐，返回本次执行摘要。"""
    if not config.league_clicks_enabled:
        return {"ran": False, "reason": "disabled"}
    if not credentials_configured(config):
        return {"ran": False, "reason": "starrocks_not_configured"}

    days = pending_dates(config, connection, now)
    if not days:
        return {"ran": False, "reason": "up_to_date"}

    # 一次只跑几天，剩下的留给下一轮，免得后台线程挂太久
    days = days[:MAX_DAYS_PER_RUN]
    synced: list[str] = []
    failed: list[str] = []
    for day in days:
        try:
            rows = fetch_day(config, day)
        except Exception as exc:  # noqa: BLE001 - 单日失败不该影响其他日期
            logger.exception("联赛tab点击数据拉取失败: %s", day)
            repo.mark_league_tab_click_run(
                day.isoformat(), connection, status="FAILED", error=str(exc)[:500]
            )
            failed.append(day.isoformat())
            continue
        repo.save_league_tab_clicks(day.isoformat(), rows, connection)
        repo.mark_league_tab_click_run(
            day.isoformat(), connection, status="SUCCESS",
            row_count=len(rows), total_cnt=sum(cnt for _, cnt in rows),
        )
        synced.append(day.isoformat())

    return {
        "ran": True,
        "synced": synced,
        "failed": failed,
        "remaining": max(0, len(pending_dates(config, connection, now))),
    }


class LeagueClicksController:
    """在后台线程里跑一次补数检查。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict | None = None

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    def start(self) -> bool:
        if not self.config.league_clicks_enabled:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(target=self._run, name="league-clicks-worker", daemon=True)
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
            self._last_result = sync_due_days(self.config, conn)
        except Exception:  # noqa: BLE001 - keep future scheduler ticks alive
            logger.exception("联赛tab点击数据同步任务失败")
        finally:
            conn.close()
            with self._lock:
                self._running = False


class LeagueClicksScheduler:
    """独立于抓取调度开关运行，轮询判断是否有待补的日期。

    幂等不靠内存计时器，而靠 league_tab_click_runs 表：没有待补日期时
    只查一次本地 SQLite 就返回，开销可以忽略。所以进程重启、机器睡眠
    都不会漏跑或重复跑。
    """

    def __init__(self, controller: LeagueClicksController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = max(15, int(interval_seconds))
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.controller.config.league_clicks_enabled:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="league-clicks-scheduler",
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
        # 启动就先跑一次，机器关机几天后开机能立刻开始追平
        self.controller.start()
        while not self._shutdown.wait(self.interval_seconds):
            self.controller.start()
