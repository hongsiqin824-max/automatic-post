"""按栏目的标签白名单：从 StarRocks 拉各联赛「进行中赛季」的球队/球员标签。

背景与口径（2026-10-16 实测核实，改动前务必看清）：
  - 文章 ``channels`` 里的数字 ID = dedecms 的「频道/标签」ID，存在
    ``ods.ods_mysql_web_new_dqd_dedecms_dqd_dedecms_channels``。
  - 该表 ``relate_type`` 区分 Team/Player；``relate_id`` 被服务端列脱敏、
    无法 JOIN，但 ``dqd_id`` 未脱敏且等于足球库的 ``teams.id`` / ``persons.id``，
    用它连。
  - 绝不用 ``status`` 过滤：在役球队在标签表里也可能 status=0。
  - 联赛锚点用 ``competitions.known_name``（中文名），已在 COMPETITION_ROUTING
    里固化成 competition_id，运行时不再按名字猜。

安全设计（方案 B 的命门）：每个 competition 独立拉取、独立校验、原子替换。
拉空、或与上一次成功相比骤降超过阈值，一律拒绝覆盖、保留上一版 good 数据，
绝不让白名单塌缩导致误删文章标签。连接方式与 league_clicks 完全一致。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect
from . import channel_filter
from .league_clicks import credentials_configured, resolve_credentials


logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")

# 足球库 / dedecms 标签表（全限定名），与 league_clicks 的 ODS 命名一致。
_T_SEASONS = "ods.ods_mysql_dqd_soccer_data_dqd_soccer_data_seasons"
_T_SEASON_TEAMS = "ods.ods_mysql_dqd_soccer_data_dqd_soccer_data_season_teams"
_T_TEAM_PERSONS = "ods.ods_mysql_dqd_soccer_data_dqd_soccer_data_team_current_persons"
_T_TEAMS = "ods.ods_mysql_dqd_soccer_data_dqd_soccer_data_teams"
_T_CHANNELS = "ods.ods_mysql_web_new_dqd_dedecms_dqd_dedecms_channels"

# 取已开始(start_date<=今天)的最近两个赛季，当季 + 上一季取并集。
# 只取当季会在赛季之交误删标签：降级队的赛季总结、转会动向稿还在进来，而它
# 已经不在新赛季 season_teams 里了。并集的代价只是多留十几个旧球队标签
# （赛季中期不会有人给它们错挂），比误删合法标签便宜得多。骤降保护拦不住
# 这种情况——队数不变只是换了队时，它不触发。
_SEASON_LIMIT = 2
# 盖过服务端 sql_select_limit 默认值（5000）造成的静默截断，见 _connect_starrocks。
_SELECT_LIMIT = 200000
_SEASON_SQL = (
    f"SELECT id FROM {_T_SEASONS} "
    "WHERE competition_id=%s AND start_date <= CURDATE() "
    f"ORDER BY start_date DESC LIMIT {_SEASON_LIMIT}"
)

# team_type='default' 排除同一赛事里混进来的女足队和资格赛「胜者待定」占位
# 记录（欧联当季实测：76 真实俱乐部 + 1 女足 + 19 条 "A/B" 占位）。占位标签
# 目前在文章里一次都没出现过，但它们是真实可挂的标签 ID，放进白名单就等于
# 承认它们合法；女足队更会和同名男队混淆。
_TEAM_TAGS_SQL = (
    f"SELECT DISTINCT c.id "
    f"FROM {_T_SEASON_TEAMS} st "
    f"JOIN {_T_TEAMS} t ON t.id = st.team_id "
    "    AND t.deleted_at IS NULL AND t.team_type='default' "
    f"JOIN {_T_CHANNELS} c ON c.dqd_id = t.id AND c.relate_type='Team' "
    "WHERE st.season_id IN %s"
)

_PLAYER_TAGS_SQL = (
    f"SELECT DISTINCT c.id "
    f"FROM {_T_SEASON_TEAMS} st "
    f"JOIN {_T_TEAMS} t ON t.id = st.team_id "
    "    AND t.deleted_at IS NULL AND t.team_type='default' "
    f"JOIN {_T_TEAM_PERSONS} tcp ON tcp.team_id = t.id "
    f"JOIN {_T_CHANNELS} c ON c.dqd_id = tcp.person_id AND c.relate_type='Player' "
    "WHERE st.season_id IN %s"
)

# 国家队栏目白名单：不走赛事/赛季管道（国家队没有 competition/season 概念），
# 直接按 teams.type='national' 取。按用户要求只认 type='national'、不限 team_type，
# 所以成年男足 + U23/U21/U19 青年队 + 女足国家队的标签都纳入白名单。
#
# 白名单的职责是「收敛」：国家队栏目的文章只保留这些国家队相关标签，删掉误打的
# 俱乐部/随机标签。静态黑名单仍在收敛之后照常生效（主流国家队球队标签等该删的
# 一律删），和其他联赛栏目处理逻辑完全一致，不做任何特殊豁免。
NATIONAL_TEAMS_COMPETITION_ID = -1  # 哨兵：复用 competition 存储/校验/审计管道
NATIONAL_TEAMS_TAB = "国家队"
# 国家队数据不来自 seasons，存储/审计表的 season_id 非空约束用哨兵 0 占位。
_NATIONAL_SEASON_SENTINEL = 0
_NATIONAL_TEAM_TAGS_SQL = (
    f"SELECT DISTINCT c.id "
    f"FROM {_T_TEAMS} t "
    f"JOIN {_T_CHANNELS} c ON c.dqd_id = t.id AND c.relate_type='Team' "
    "WHERE t.type='national' AND t.deleted_at IS NULL"
)
_NATIONAL_PLAYER_TAGS_SQL = (
    f"SELECT DISTINCT c.id "
    f"FROM {_T_TEAMS} t "
    f"JOIN {_T_TEAM_PERSONS} tcp ON tcp.team_id = t.id "
    f"JOIN {_T_CHANNELS} c ON c.dqd_id = tcp.person_id AND c.relate_type='Player' "
    "WHERE t.type='national' AND t.deleted_at IS NULL"
)

# 第一期：按栏目白名单的 competition 路由（competition_id, 栏目名）。
# 栏目名用于运行时在 tabs 表里解析 backend_tab_id；栏目若不存在则跳过。
# 五大联赛/中超属于「全局黑名单」阵营，不在这里（它们的标签在任何文章里都删，
# 由现有黑名单处理）。competition_id 全部已用已知数据反查核实。
#
# 只放当前启用的栏目。未启用栏目的 competition_id 尚未逐个核实，而这个库里
# 同名赛事很多（known_name 重复、女足/青年/杯赛混在一起），配错的后果是给错
# 栏目发白名单、误删合法标签。要启用某个栏目时，先用它的历史文章标签反查核实
# competition_id，再把对应行从下面的待核实清单搬上来。
COMPETITION_ROUTING: tuple[tuple[int, str], ...] = (
    (257, "澳超"),
    (97, "日职联"),
    (98, "日职乙"),
    (7, "德乙"),
    (22, "挪超"),
    (21, "瑞典超"),
    (14, "欧联"),
    (117, "韩K"),
    (226, "亚冠精英"),
    (19, "巴甲"),
    (26, "美职联"),
)

# 待核实：对应栏目当前未启用，competition_id 未经文章标签反查验证，暂不生效。
# 其中「欧冠」还需先决定归属——整联赛屏蔽的话它属于 BLACKLIST_COMPETITIONS，
# 不该在这份按栏目白名单里。
_UNVERIFIED_COMPETITION_ROUTING: tuple[tuple[int, str], ...] = (
    (15, "土超"),
    (137, "墨甲"),
    (52, "葡超"),
    (1, "荷甲"),
    (59, "英冠"),
    (36, "苏超"),
    (197, "沙特联"),
    (6, "欧冠"),
    (3904, "欧协联"),
    (258, "世俱杯"),
    (981, "韩K2联"),
    (228, "亚冠2"),
    (554, "印尼超"),
    (80, "哥伦甲"),
)

# 第二期：并入动态黑名单的「整联赛」competition。它们的球队/球员标签在任何文章
# 里都删（叠加在静态 BLOCKED_CHANNEL_IDS 之上），替换两个生成文件里会随赛季/转会
# 变动的部分，保持新鲜。名宿退役、额外8队、通用后台、国家队标签仍由静态常量兜底
# （非整联赛、无法按赛季刷新）。第二个元素仅用于日志可读。
BLACKLIST_COMPETITIONS: tuple[tuple[int, str], ...] = (
    (4, "英超"),
    (3, "西甲"),
    (5, "德甲"),
    (9, "意甲"),
    (12, "法甲"),
    (43, "中超"),
    (129, "中甲"),
    (1535, "中乙"),
)


# 白名单侧全部路由，目前只含已核实的联赛栏目（COMPETITION_ROUTING）。
#
# 【国家队栏目为何退出白名单——前因后果，勿轻易加回】
# 曾把国家队（哨兵 -1、teams.type='national'）并进本路由，给「国家队」栏目做标签
# 收敛。但对生产库全量 6782 篇国家队文章实测评估后停用，原因：
#   1. 国家队新闻天然牵涉球员所属俱乐部，而白名单是「正向枚举、只留国家队标签」，
#      会把这些合理的俱乐部标签误删——评估中 49% 的文章被波及、16% 被收敛到空；
#      被删的 5109 个标签里 88% 是俱乐部、12% 是球员（含严智星等真国脚，因国家队
#      大名单数据滞后落在白名单外），0 个是真正的随机垃圾标签。
#   2. 方向本就不对：俱乐部遍布全球所有联赛，无法用正向枚举白名单穷举。
# 结论：国家队栏目改为「只走黑名单」（与头条/快讯等非联赛栏目一致），俱乐部/国家队
# 标签都保留，黑名单仍照常删主流国家队/五大联赛等标签。下方 fetch_national_teams 与
# NATIONAL_TEAMS_* 常量保留仅供追溯，当前不再被调用。
_WHITELIST_ROUTING: tuple[tuple[int, str], ...] = (
    *COMPETITION_ROUTING,
)


def _all_competition_ids() -> list[int]:
    """白名单 + 黑名单需要拉取的全部 competition（去重、保序）。"""
    ordered: list[int] = []
    seen: set[int] = set()
    for comp_id, _ in (*_WHITELIST_ROUTING, *BLACKLIST_COMPETITIONS):
        cid = int(comp_id)
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    return ordered

_LAST_SYNC_SETTING = "competition_tags_last_sync_date"


def _connect_starrocks(config: AppConfig):
    import pymysql  # 延迟导入：没装 PyMySQL 也不该拖垮应用启动

    creds = resolve_credentials(config)
    timeout = int(config.competition_tags_query_timeout_seconds)
    return pymysql.connect(
        host=creds["host"],
        port=int(creds["port"]),
        user=creds["user"],
        password=creds["password"],
        charset="utf8mb4",
        connect_timeout=15,
        read_timeout=timeout + 30,
        # sql_select_limit 服务端默认 5000，会静默截断结果集（欧联两季人员标签
        # 实测 5720 个，只拿回 5000）。白名单被截断就等于误删尾部标签，所以显式
        # 放开。这些查询返回的是去重后的标签 ID，量级在万以内，不会打爆内存。
        init_command=(
            f"SET query_timeout = {timeout}, sql_select_limit = {_SELECT_LIMIT}"
        ),
    )


def fetch_competition(config: AppConfig, competition_id: int) -> dict:
    """拉取某 competition 当季 + 上一季（并集）的球队/球员标签。

    返回 ``{"season_id", "season_ids", "team_ids", "player_ids"}``；无已开赛的
    赛季时 season_id 为 None、列表为空。``season_id`` 是其中最新的那个，只用于
    审计记录（表里一个 competition 一行，存最新赛季便于排查）。
    只读查询，不写任何东西。
    """
    conn = _connect_starrocks(config)
    try:
        cursor = conn.cursor()
        cursor.execute(_SEASON_SQL, (int(competition_id),))
        season_ids = [
            int(r[0]) for r in cursor.fetchall() if r and r[0] is not None
        ]
        if not season_ids:
            return {
                "season_id": None,
                "season_ids": [],
                "team_ids": [],
                "player_ids": [],
            }
        # _SEASON_SQL 按 start_date DESC 排序，第一个就是当季。
        season_id = season_ids[0]
        # pymysql 把 tuple 展开成 IN (...)；单元素时也会生成合法的 IN (x)。
        season_tuple = tuple(season_ids)

        cursor.execute(_TEAM_TAGS_SQL, (season_tuple,))
        team_ids = [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]

        cursor.execute(_PLAYER_TAGS_SQL, (season_tuple,))
        player_ids = [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]
        return {
            "season_id": season_id,
            "season_ids": season_ids,
            "team_ids": team_ids,
            "player_ids": player_ids,
        }
    finally:
        conn.close()


def fetch_national_teams(config: AppConfig) -> dict:
    """拉取全部国家队（``teams.type='national'``，不限 team_type）的球队/球员标签。

    不碰 seasons/season_teams，直接按球队类型取。返回结构与 ``fetch_competition``
    一致，``season_id`` 用哨兵 0 占位（仅为复用存储/校验/审计管道，国家队无赛季）。
    只读查询，不写任何东西。
    """
    conn = _connect_starrocks(config)
    try:
        cursor = conn.cursor()
        cursor.execute(_NATIONAL_TEAM_TAGS_SQL)
        team_ids = [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]

        cursor.execute(_NATIONAL_PLAYER_TAGS_SQL)
        player_ids = [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]
        return {
            "season_id": _NATIONAL_SEASON_SENTINEL,
            "season_ids": [_NATIONAL_SEASON_SENTINEL],
            "team_ids": team_ids,
            "player_ids": player_ids,
        }
    finally:
        conn.close()


def _validate(config: AppConfig, fetched: dict, previous: dict | None) -> tuple[bool, str]:
    """安全校验：决定这批数据能否覆盖本地。

    - 拉空（球队或球员为 0）一律拒绝。
    - 有历史成功基线时，球队或球员数骤降超过阈值则拒绝（防赛季切换塌缩）。
    返回 ``(accepted, reason)``；reason 为空表示通过。
    """
    team_count = len(fetched["team_ids"])
    player_count = len(fetched["player_ids"])
    if fetched["season_id"] is None:
        return False, "未找到进行中赛季"
    if team_count == 0 or player_count == 0:
        return False, f"拉取为空（球队 {team_count} / 球员 {player_count}）"

    prev_ok = (
        previous is not None
        and str(previous.get("status")) == "SUCCESS"
        and int(previous.get("team_count") or 0) > 0
    )
    if prev_ok:
        keep = 1.0 - float(config.competition_tags_max_drop_ratio)
        min_team = int(int(previous["team_count"]) * keep)
        min_player = int(int(previous["player_count"]) * keep)
        if team_count < min_team or player_count < min_player:
            return False, (
                f"较上次骤降（球队 {team_count}<{min_team} 或 "
                f"球员 {player_count}<{min_player}），保留上一版数据"
            )
        return True, ""

    return True, ""


def sync_competition(config: AppConfig, competition_id: int, connection) -> dict:
    """刷新单个 competition：拉取 → 校验 → 通过则原子替换，否则保留旧数据。

    正常路径走 ``fetch_competition``。国家队哨兵 id 的分支（走
    ``fetch_national_teams``）已停用：``_all_competition_ids`` 不再产出该哨兵
    （国家队白名单已退出，原因见 ``_WHITELIST_ROUTING``），故正常刷新不会触发；
    分支保留仅为向后兼容手动调用。
    """
    try:
        # 国家队哨兵分支已停用：正常刷新走不到这里（_all_competition_ids 不产出 -1）。
        if int(competition_id) == NATIONAL_TEAMS_COMPETITION_ID:
            fetched = fetch_national_teams(config)
        else:
            fetched = fetch_competition(config, competition_id)
    except Exception as exc:  # noqa: BLE001 - 单个联赛失败不影响其他
        logger.exception("联赛标签拉取失败 competition_id=%s", competition_id)
        repo.mark_competition_tag_run(
            competition_id, connection, status="FAILED", error=str(exc)[:500]
        )
        return {"competition_id": competition_id, "status": "FAILED", "error": str(exc)[:300]}

    previous = repo.get_competition_tag_run(competition_id, connection)
    accepted, reason = _validate(config, fetched, previous)
    team_count = len(fetched["team_ids"])
    player_count = len(fetched["player_ids"])
    if not accepted:
        repo.mark_competition_tag_run(
            competition_id, connection, status="REJECTED",
            season_id=fetched["season_id"], team_count=team_count,
            player_count=player_count, error=reason,
        )
        logger.warning("联赛标签被拒绝 competition_id=%s: %s", competition_id, reason)
        return {"competition_id": competition_id, "status": "REJECTED", "error": reason}

    repo.replace_competition_tags(
        competition_id, int(fetched["season_id"]),
        fetched["team_ids"], fetched["player_ids"], connection,
    )
    repo.mark_competition_tag_run(
        competition_id, connection, status="SUCCESS",
        season_id=fetched["season_id"], team_count=team_count,
        player_count=player_count,
    )
    return {
        "competition_id": competition_id, "status": "SUCCESS",
        "team_count": team_count, "player_count": player_count,
    }


def reload_tag_caches(connection) -> dict:
    """从本地表构建按栏目白名单 + 动态黑名单，注入 channel_filter 的内存缓存。

    白名单：每个 whitelist competition 的标签聚合到它绑定的栏目
    （backend_tab_id），多个 competition 映射到同一栏目时取并集。
    黑名单：全部 BLACKLIST_COMPETITIONS 的标签取并集。
    返回摘要 {"tabs": {backend_tab_id: 标签数}, "blocked": 动态黑名单标签数}。
    """
    tags_by_competition = repo.load_competition_channel_tags(connection)
    allowed_by_tab: dict[int, set[int]] = {}
    for competition_id, tab_name in _WHITELIST_ROUTING:
        channel_ids = tags_by_competition.get(int(competition_id))
        if not channel_ids:
            continue
        tab = repo.get_tab_by_name(tab_name, connection)
        if tab is None or tab.get("backend_tab_id") in (None, ""):
            continue
        backend_tab_id = int(tab["backend_tab_id"])
        allowed_by_tab.setdefault(backend_tab_id, set()).update(channel_ids)
    channel_filter.set_tab_whitelist(allowed_by_tab)

    dynamic_blocked: set[int] = set()
    for competition_id, _name in BLACKLIST_COMPETITIONS:
        dynamic_blocked.update(tags_by_competition.get(int(competition_id)) or ())
    channel_filter.set_dynamic_blocked(dynamic_blocked)

    return {
        "tabs": {tab_id: len(ids) for tab_id, ids in allowed_by_tab.items()},
        "blocked": len(dynamic_blocked),
    }


def _is_due(config: AppConfig, connection, now: datetime | None = None) -> bool:
    """今天（北京时区）的刷新时刻已过、且今天还没成功刷过，就该刷。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(BEIJING)
    boundary_passed = (
        local.hour > config.competition_tags_hour
        or (
            local.hour == config.competition_tags_hour
            and local.minute >= config.competition_tags_minute
        )
    )
    if not boundary_passed:
        return False
    last = repo.get_setting(_LAST_SYNC_SETTING, default="", connection=connection)
    return str(last) != local.date().isoformat()


def sync_due(
    config: AppConfig, connection=None, now: datetime | None = None, *, force: bool = False
) -> dict:
    """到点就刷新全部 competition；完成后刷新内存缓存并记下同步日期。

    ``force=True`` 跳过到点判断，用于手工/CLI 立即刷新（方案 A 首次生成）。
    """
    if not config.competition_tags_enabled:
        return {"ran": False, "reason": "disabled"}
    if not credentials_configured(config):
        return {"ran": False, "reason": "starrocks_not_configured"}
    if not force and not _is_due(config, connection, now):
        return {"ran": False, "reason": "not_due"}

    results = [
        sync_competition(config, competition_id, connection)
        for competition_id in _all_competition_ids()
    ]
    caches = reload_tag_caches(connection)

    local = (now or datetime.now(timezone.utc)).astimezone(BEIJING)
    # 只要本轮没有因异常中断就记为「今天已同步」；REJECTED 属于预期内的安全保留，
    # 不该让整轮每个周期反复重跑。
    repo.set_setting(_LAST_SYNC_SETTING, local.date().isoformat(), connection)

    success = sum(1 for r in results if r["status"] == "SUCCESS")
    rejected = sum(1 for r in results if r["status"] == "REJECTED")
    failed = sum(1 for r in results if r["status"] == "FAILED")
    return {
        "ran": True,
        "success": success,
        "rejected": rejected,
        "failed": failed,
        "tabs_loaded": len(caches["tabs"]),
        "blocked_loaded": caches["blocked"],
        "results": results,
    }


class CompetitionTagsController:
    """后台线程里跑一次刷新检查（单飞），镜像 LeagueClicksController。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict | None = None

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    def start(self) -> bool:
        if not self.config.competition_tags_enabled:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(
            target=self._run, name="competition-tags-worker", daemon=True
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True

    def _run(self) -> None:
        # _connect 必须在 try 里：连接失败（如 SQLite 瞬时 authorization denied）
        # 时也要走 finally 复位 _running，否则线程永久卡死、再也不刷新。
        conn = None
        try:
            conn = _connect(self.config.database_path)
            self._last_result = sync_due(self.config, conn)
        except Exception:  # noqa: BLE001 - keep future scheduler ticks alive
            logger.exception("联赛标签同步任务失败")
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self._running = False


class CompetitionTagsScheduler:
    """轮询判断是否到点刷新，幂等靠 settings 里的 last_sync_date。"""

    def __init__(self, controller: CompetitionTagsController, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = max(15, int(interval_seconds))
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.controller.config.competition_tags_enabled:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop, name="competition-tags-scheduler", daemon=True
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
        # 启动先跑一次：加载已有数据进内存缓存，并在停机数日后开机即追平。
        self.controller.start()
        while not self._shutdown.wait(self.interval_seconds):
            self.controller.start()
