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


def ensure_instance_dir() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
