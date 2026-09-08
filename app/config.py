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
    dqd_open_idempotency_enabled: bool = _env_bool(
        "DQD_OPEN_IDEMPOTENCY_ENABLED", False
    )
    dqd_open_idempotency_field: str = (
        os.getenv("DQD_OPEN_IDEMPOTENCY_FIELD", "client_request_id").strip()
        or "client_request_id"
    )
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

    # Open-platform article creation is a write operation, so it stays behind
    # an explicit flag even when credentials are present.
    publisher_enabled: bool = _env_bool("AUTOMATIC_POST_PUBLISHER", False)

    @property
    def material_configured(self) -> bool:
        return bool(self.material_api_key and self.material_caller)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def dqd_configured(self) -> bool:
        return bool(self.dqd_session_cookie)

    @property
    def dqd_open_configured(self) -> bool:
        return bool(self.dqd_open_appid and self.dqd_open_appsecret and self.dqd_open_enname)

    @property
    def feishu_report_configured(self) -> bool:
        return bool(self.feishu_report_enabled and self.feishu_report_webhook_url)


def ensure_instance_dir() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
