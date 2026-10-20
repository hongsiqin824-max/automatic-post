"""Runtime configuration for the independent automatic-post project."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = ROOT_DIR / "instance"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _parse_boundaries(text: str) -> tuple[tuple[int, int], ...]:
    """Parse ``"11:00,19:00"`` into sorted, de-duplicated (hour, minute) pairs.

    Malformed entries are dropped rather than raising: a typo in the
    environment should not stop the whole app from booting.
    """

    parsed: set[tuple[int, int]] = set()
    for chunk in str(text or "").split(","):
        stripped = chunk.strip()
        if not stripped:
            continue
        hour_text, _, minute_text = stripped.partition(":")
        try:
            hour = int(hour_text)
            minute = int(minute_text or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.add((hour, minute))
    return tuple(sorted(parsed))


def _env_boundaries(name: str, default: str) -> tuple[tuple[int, int], ...]:
    """Read report boundaries from the environment, falling back to *default*.

    An unparseable value falls back instead of yielding an empty list: with no
    boundary at all the reporter would silently never send anything.
    """

    return _parse_boundaries(os.getenv(name) or "") or _parse_boundaries(default)


def _env_choice(name: str, default: str, allowed: frozenset[str]) -> str:
    value = (os.getenv(name) or "").strip().lower()
    return value if value in allowed else default


def _env_json_object(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"环境变量 {name} 必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError(f"环境变量 {name} 必须是 JSON 对象")
    return {str(key): str(val) for key, val in value.items()}


def _default_redirect_uri() -> str:
    host = os.getenv("AUTOMATIC_POST_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("AUTOMATIC_POST_PORT", "8890").strip() or "8890"
    return f"http://{host}:{port}/api/open/auth/callback"


@dataclass(frozen=True)
class AppConfig:
    database_path: str = os.getenv(
        "AUTOMATIC_POST_DB",
        str(INSTANCE_DIR / "automatic_post.sqlite3"),
    )
    host: str = os.getenv("AUTOMATIC_POST_HOST", "127.0.0.1")
    port: int = _env_int("AUTOMATIC_POST_PORT", 8890)
    debug: bool = _env_bool("AUTOMATIC_POST_DEBUG", False)

    material_base_url: str = os.getenv(
        "MATERIAL_API_BASE_URL", "https://aigc-core.dongqiudi.com"
    ).rstrip("/")
    material_api_key: str = os.getenv("MATERIAL_API_KEY", "")
    material_caller: str = os.getenv("MATERIAL_API_CALLER", "")
    fetch_hours: int = max(1, min(24, _env_int("MATERIAL_FETCH_HOURS", 24)))
    fetch_limit: int = max(1, min(500, _env_int("MATERIAL_FETCH_LIMIT", 500)))
    request_timeout: int = max(5, _env_int("MATERIAL_REQUEST_TIMEOUT", 20))

    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-5.5")
    llm_timeout: int = max(10, _env_int("LLM_TIMEOUT_SECONDS", 45))
    llm_max_retries: int = max(0, min(3, _env_int("LLM_MAX_RETRIES", 2)))
    llm_retry_delay_seconds: float = max(
        0.1, min(10.0, _env_float("LLM_RETRY_DELAY_SECONDS", 0.5))
    )

    # Fallback LLM: tried when the primary model is temporarily unavailable
    # (timeout / connection / rate_limit / http 5xx).  The retry budget is
    # deliberately small (1 attempt) because this is an emergency switch, not
    # a retry loop — the primary already exhausted its own retries.
    llm_api_key2: str = os.getenv("LLM_API_KEY2", "")
    llm_base_url2: str = os.getenv("LLM_BASE_URL2", "https://api.openai.com/v1")
    llm_model2: str = os.getenv("LLM_MODEL2", "gpt-5.5")
    llm_timeout2: int = max(10, _env_int("LLM_TIMEOUT_SECONDS2", 80))
    llm_max_retries2: int = max(0, min(2, _env_int("LLM_MAX_RETRIES2", 1)))
    llm_retry_delay_seconds2: float = max(
        0.1, min(10.0, _env_float("LLM_RETRY_DELAY_SECONDS2", 1.0))
    )

    dqd_base_url: str = os.getenv(
        "DQD_BASE_URL", "https://dadmin.dongqiudi.com"
    ).rstrip("/")
    dqd_session_cookie: str = os.getenv("DQD_SESSION_COOKIE", "")
    dqd_open_appid: str = os.getenv("DQD_OPEN_APPID", "").strip()
    dqd_open_appsecret: str = os.getenv("DQD_OPEN_APPSECRET", "").strip()
    dqd_open_base_url: str = os.getenv(
        "DQD_OPEN_BASE_URL", "https://platform.dongqiudi.com/open/v1/do"
    ).rstrip("/")
    dqd_open_api_name: str = os.getenv(
        "DQD_OPEN_API_NAME", "admin-archive-createarticle"
    ).strip()
    dqd_headers: dict[str, str] = field(default_factory=lambda: _env_json_object("DQD_OPEN_API_HEADERS_JSON"))
    dqd_open_redirect_uri: str = os.getenv(
        "DQD_OPEN_REDIRECT_URI", _default_redirect_uri()
    ).strip()
    dqd_open_enname: str = os.getenv("DQD_OPEN_ENNAME", "").strip()
    dqd_open_status: int = _env_int("DQD_OPEN_STATUS", 0)
    dqd_open_timeout: int = max(5, _env_int("DQD_OPEN_TIMEOUT_SECONDS", 30))
    dqd_open_archive_level: str = os.getenv("DQD_OPEN_ARCHIVE_LEVEL", "B").strip().upper() or "B"
    # 懂球帝开放平台没有幂等请求键，也没有查询接口，因此任何「结果未知后重发」
    # 都可能创建出第二篇。只保留创建草稿的一次性 502 重试：草稿重复只落在后台
    # 且可人工删除，而直接发布的重复对读者可见，一律不重发。
    dqd_open_502_retry_enabled: bool = _env_bool(
        "DQD_OPEN_502_RETRY_ENABLED", True
    )
    dqd_open_502_retry_delay_seconds: int = max(
        1, min(300, _env_int("DQD_OPEN_502_RETRY_DELAY_SECONDS", 5))
    )

    scheduler_enabled: bool = _env_bool("AUTOMATIC_POST_SCHEDULER", True)
    scheduler_interval_seconds: int = max(
        60, _env_int("AUTOMATIC_POST_INTERVAL_SECONDS", 600)
    )

    # Daily direct-publish report sent to a Feishu group bot.  The webhook is
    # intentionally supplied through the environment rather than committed.
    feishu_report_webhook_url: str = os.getenv(
        "FEISHU_REPORT_WEBHOOK_URL", ""
    ).strip()
    feishu_report_enabled: bool = _env_bool("FEISHU_REPORT_ENABLED", True)
    feishu_report_hour: int = max(0, min(23, _env_int("FEISHU_REPORT_HOUR", 19)))
    feishu_report_minute: int = max(0, min(59, _env_int("FEISHU_REPORT_MINUTE", 0)))
    feishu_report_timeout_seconds: int = max(
        5, _env_int("FEISHU_REPORT_TIMEOUT_SECONDS", 10)
    )
    feishu_report_retry_seconds: int = max(
        60, _env_int("FEISHU_REPORT_RETRY_SECONDS", 300)
    )
    feishu_report_stale_seconds: int = max(
        300, _env_int("FEISHU_REPORT_STALE_SECONDS", 900)
    )
    feishu_report_check_interval_seconds: int = max(
        15, min(3600, _env_int("FEISHU_REPORT_CHECK_INTERVAL_SECONDS", 30))
    )

    # 来源抓取量预警，发给另一个飞书群机器人（与上面的发布统计是两个不同的
    # webhook）。一天多期：每个边界发一次，统计的是「上一个边界到这个边界」，
    # 所以边界列表既决定了发送时刻、也决定了统计区间的切分。
    source_report_webhook_url: str = os.getenv(
        "SOURCE_REPORT_WEBHOOK_URL", ""
    ).strip()
    source_report_enabled: bool = _env_bool("SOURCE_REPORT_ENABLED", True)
    source_report_boundaries: tuple[tuple[int, int], ...] = _env_boundaries(
        "SOURCE_REPORT_BOUNDARIES", "11:00,19:00"
    )
    source_report_timeout_seconds: int = max(
        5, _env_int("SOURCE_REPORT_TIMEOUT_SECONDS", 10)
    )
    source_report_retry_seconds: int = max(
        60, _env_int("SOURCE_REPORT_RETRY_SECONDS", 300)
    )
    source_report_stale_seconds: int = max(
        300, _env_int("SOURCE_REPORT_STALE_SECONDS", 900)
    )
    source_report_check_interval_seconds: int = max(
        15, min(3600, _env_int("SOURCE_REPORT_CHECK_INTERVAL_SECONDS", 30))
    )

    # 提交懂球帝失败告警：每隔 check_interval 看一次最近 lookback 秒内落
    # PUBLISH_FAILED 的篇数，达到 threshold 就推飞书；发过之后 cooldown 内不再发，
    # 上游持续故障时每个 cooldown 提醒一次。webhook 不单独配时沿用来源预警机器人。
    publish_alert_enabled: bool = _env_bool("PUBLISH_ALERT_ENABLED", True)
    publish_alert_webhook_url: str = os.getenv("PUBLISH_ALERT_WEBHOOK_URL", "").strip()
    publish_alert_threshold: int = max(1, _env_int("PUBLISH_ALERT_THRESHOLD", 3))
    publish_alert_lookback_seconds: int = max(
        60, min(86400, _env_int("PUBLISH_ALERT_LOOKBACK_SECONDS", 900))
    )
    publish_alert_cooldown_seconds: int = max(
        60, min(86400, _env_int("PUBLISH_ALERT_COOLDOWN_SECONDS", 1800))
    )
    publish_alert_check_interval_seconds: int = max(
        15, min(3600, _env_int("PUBLISH_ALERT_CHECK_INTERVAL_SECONDS", 300))
    )
    publish_alert_timeout_seconds: int = max(
        5, _env_int("PUBLISH_ALERT_TIMEOUT_SECONDS", 10)
    )

    # 联赛 tab 点击数据：每天凌晨从 StarRocks 拉前一天的结果存进本地累积表。
    # 上游实时表只保留约 30 天，所以补数窗口也就只能是 30 天。
    # StarRocks 连接参数复用 dqd-bigdata-sr 技能里已配好的 .env，
    # 也可以用下面的环境变量直接覆盖。
    league_clicks_enabled: bool = _env_bool("LEAGUE_CLICKS_ENABLED", True)
    league_clicks_hour: int = max(0, min(23, _env_int("LEAGUE_CLICKS_HOUR", 3)))
    league_clicks_minute: int = max(0, min(59, _env_int("LEAGUE_CLICKS_MINUTE", 0)))
    league_clicks_check_interval_seconds: int = max(
        15, min(3600, _env_int("LEAGUE_CLICKS_CHECK_INTERVAL_SECONDS", 60))
    )
    # 最多往前补几天。上游只留约 30 天，设更大也补不回来。
    league_clicks_backfill_days: int = max(
        1, min(30, _env_int("LEAGUE_CLICKS_BACKFILL_DAYS", 30))
    )
    # 单日查询要 70~120 秒，留足超时余量。
    league_clicks_query_timeout_seconds: int = max(
        60, min(1800, _env_int("LEAGUE_CLICKS_QUERY_TIMEOUT_SECONDS", 540))
    )
    starrocks_host: str = os.getenv("STARROCKS_HOST", "").strip()
    starrocks_port: int = _env_int("STARROCKS_PORT", 0)
    starrocks_user: str = os.getenv("STARROCKS_USER", "").strip()
    starrocks_password: str = os.getenv("STARROCKS_PASSWORD", "")

    # 联赛点击数据周报：每周六自动汇总前 7 天数据并写入飞书表格
    league_clicks_weekly_enabled: bool = _env_bool("LEAGUE_CLICKS_WEEKLY_ENABLED", True)
    league_clicks_weekly_hour: int = max(0, min(23, _env_int("LEAGUE_CLICKS_WEEKLY_HOUR", 19)))
    league_clicks_weekly_minute: int = max(0, min(59, _env_int("LEAGUE_CLICKS_WEEKLY_MINUTE", 0)))
    league_clicks_weekly_check_interval_seconds: int = max(
        15, min(3600, _env_int("LEAGUE_CLICKS_WEEKLY_CHECK_INTERVAL_SECONDS", 60))
    )
    feishu_app_id: str = os.getenv("FEISHU_APP_ID", "").strip()
    feishu_app_secret: str = os.getenv("FEISHU_APP_SECRET", "").strip()
    feishu_spreadsheet_token: str = os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip()
    feishu_api_timeout_seconds: int = max(5, _env_int("FEISHU_API_TIMEOUT_SECONDS", 30))

    # 栏目标签白名单：每天从 StarRocks 按「进行中赛季」拉取各联赛的球队/球员
    # 标签，写进本地表后在提交懂球帝前把文章标签过滤到所属栏目联赛的范围内，
    # 解决上游乱打标签。StarRocks 连接参数复用 league_clicks 的 resolve_credentials。
    competition_tags_enabled: bool = _env_bool("AUTOMATIC_POST_COMPETITION_TAGS_ENABLED", True)
    competition_tags_hour: int = max(0, min(23, _env_int("AUTOMATIC_POST_COMPETITION_TAGS_HOUR", 4)))
    competition_tags_minute: int = max(0, min(59, _env_int("AUTOMATIC_POST_COMPETITION_TAGS_MINUTE", 0)))
    competition_tags_check_interval_seconds: int = max(
        15, min(3600, _env_int("AUTOMATIC_POST_COMPETITION_TAGS_CHECK_INTERVAL_SECONDS", 60))
    )
    # 与上一次成功相比，球队/球员数骤降超过该比例则拒绝覆盖、保留上一版 good 数据，
    # 防赛季切换瞬间 season_teams 尚未灌满导致白名单塌缩误删标签。
    competition_tags_max_drop_ratio: float = max(
        0.0, min(1.0, _env_float("AUTOMATIC_POST_COMPETITION_TAGS_MAX_DROP_RATIO", 0.5))
    )
    competition_tags_query_timeout_seconds: int = max(
        30, min(1800, _env_int("AUTOMATIC_POST_COMPETITION_TAGS_QUERY_TIMEOUT_SECONDS", 180))
    )

    # Open-platform article creation is a write operation, so it stays behind
    # an explicit flag even when credentials are present.
    publisher_enabled: bool = _env_bool("AUTOMATIC_POST_PUBLISHER", False)
    # Independent publish worker: drains READY_TO_PUBLISH on its own cadence so
    # queued articles no longer wait for a full (and slow) ingestion cycle to
    # finish before the end-of-run publish step reaches them. When disabled the
    # scheduler falls back to the draft-confirmation maintenance worker.
    publish_worker_enabled: bool = _env_bool("AUTOMATIC_POST_PUBLISH_WORKER", True)
    publish_worker_interval_seconds: int = max(
        5, min(3600, _env_int("AUTOMATIC_POST_PUBLISH_WORKER_INTERVAL_SECONDS", 30))
    )

    # Title dedup for direct-publish articles: lexical recall against locally
    # published (window) and in-flight articles, then an LLM same-fact decision.
    title_dedup_enabled: bool = _env_bool("AUTOMATIC_POST_TITLE_DEDUP_ENABLED", True)
    title_dedup_hours: int = max(1, min(72, _env_int("AUTOMATIC_POST_TITLE_DEDUP_HOURS", 24)))
    title_dedup_dice_min: float = max(0.0, min(1.0, _env_float("AUTOMATIC_POST_TITLE_DEDUP_DICE_MIN", 0.25)))
    # 同一事件的稿件常被路由到不同标签，因此同栏目也纳入召回；但栏目比标签粗得多，
    # 仅靠同栏目入围时要求更高的词面相似度，避免把低相似候选灌进 LLM 判定。
    title_dedup_tab_dice_min: float = max(0.0, min(1.0, _env_float("AUTOMATIC_POST_TITLE_DEDUP_TAB_DICE_MIN", 0.6)))
    title_dedup_lcs_min: int = max(2, min(12, _env_int("AUTOMATIC_POST_TITLE_DEDUP_LCS_MIN", 4)))
    # 语序无关的兜底信号。bigram 和最长公共子串都吃语序，而「町田2-4柏」和
    # 「柏4-2逆转町田」写的是同一场球：跨越主客队顺序后两者几乎没有公共二元组
    # （实测 0.13），只有按字符重合度才看得出是同一件事（0.49）。取 0.48 是因为
    # 同栏目随机配对的字符重合度 P99 只有 0.46，再低就会把无关稿件灌进候选。
    title_dedup_char_dice_min: float = max(0.0, min(1.0, _env_float("AUTOMATIC_POST_TITLE_DEDUP_CHAR_DICE_MIN", 0.48)))
    # 同一事件常有五六家媒体各发一篇，上限太小会把真正的孪生稿挤出送审名单：
    # 实测一天 590 篇里有 180 篇的候选被截到 3，放到 5 能降到 131。放宽不增加
    # 调用次数（一次判定只是多带几个候选），但候选越多 LLM 越容易挑错，所以
    # 先保守留在 3，要放宽用环境变量调，别忘了 .env 会覆盖这里。
    title_dedup_max_candidates: int = max(1, min(10, _env_int("AUTOMATIC_POST_TITLE_DEDUP_MAX_CANDIDATES", 3)))
    # 正文二级确认：标题判重之后，再拿两篇正文问一次，只有正文也认才真的拦下。
    # 标题只有 20 来个字，同一场比赛的首发名单和赛前前瞻在标题层面长得一样，
    # 换更强的模型也变不出信息（实测 pro 的误杀率 44.3% 反而高于 flash 的 39.5%），
    # 只有把正文喂进去才有新信息。实测误杀率可从约 10% 降到 4~5%，代价是放跑
    # 约 4% 的真重复。任何一步取不到正文或调用失败都维持原判定，不引入新风险。
    title_dedup_body_confirm_enabled: bool = _env_bool(
        "AUTOMATIC_POST_TITLE_DEDUP_BODY_CONFIRM", True
    )
    # 送进确认的正文长度。测量用的就是 420 字，够覆盖导语和核心事实；再长只是
    # 把赛后感想和背景资料塞进去，对判断没帮助还更贵。
    title_dedup_body_confirm_chars: int = max(
        200, min(2000, _env_int("AUTOMATIC_POST_TITLE_DEDUP_BODY_CONFIRM_CHARS", 420))
    )
    # 两侧正文都至少要有这么多字，否则没有判断依据，直接跳过确认维持原判定。
    title_dedup_body_confirm_min_chars: int = max(
        20, min(500, _env_int("AUTOMATIC_POST_TITLE_DEDUP_BODY_CONFIRM_MIN_CHARS", 60))
    )

    # AI 栏目归属护栏。原实现只会逐个追问「是否属于某个预配候选栏目」
    # （tabs.ai_fallback_tab_ids），近半数栏目没配候选，于是 AI 答完「不属于」
    # 就无处可去、文章滞留草稿。开启分类模式后改为一次调用在全部候选栏目里选一个，
    # 覆盖面不再取决于人工配置。关掉即回退到原级联行为。
    league_guard_classifier_enabled: bool = _env_bool(
        "AUTOMATIC_POST_LEAGUE_GUARD_CLASSIFIER", True
    )
    league_guard_min_confidence: float = max(
        0.0, min(1.0, _env_float("AUTOMATIC_POST_LEAGUE_GUARD_MIN_CONFIDENCE", 0.9))
    )
    # 改挂到别的栏目后用哪个发布模式：
    #   always_direct 一律升级为直接发布（默认，也是改造前的级联行为）
    #   target        沿用目标栏目自己的 publish_mode
    # 默认取 always_direct 而不是 target：32 个候选栏目里只有「瑞典超」「澳超」
    # 配成直发，其余都是草稿，所以 target 会让绝大多数改挂结果停在草稿——纠偏挂
    # 对了栏目却发不出去，等于把现有级联的收益也一起退掉。
    league_guard_reassign_publish_mode: str = _env_choice(
        "AUTOMATIC_POST_LEAGUE_GUARD_REASSIGN_MODE",
        "always_direct",
        frozenset({"target", "always_direct"}),
    )

    # 内容相关性过滤（全量 AI 归属校验的扩展）。原护栏三道闸只在「草稿模式 + 非
    # league 路径 + 栏目显式开 ai_league_guard_enabled」时才跑，于是 intl 这类
    # catch-all 标记硬挂栏目后直发，全程没看过内容。开启后对 scope 命中的来源在发布
    # 前补跑一次归属校验：挂错的改挂、确属的保留、高置信「谁都不属于」的直发稿降级为
    # 草稿送审；拿不准 / 调用失败一律放行（fail-open），绝不因不确定而少发。
    relevance_guard_enabled: bool = _env_bool(
        "AUTOMATIC_POST_RELEVANCE_GUARD_ENABLED", True
    )
    # 生效范围：填 route_league 短码名单（逗号分隔，如 "intl,asia"），或 "all" 覆盖
    # 全部。默认只打最脏的 catch-all 入口 intl，验证后再放量。
    relevance_guard_scope: str = os.getenv(
        "AUTOMATIC_POST_RELEVANCE_GUARD_SCOPE", "intl"
    ).strip()
    # 直发稿被判「谁都不属于任何栏目」要降级草稿，至少要这么大的把握，防误伤少发。
    relevance_guard_min_confidence: float = max(
        0.0, min(1.0, _env_float("AUTOMATIC_POST_RELEVANCE_GUARD_MIN_CONFIDENCE", 0.9))
    )
    # 是否保留「判定确属当前栏目 → 把草稿升级为直接发布」这个现有行为。关掉后确属的
    # 草稿仍留草稿，只做改挂与拦截。
    relevance_guard_keep_upgrade: bool = _env_bool(
        "AUTOMATIC_POST_RELEVANCE_GUARD_KEEP_UPGRADE", True
    )

    # Concurrent quality workers per ingestion run; 1 keeps serial behavior.
    quality_workers: int = max(1, min(8, _env_int("AUTOMATIC_POST_QUALITY_WORKERS", 4)))

    # Transient LLM outages (timeout / connection / rate-limit / invalid JSON)
    # must not permanently park an article in manual review.  Each ingestion
    # pass re-checks a bounded number of such articles once enough delay has
    # passed, up to N automatic attempts per article.
    transient_recheck_enabled: bool = _env_bool(
        "AUTOMATIC_POST_TRANSIENT_RECHECK_ENABLED", True
    )
    transient_recheck_delay_seconds: int = max(
        60, _env_int("AUTOMATIC_POST_TRANSIENT_RECHECK_DELAY_SECONDS", 600)
    )
    transient_recheck_max_attempts: int = max(
        1, min(10, _env_int("AUTOMATIC_POST_TRANSIENT_RECHECK_MAX_ATTEMPTS", 2))
    )
    transient_recheck_batch_limit: int = max(
        1, min(100, _env_int("AUTOMATIC_POST_TRANSIENT_RECHECK_BATCH_LIMIT", 20))
    )

    @property
    def material_configured(self) -> bool:
        return bool(self.material_api_key and self.material_caller)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def llm_fallback_configured(self) -> bool:
        return bool(self.llm_api_key2)

    @property
    def dqd_configured(self) -> bool:
        return bool(self.dqd_session_cookie)

    @property
    def dqd_open_configured(self) -> bool:
        return bool(self.dqd_open_appid and self.dqd_open_appsecret and self.dqd_open_enname)

    @property
    def feishu_report_configured(self) -> bool:
        return bool(self.feishu_report_enabled and self.feishu_report_webhook_url)

    @property
    def source_report_configured(self) -> bool:
        return bool(
            self.source_report_enabled
            and self.source_report_webhook_url
            and self.source_report_boundaries
        )

    @property
    def publish_alert_resolved_webhook_url(self) -> str:
        return self.publish_alert_webhook_url or self.source_report_webhook_url

    @property
    def publish_alert_configured(self) -> bool:
        return bool(self.publish_alert_enabled and self.publish_alert_resolved_webhook_url)

    @property
    def feishu_spreadsheet_configured(self) -> bool:
        return bool(
            self.league_clicks_weekly_enabled
            and self.feishu_app_id
            and self.feishu_app_secret
            and self.feishu_spreadsheet_token
        )


def ensure_instance_dir() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
