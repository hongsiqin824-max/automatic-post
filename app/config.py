"""Runtime configuration for the independent automatic-post project."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    dqd_open_enname: str = os.getenv("DQD_OPEN_ENNAME", "hongsiqin").strip()
    dqd_open_status: int = _env_int("DQD_OPEN_STATUS", 0)
    dqd_open_timeout: int = max(5, _env_int("DQD_OPEN_TIMEOUT_SECONDS", 30))
    dqd_open_archive_level: str = os.getenv("DQD_OPEN_ARCHIVE_LEVEL", "B").strip().upper() or "B"

    scheduler_enabled: bool = _env_bool("AUTOMATIC_POST_SCHEDULER", True)
    scheduler_interval_seconds: int = max(
        60, _env_int("AUTOMATIC_POST_INTERVAL_SECONDS", 600)
    )

    # The v1 app stops at READY_TO_PUBLISH. This flag is intentionally kept
    # separate so the future DQD publisher can be enabled without changing the
    # ingestion and quality state machine.
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


def ensure_instance_dir() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
