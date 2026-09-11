"""Persistence operations for articles, configuration, and workflow history."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from .db import get_db, _prepare_connection
from .origin_identity import source_origin_key
from .services.article_images import _usable_image_src
from .services.channel_filter import filter_blocked_channels
from .services.link_sanitizer import remove_clickable_links


VALID_STATUSES = {
    "RECEIVED",
    "QUALITY_CHECKING",
    "NEEDS_REVIEW",
    "READY_TO_PUBLISH",
    "PUBLISHING",
    "DRAFT_CONFIRMING",
    "DRAFT_CREATED",
    "PUBLISHED",
    "PUBLISH_FAILED",
    "REJECTED",
    "ALREADY_PUBLISHED",
    "MAPPING_BLOCKED",
    "SOURCE_DUPLICATE",
    "TITLE_DUPLICATE",
    "ERROR",
}
VALID_LEVELS = {"S", "A", "B", "C"}
VALID_EVENT_MARKER_TYPES = {"league", "team"}
EMPTY_EVENT_MARKERS = {"", "_", "xxx", "null", "none"}
_UNSET = object()


def _conn(connection=None):
    return _prepare_connection(connection) if connection is not None else get_db()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Optional[str], default: Any):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row(row):
    return dict(row) if row is not None else None


def _rows(rows):
    return [dict(row) for row in rows]


def _tab_ids(values: Any) -> list[int]:
    if values is None:
        return []
    if isinstance(values, (str, int)):
        values = [values]
    result = []
    for value in values:
        try:
            tab_id = int(value)
        except (TypeError, ValueError):
            raise ValueError("invalid tab id") from None
        if tab_id not in result:
            result.append(tab_id)
    return result


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be a positive integer")
    if result <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return result


def _publish_user_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("publish account user_name is required")
    if len(name) > 200:
        raise ValueError("publish account user_name is too long")
    return name


def _enabled_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in {0, 1}:
        return value
    raise ValueError("enabled must be a boolean")


def _publish_mode_int(value: Any, field_name: str = "publish_mode") -> int:
    """Normalize the two supported backend submission modes.

    ``0`` creates a DQD draft and ``1`` publishes immediately.  Keep this
    validation in the repository so web/API callers cannot persist an
    unsupported value and so SQLite CHECK errors become a useful message.
    """

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be 0 (draft) or 1 (publish)")
    if isinstance(value, int) and value in {0, 1}:
        return value
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return int(value.strip())
    raise ValueError(f"{field_name} must be 0 (draft) or 1 (publish)")


def _fallback_litpic(value: Any) -> str:
    """Validate a tab's optional CDN/path fallback image."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 2000 or _usable_image_src(text) is None:
        raise ValueError("fallback_litpic 必须是有效的 CDN 图片地址或图片路径")
    return text


def _source_code(value: Any) -> str:
    code = str(value or "").strip()
    if not code:
        raise ValueError("source code is required")
    if len(code) > 100:
        raise ValueError("source code is too long")
    if any(ord(char) < 32 for char in code):
        raise ValueError("source code contains invalid control characters")
    return code


def _source_display_name(value: Any, code: str) -> str:
    name = str(value if value is not None else code).strip()
    if not name:
        raise ValueError("source display_name is required")
    if len(name) > 80:
        raise ValueError("source display_name is too long")
    return name


def _event_marker_type(value: Any) -> str:
    marker_type = str(value or "").strip().lower()
    if marker_type not in VALID_EVENT_MARKER_TYPES:
        raise ValueError("marker_type must be league or team")
    return marker_type


def _event_marker_code(value: Any) -> str:
    marker_code = str(value or "").strip().lower()
    if marker_code in EMPTY_EVENT_MARKERS:
        raise ValueError("marker_code is required")
    if len(marker_code) > 80:
        raise ValueError("marker_code is too long")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", marker_code):
        raise ValueError("marker_code must use lowercase letters, numbers, _ or -")
    return marker_code


def _event_rule_source_code(value: Any, conn) -> Optional[str]:
    if value in (None, ""):
        return None
    code = _source_code(value)
    row = conn.execute("SELECT code FROM sources WHERE code=?", (code,)).fetchone()
    if row is None:
        raise ValueError("source not found")
    return str(row["code"])


def _event_rule_publish_mode(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return _publish_mode_int(value, "publish_mode_override")


def _optional_event_marker_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in EMPTY_EVENT_MARKERS:
        return ""
    try:
        return _event_marker_code(text)
    except ValueError:
        # An unexpected upstream marker must not block article ingestion.
        return ""


def parse_material_user_name(value: Any) -> dict[str, Any]:
    """Parse ``media:sport.league.team`` without trusting upstream text.

    Missing markers (``_``/``xxx``/empty) and malformed values resolve to an
    empty league/team, so the caller can safely retain the source's columns.
    """

    raw = str(value or "").strip()
    result: dict[str, Any] = {
        "raw": raw[:500],
        "valid": False,
        "media": "",
        "sport": "",
        "league": "",
        "team": "",
    }
    if not raw or len(raw) > 500:
        return result
    media, separator, markers = raw.partition(":")
    parts = markers.split(".") if separator else []
    if not media.strip() or len(parts) != 3:
        return result
    result.update({
        "valid": True,
        "media": media.strip().lower()[:100],
        "sport": _optional_event_marker_code(parts[0]),
        "league": _optional_event_marker_code(parts[1]),
        "team": _optional_event_marker_code(parts[2]),
    })
    return result


def _tabs_for_source_id(source_id: int, conn) -> list[dict]:
    return _rows(conn.execute(
        """
        SELECT t.*, st.sort_order
        FROM source_tabs st JOIN tabs t ON t.id=st.tab_id
        WHERE st.source_id=?
        ORDER BY st.sort_order, st.rowid
        """,
        (source_id,),
    ).fetchall())


def _tabs_for_article_id(article_id: int, conn) -> list[dict]:
    return _rows(conn.execute(
        """
        SELECT t.*, at.sort_order
        FROM article_tabs at JOIN tabs t ON t.id=at.tab_id
        WHERE at.article_id=?
        ORDER BY at.sort_order, at.rowid
        """,
        (article_id,),
    ).fetchall())


def _decorate_tabs(item: dict, tabs: list[dict]) -> dict:
    item["tabs"] = tabs
    item["tab_ids"] = [tab["id"] for tab in tabs]
    item["backend_tab_ids"] = [tab["backend_tab_id"] for tab in tabs]
    if tabs:
        item["tab_id"] = tabs[0]["id"]
        item["tab_name"] = tabs[0]["name"]
        item["backend_tab_id"] = tabs[0]["backend_tab_id"]
    else:
        item["tab_name"] = item.get("tab_name") or ""
    return item


def _decorate_article(item, conn=None):
    if item is None:
        return None
    item = dict(item)
    item["channels"] = _loads(item.get("channels_json"), [])
    item["quality"] = _loads(item.get("quality_json"), {})
    item["raw"] = _loads(item.get("raw_json"), {})
    item["raw_payload"] = item["raw"]
    item["original_title"] = item.get("title_original", "")
    item["cover_url"] = item.get("litpic", "")
    item["title"] = item.get("title_final", "")
    item["body"] = item.get("body_html", "")
    item["processed_body"] = item.get("body_html", "")
    item["original_body"] = str(item["raw"].get("translate_body") or item.get("body_html", ""))
    item["status_label"] = _status_label(item.get("status", ""))
    quality = item.get("quality") or {}
    item["quality_passed"] = quality.get("pass") if isinstance(quality, dict) else None
    item["quality_reason"] = quality.get("reason", "") if isinstance(quality, dict) else ""
    tabs = _tabs_for_article_id(item["id"], conn) if conn is not None else []
    return _decorate_tabs(item, tabs)


def _status_label(value: str) -> str:
    labels = {
        "RECEIVED": "已获取",
        "QUALITY_CHECKING": "质检中",
        "NEEDS_REVIEW": "待人工审核",
        "READY_TO_PUBLISH": "待发队列",
        "PUBLISHING": "提交懂球帝中",
        "DRAFT_CONFIRMING": "提交结果确认中",
        "DRAFT_CREATED": "草稿已创建",
        "PUBLISHED": "已直接发布",
        "PUBLISH_FAILED": "提交懂球帝失败",
        "REJECTED": "已驳回",
        "ALREADY_PUBLISHED": "已存在后台文章",
        "MAPPING_BLOCKED": "待匹配后台素材",
        "SOURCE_DUPLICATE": "来源重复（已拦截）",
        "TITLE_DUPLICATE": "标题重复（已取消自动发布）",
        "ERROR": "处理失败",
    }
    return labels.get(value, value or "未知")


def list_publish_accounts(connection=None, *, include_disabled: bool = True) -> list[dict]:
    conn = _conn(connection)
    if not isinstance(include_disabled, bool):
        raise ValueError("include_disabled must be a boolean")
    query = "SELECT * FROM publish_accounts"
    if not include_disabled:
        query += " WHERE enabled=1"
    query += " ORDER BY enabled DESC, assignment_count, id"
    return _rows(conn.execute(query).fetchall())


def get_publish_account(account_id: int, connection=None) -> Optional[dict]:
    numeric_id = _positive_int(account_id, "publish account id")
    return _row(_conn(connection).execute(
        "SELECT * FROM publish_accounts WHERE id=?", (numeric_id,)
    ).fetchone())


def create_publish_account(
    dqd_user_id: int,
    user_name: str,
    enabled: bool = True,
    connection=None,
) -> dict:
    numeric_user_id = _positive_int(dqd_user_id, "dqd_user_id")
    normalized_name = _publish_user_name(user_name)
    enabled_value = _enabled_int(enabled)
    conn = _conn(connection)
    now = _now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO publish_accounts
            (dqd_user_id,user_name,enabled,assignment_count,last_assigned_at,created_at,updated_at)
            VALUES (?,?,?,0,NULL,?,?)
            """,
            (numeric_user_id, normalized_name, enabled_value, now, now),
        )
    account = get_publish_account(int(cursor.lastrowid), conn)
    if account is None:  # pragma: no cover - defensive guard
        raise RuntimeError("publish account was not created")
    return account


def update_publish_account(
    account_id: int,
    connection=None,
    *,
    dqd_user_id: Optional[int] = None,
    user_name: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    numeric_account_id = _positive_int(account_id, "publish account id")
    requested_user_id = (
        None if dqd_user_id is None else _positive_int(dqd_user_id, "dqd_user_id")
    )
    requested_user_name = (
        None if user_name is None else _publish_user_name(user_name)
    )
    requested_enabled = None if enabled is None else _enabled_int(enabled)
    conn = _conn(connection)
    with conn:
        # Acquire SQLite's writer lock before checking the last-enabled-account
        # invariant so two concurrent requests cannot both disable an account.
        conn.execute(
            "UPDATE publish_accounts SET id=id WHERE id=?", (numeric_account_id,)
        )
        current_row = conn.execute(
            "SELECT * FROM publish_accounts WHERE id=?", (numeric_account_id,)
        ).fetchone()
        if current_row is None:
            raise ValueError("publish account not found")
        current = dict(current_row)
        values = {
            "dqd_user_id": (
                current["dqd_user_id"]
                if requested_user_id is None
                else requested_user_id
            ),
            "user_name": (
                current["user_name"]
                if requested_user_name is None
                else requested_user_name
            ),
            "enabled": (
                current["enabled"]
                if requested_enabled is None
                else requested_enabled
            ),
        }
        if bool(current["enabled"]) and not bool(values["enabled"]):
            pool_enabled = bool(
                get_setting("publish_account_pool_enabled", False, conn)
            )
            remaining = conn.execute(
                "SELECT 1 FROM publish_accounts WHERE enabled=1 AND id<>? LIMIT 1",
                (numeric_account_id,),
            ).fetchone()
            if pool_enabled and remaining is None:
                raise ValueError("账号池已启用，不能停用最后一个可用账号")
        conn.execute(
            """
            UPDATE publish_accounts
            SET dqd_user_id=?, user_name=?, enabled=?, updated_at=?
            WHERE id=?
            """,
            (
                values["dqd_user_id"],
                values["user_name"],
                values["enabled"],
                _now(),
                current["id"],
            ),
        )
    updated = get_publish_account(int(current["id"]), conn)
    if updated is None:  # pragma: no cover - updates never delete accounts
        raise RuntimeError("publish account disappeared during update")
    return updated


def set_publish_account_pool_enabled(enabled: bool, connection=None) -> bool:
    """Change the master switch while preserving at least one active account."""

    enabled_value = bool(_enabled_int(enabled))
    conn = _conn(connection)
    now = _now()
    with conn:
        # This write serializes the account-count check with account updates.
        conn.execute(
            """
            INSERT OR IGNORE INTO settings (key,value_json,updated_at)
            VALUES ('publish_account_pool_enabled','false',?)
            """,
            (now,),
        )
        if enabled_value:
            active = conn.execute(
                "SELECT 1 FROM publish_accounts WHERE enabled=1 LIMIT 1"
            ).fetchone()
            if active is None:
                raise ValueError("账号池至少需要一个已启用的发布账号")
        conn.execute(
            """
            UPDATE settings SET value_json=?, updated_at=?
            WHERE key='publish_account_pool_enabled'
            """,
            (_json(enabled_value, False), now),
        )
    return enabled_value


def seed_catalog(connection=None, tabs: Iterable[Mapping[str, Any]] = (),
                 sources: Iterable[Any] = ()) -> dict:
    """Seed missing catalog rows without overwriting operator changes."""

    conn = _conn(connection)
    tab_count = source_count = 0
    now = _now()
    with conn:
        for tab in tabs:
            backend_id = int(tab["backend_tab_id"] if "backend_tab_id" in tab else tab["id"])
            name = str(tab["name"]).strip()
            if not name:
                continue
            existing_backend = conn.execute(
                "SELECT id FROM tabs WHERE backend_tab_id=?", (backend_id,)
            ).fetchone()
            existing_name = conn.execute(
                "SELECT id FROM tabs WHERE name=?", (name,)
            ).fetchone()
            if existing_backend or existing_name:
                # Names and backend IDs are operator-editable. Preserve either
                # form of an existing row instead of reapplying seed metadata.
                conn.execute("UPDATE tabs SET updated_at=? WHERE id=?", (now, (existing_backend or existing_name)["id"]))
                tab_count += 1
                continue
            cursor = conn.execute(
                """
                INSERT INTO tabs (backend_tab_id, name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(backend_tab_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (backend_id, name, int(tab.get("enabled", 1)), now, now),
            )
            tab_count += cursor.rowcount if cursor.rowcount > 0 else 0
        for source in sources:
            if isinstance(source, Mapping):
                code = source.get("code")
                display_name = source.get("display_name") or source.get("name") or code
                enabled = source.get("enabled", 0)
                tab_id = source.get("tab_id")
                source_tab_ids = source.get("tab_ids")
            else:
                code, display_name = source[0], source[1]
                enabled, tab_id = 0, None
                source_tab_ids = None
            code = str(code or "").strip()
            display_name = str(display_name or code).strip()
            if not code:
                continue
            cursor = conn.execute(
                """
                INSERT INTO sources (code, display_name, enabled, tab_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (code, display_name, int(bool(enabled)), tab_id, now, now),
            )
            source_count += cursor.rowcount if cursor.rowcount > 0 else 0
            if source_tab_ids is not None or tab_id is not None:
                source_row = conn.execute("SELECT id FROM sources WHERE code=?", (code,)).fetchone()
                requested = _tab_ids(source_tab_ids if source_tab_ids is not None else [tab_id])
                if source_row and requested:
                    _replace_source_tabs(source_row["id"], requested, conn)
    return {"tabs": tab_count, "sources": source_count}


def list_tabs(connection=None, include_disabled: bool = True) -> list[dict]:
    conn = _conn(connection)
    query = "SELECT * FROM tabs"
    params = ()
    if not include_disabled:
        query += " WHERE enabled=1"
    query += " ORDER BY name COLLATE NOCASE, id"
    return _rows(conn.execute(query, params).fetchall())


def get_tab(tab_id: int, connection=None) -> Optional[dict]:
    return _row(_conn(connection).execute("SELECT * FROM tabs WHERE id=?", (tab_id,)).fetchone())


def get_tab_by_backend_id(backend_tab_id: int, connection=None) -> Optional[dict]:
    return _row(_conn(connection).execute(
        "SELECT * FROM tabs WHERE backend_tab_id=?", (int(backend_tab_id),)
    ).fetchone())


def create_tab(name: str, backend_tab_id: int, enabled: bool = True,
               connection=None, *, publish_mode: int = 0,
               fallback_litpic: str = "") -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("tab name is required")
    conn = _conn(connection)
    mode = _publish_mode_int(publish_mode)
    fallback = _fallback_litpic(fallback_litpic)
    now = _now()
    with conn:
        cursor = conn.execute(
            "INSERT INTO tabs (backend_tab_id, name, enabled, publish_mode, fallback_litpic, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (int(backend_tab_id), name, int(bool(enabled)), mode, fallback, now, now),
        )
    return get_tab(cursor.lastrowid, conn)


def update_tab(tab_id: int, connection=None, *, name: Optional[str] = None,
               backend_tab_id: Optional[int] = None, enabled: Optional[bool] = None,
               publish_mode: Optional[int] = None,
               fallback_litpic: Optional[str] = None) -> dict:
    conn = _conn(connection)
    current = get_tab(tab_id, conn)
    if current is None:
        raise ValueError("tab not found")
    values = {
        "name": str(name).strip() if name is not None else current["name"],
        "backend_tab_id": int(backend_tab_id) if backend_tab_id is not None else current["backend_tab_id"],
        "enabled": int(bool(enabled)) if enabled is not None else current["enabled"],
        "publish_mode": (
            current.get("publish_mode", 0)
            if publish_mode is None
            else _publish_mode_int(publish_mode)
        ),
        "fallback_litpic": (
            current.get("fallback_litpic", "")
            if fallback_litpic is None
            else _fallback_litpic(fallback_litpic)
        ),
        "updated_at": _now(),
    }
    if not values["name"]:
        raise ValueError("tab name is required")
    if current["enabled"] and not values["enabled"]:
        _assert_tab_can_disable(tab_id, conn)
    with conn:
        conn.execute(
            "UPDATE tabs SET name=?, backend_tab_id=?, enabled=?, publish_mode=?, fallback_litpic=?, updated_at=? WHERE id=?",
            (values["name"], values["backend_tab_id"], values["enabled"],
             values["publish_mode"], values["fallback_litpic"], values["updated_at"], tab_id),
        )
    return get_tab(tab_id, conn)


def get_tab_publish_mode(tab_id: int, connection=None) -> Optional[int]:
    """Return a tab's current draft/publish mode, or ``None`` if missing."""

    row = _conn(connection).execute(
        "SELECT publish_mode FROM tabs WHERE id=?", (int(tab_id),)
    ).fetchone()
    return None if row is None else _publish_mode_int(row["publish_mode"])


def _assert_tab_can_disable(tab_id: int, conn) -> None:
    row = conn.execute(
        """
        SELECT 1
        FROM sources s LEFT JOIN source_tabs st ON st.source_id=s.id
        WHERE s.enabled=1 AND (st.tab_id=? OR (st.tab_id IS NULL AND s.tab_id=?))
        LIMIT 1
        """,
        (tab_id, tab_id),
    ).fetchone()
    if row:
        raise ValueError("cannot disable a tab referenced by an enabled source")


def disable_tab(tab_id: int, connection=None) -> dict:
    return update_tab(tab_id, connection, enabled=False)


def enable_tab(tab_id: int, connection=None) -> dict:
    return update_tab(tab_id, connection, enabled=True)


def delete_tab(tab_id: int, connection=None) -> None:
    conn = _conn(connection)
    _assert_tab_can_disable(tab_id, conn)
    with conn:
        conn.execute("DELETE FROM tabs WHERE id=?", (tab_id,))


def list_sources(connection=None, *, include_disabled: bool = True,
                 tab_id: Optional[int] = None) -> list[dict]:
    conn = _conn(connection)
    clauses, params = [], []
    if not include_disabled:
        clauses.append("s.enabled=1")
    if tab_id is not None:
        clauses.append("EXISTS (SELECT 1 FROM source_tabs st WHERE st.source_id=s.id AND st.tab_id=?)")
        params.append(tab_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        """
        SELECT s.*, t.name AS tab_name, t.backend_tab_id
        FROM sources s LEFT JOIN tabs t ON t.id=s.tab_id
        """ + where + " ORDER BY s.display_name COLLATE NOCASE, s.code",
        params,
    ).fetchall()
    result = _rows(rows)
    for item in result:
        item["name"] = item["display_name"]
        _decorate_tabs(item, _tabs_for_source_id(item["id"], conn))
    return result


def get_source(code: str, connection=None) -> Optional[dict]:
    conn = _conn(connection)
    item = _row(conn.execute(
        """
        SELECT s.*, t.name AS tab_name, t.backend_tab_id
        FROM sources s LEFT JOIN tabs t ON t.id=s.tab_id WHERE s.code=?
        """, (str(code).strip(),)
    ).fetchone())
    if item is not None:
        item["name"] = item["display_name"]
        _decorate_tabs(item, _tabs_for_source_id(item["id"], conn))
    return item


def get_source_publish_mode(source: str, connection=None) -> Optional[int]:
    """Return the legacy source-level mode for compatibility callers."""

    row = _conn(connection).execute(
        "SELECT publish_mode FROM sources WHERE code=?", (str(source).strip(),)
    ).fetchone()
    return None if row is None else _publish_mode_int(row["publish_mode"])


def get_source_publish_mode_override(source: str, connection=None) -> Optional[int]:
    """Return an explicit source override, or ``None`` when following tabs."""

    row = _conn(connection).execute(
        "SELECT publish_mode_override FROM sources WHERE code=?",
        (str(source).strip(),),
    ).fetchone()
    if row is None or row["publish_mode_override"] is None:
        return None
    return _publish_mode_int(row["publish_mode_override"], "publish_mode_override")


def create_source(
    code: str,
    display_name: str,
    *,
    tab_ids: Iterable[int] = (),
    enabled: bool = False,
    connection=None,
    publish_mode_override: Optional[int] = None,
) -> dict:
    """Create a manually configured source and its optional tab mappings.

    New sources are disabled by default so entering a source code cannot
    unexpectedly start fetching materials before its operator has checked the
    tab mapping.  Enabling a source still requires at least one enabled tab.
    """

    normalized_code = _source_code(code)
    normalized_name = _source_display_name(display_name, normalized_code)
    enabled_value = _enabled_int(enabled)
    conn = _conn(connection)
    override = (
        None if publish_mode_override is None
        else _publish_mode_int(publish_mode_override, "publish_mode_override")
    )
    ids = _validate_tabs(tab_ids, conn, require_enabled=bool(enabled_value))
    if enabled_value and not ids:
        raise ValueError("an enabled source must be assigned to a tab (at least one enabled tab)")
    now = _now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO sources
            (code, display_name, enabled, tab_id, publish_mode_override, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (normalized_code, normalized_name, enabled_value, ids[0] if ids else None,
             override, now, now),
        )
        _replace_source_tabs(cursor.lastrowid, ids, conn)
    source = get_source(normalized_code, conn)
    if source is None:  # pragma: no cover - defensive guard
        raise RuntimeError("source was not created")
    return source


def get_source_tabs(code: str, connection=None) -> list[dict]:
    conn = _conn(connection)
    row = conn.execute("SELECT id FROM sources WHERE code=?", (str(code).strip(),)).fetchone()
    if row is None:
        raise ValueError("source not found")
    return _tabs_for_source_id(row["id"], conn)


def _validate_tabs(tab_ids: Iterable[int], conn, *, require_enabled: bool = False) -> list[int]:
    ids = _tab_ids(tab_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, enabled FROM tabs WHERE id IN ({placeholders})", ids
    ).fetchall()
    found = {int(row["id"]): bool(row["enabled"]) for row in rows}
    if any(tab_id not in found for tab_id in ids):
        raise ValueError("tab not found")
    if require_enabled and any(not found[tab_id] for tab_id in ids):
        raise ValueError("enabled source must use enabled tabs")
    return ids


def _replace_source_tabs(source_id: int, tab_ids: Iterable[int], conn) -> list[int]:
    ids = _validate_tabs(tab_ids, conn)
    conn.execute("DELETE FROM source_tabs WHERE source_id=?", (source_id,))
    conn.executemany(
        "INSERT INTO source_tabs (source_id, tab_id, sort_order) VALUES (?,?,?)",
        [(source_id, tab_id, index) for index, tab_id in enumerate(ids)],
    )
    conn.execute(
        "UPDATE sources SET tab_id=? WHERE id=?",
        (ids[0] if ids else None, source_id),
    )
    return ids


def set_source_tabs(code: str, tab_ids: Iterable[int], connection=None) -> dict:
    conn = _conn(connection)
    current = get_source(code, conn)
    if current is None:
        raise ValueError("source not found")
    ids = _validate_tabs(tab_ids, conn, require_enabled=bool(current["enabled"]))
    if current["enabled"] and not ids:
        raise ValueError("an enabled source must be assigned to a tab (at least one enabled tab)")
    with conn:
        _replace_source_tabs(current["id"], ids, conn)
        conn.execute("UPDATE sources SET updated_at=? WHERE id=?", (_now(), current["id"]))
    return get_source(code, conn)


def update_source(code: str, connection=None, *, display_name: Optional[str] = None,
                  enabled: Optional[bool] = None, tab_id: Optional[int] = None,
                  clear_tab: bool = False, tab_ids: Any = _UNSET,
                  publish_mode: Optional[int] = None,
                  publish_mode_override: Any = _UNSET) -> dict:
    conn = _conn(connection)
    current = get_source(code, conn)
    if current is None:
        raise ValueError("source not found")
    if tab_ids is not _UNSET:
        new_tab_ids = _tab_ids(tab_ids)
    elif clear_tab:
        new_tab_ids = []
    elif tab_id is not None:
        new_tab_ids = [tab_id]
    else:
        new_tab_ids = list(current["tab_ids"])
    new_enabled = int(bool(enabled)) if enabled is not None else current["enabled"]
    new_tab_ids = _validate_tabs(new_tab_ids, conn, require_enabled=bool(new_enabled))
    if new_enabled and not new_tab_ids:
        raise ValueError("an enabled source must be assigned to a tab (at least one enabled tab)")
    name = str(display_name).strip() if display_name is not None else current["display_name"]
    if not name:
        raise ValueError("source display_name is required")
    new_publish_mode = (
        current.get("publish_mode", 0)
        if publish_mode is None
        else _publish_mode_int(publish_mode)
    )
    if publish_mode_override is _UNSET:
        new_publish_mode_override = current.get("publish_mode_override")
    elif publish_mode_override is None:
        new_publish_mode_override = None
    else:
        new_publish_mode_override = _publish_mode_int(
            publish_mode_override, "publish_mode_override"
        )
    # The old parameter remains a supported source-level switch. Treating it
    # as an explicit override preserves behavior for existing API callers.
    if publish_mode is not None and publish_mode_override is _UNSET:
        new_publish_mode_override = new_publish_mode
    with conn:
        conn.execute(
            """
            UPDATE sources
            SET display_name=?, enabled=?, tab_id=?, publish_mode=? ,
                publish_mode_override=?, updated_at=?
            WHERE code=?
            """,
            (name, new_enabled, new_tab_ids[0] if new_tab_ids else None,
             new_publish_mode, new_publish_mode_override, _now(), str(code).strip()),
        )
        _replace_source_tabs(current["id"], new_tab_ids, conn)
    return get_source(code, conn)


def set_source_enabled(code: str, enabled: bool, connection=None) -> dict:
    return update_source(code, connection, enabled=enabled)


def _decorate_event_tab_rule(row) -> Optional[dict]:
    item = _row(row)
    if item is None:
        return None
    item["configured"] = item.get("tab_id") is not None
    return item


def get_event_tab_rule(rule_id: int, connection=None) -> Optional[dict]:
    return _decorate_event_tab_rule(_conn(connection).execute(
        """
        SELECT r.*, t.name AS tab_name, t.backend_tab_id,
               t.enabled AS tab_enabled, s.display_name AS source_display_name
        FROM event_tab_rules r
        LEFT JOIN tabs t ON t.id=r.tab_id
        LEFT JOIN sources s ON s.code=r.source_code
        WHERE r.id=?
        """,
        (_positive_int(rule_id, "rule id"),),
    ).fetchone())


def list_event_tab_rules(connection=None, *, marker_type: Optional[str] = None,
                         pending_only: bool = False,
                         search: Optional[str] = None,
                         source_code: Any = _UNSET) -> list[dict]:
    conn = _conn(connection)
    clauses: list[str] = []
    params: list[Any] = []
    if marker_type not in (None, ""):
        clauses.append("r.marker_type=?")
        params.append(_event_marker_type(marker_type))
    if pending_only:
        clauses.append("r.tab_id IS NULL")
    if source_code is not _UNSET:
        normalized_source = _event_rule_source_code(source_code, conn)
        if normalized_source is None:
            clauses.append("r.source_code IS NULL")
        else:
            clauses.append("r.source_code=?")
            params.append(normalized_source)
    search_text = str(search or "").strip()
    if search_text:
        clauses.append(
            "(r.marker_code LIKE ? OR t.name LIKE ? OR r.sample_source LIKE ? "
            "OR r.source_code LIKE ? OR s.display_name LIKE ?)"
        )
        pattern = f"%{search_text[:100]}%"
        params.extend((pattern, pattern, pattern, pattern, pattern))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        """
        SELECT r.*, t.name AS tab_name, t.backend_tab_id,
               t.enabled AS tab_enabled, s.display_name AS source_display_name
        FROM event_tab_rules r
        LEFT JOIN tabs t ON t.id=r.tab_id
        LEFT JOIN sources s ON s.code=r.source_code
        """ + where + """
        ORDER BY CASE WHEN r.tab_id IS NULL THEN 0 ELSE 1 END,
                 r.marker_type, r.marker_code,
                 CASE WHEN r.source_code IS NULL THEN 0 ELSE 1 END,
                 r.source_code, r.id
        """,
        params,
    ).fetchall()
    return [_decorate_event_tab_rule(row) for row in rows]


def _event_rule_tab_id(value: Any, conn) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("tab_id must be an integer or null")
    try:
        tab_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("tab_id must be an integer or null") from None
    return _validate_tabs([tab_id], conn)[0]


def create_event_tab_rule(marker_type: Any, marker_code: Any,
                          tab_id: Any = None, enabled: Any = True,
                          connection=None, *, source_code: Any = None,
                          publish_mode_override: Any = None) -> dict:
    conn = _conn(connection)
    normalized_type = _event_marker_type(marker_type)
    normalized_code = _event_marker_code(marker_code)
    target_tab_id = _event_rule_tab_id(tab_id, conn)
    enabled_value = _enabled_int(enabled)
    normalized_source = _event_rule_source_code(source_code, conn)
    publish_override = _event_rule_publish_mode(publish_mode_override)
    now = _now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO event_tab_rules
            (source_code, marker_type, marker_code, tab_id,
             publish_mode_override, enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (normalized_source, normalized_type, normalized_code, target_tab_id,
             publish_override, enabled_value, now, now),
        )
    result = get_event_tab_rule(cursor.lastrowid, conn)
    if result is None:  # pragma: no cover - row was just inserted
        raise RuntimeError("event tab rule was not created")
    return result


def update_event_tab_rule(rule_id: int, connection=None, *,
                          marker_type: Any = None, marker_code: Any = None,
                          tab_id: Any = _UNSET, enabled: Any = None,
                          source_code: Any = _UNSET,
                          publish_mode_override: Any = _UNSET) -> dict:
    conn = _conn(connection)
    current = get_event_tab_rule(rule_id, conn)
    if current is None:
        raise ValueError("event tab rule not found")
    normalized_type = (
        current["marker_type"]
        if marker_type is None else _event_marker_type(marker_type)
    )
    normalized_code = (
        current["marker_code"]
        if marker_code is None else _event_marker_code(marker_code)
    )
    target_tab_id = (
        current["tab_id"]
        if tab_id is _UNSET else _event_rule_tab_id(tab_id, conn)
    )
    enabled_value = (
        current["enabled"] if enabled is None else _enabled_int(enabled)
    )
    normalized_source = (
        current.get("source_code")
        if source_code is _UNSET
        else _event_rule_source_code(source_code, conn)
    )
    publish_override = (
        current.get("publish_mode_override")
        if publish_mode_override is _UNSET
        else _event_rule_publish_mode(publish_mode_override)
    )
    with conn:
        conn.execute(
            """
            UPDATE event_tab_rules
            SET source_code=?, marker_type=?, marker_code=?, tab_id=?,
                publish_mode_override=?, enabled=?, updated_at=?
            WHERE id=?
            """,
            (normalized_source, normalized_type, normalized_code, target_tab_id,
             publish_override, enabled_value, _now(), current["id"]),
        )
    result = get_event_tab_rule(current["id"], conn)
    if result is None:  # pragma: no cover - row was just updated
        raise RuntimeError("event tab rule was not updated")
    return result


def _observe_event_marker(marker_type: str, marker_code: str,
                          source: str, conn, *, create_pending: bool) -> None:
    if not marker_code:
        return
    now = _now()
    if create_pending:
        conn.execute(
            """
            INSERT INTO event_tab_rules
            (source_code, marker_type, marker_code, tab_id,
             publish_mode_override, enabled, first_seen_at,
             last_seen_at, sample_source, created_at, updated_at)
            VALUES (NULL,?,?,NULL,NULL,1,?,?,?,?,?)
            ON CONFLICT(marker_type, marker_code) WHERE source_code IS NULL
            DO UPDATE SET
                first_seen_at=COALESCE(event_tab_rules.first_seen_at,
                                       excluded.first_seen_at),
                last_seen_at=excluded.last_seen_at,
                sample_source=CASE
                    WHEN event_tab_rules.sample_source='' THEN excluded.sample_source
                    ELSE event_tab_rules.sample_source
                END
            """,
            (marker_type, marker_code, now, now, str(source or "")[:100], now, now),
        )
    conn.execute(
        """
        UPDATE event_tab_rules
        SET first_seen_at=COALESCE(first_seen_at, ?), last_seen_at=?,
            sample_source=CASE WHEN sample_source='' THEN ? ELSE sample_source END
        WHERE marker_type=? AND marker_code=?
          AND (source_code IS NULL OR source_code=?)
        """,
        (now, now, str(source or "")[:100], marker_type, marker_code, source),
    )


def _find_event_tab_rule(marker_type: str, marker_code: str,
                         source: str, conn) -> Optional[dict]:
    if not marker_code:
        return None
    return _decorate_event_tab_rule(conn.execute(
        """
        SELECT r.*, t.name AS tab_name, t.backend_tab_id,
               t.enabled AS tab_enabled, s.display_name AS source_display_name
        FROM event_tab_rules r
        JOIN tabs t ON t.id=r.tab_id AND t.enabled=1
        LEFT JOIN sources s ON s.code=r.source_code
        WHERE r.marker_type=? AND r.marker_code=? AND r.enabled=1
          AND (r.source_code=? OR r.source_code IS NULL)
        ORDER BY CASE WHEN r.source_code=? THEN 0 ELSE 1 END, r.id
        LIMIT 1
        """,
        (marker_type, marker_code, source, source),
    ).fetchone())


def _resolve_material_tabs(parsed: Mapping[str, Any], source: str,
                           source_tabs: list[dict], conn) -> dict[str, Any]:
    _observe_event_marker(
        "league", str(parsed.get("league") or ""), source, conn,
        create_pending=True,
    )
    _observe_event_marker(
        "team", str(parsed.get("team") or ""), source, conn,
        create_pending=False,
    )
    matched_rule = None
    for marker_type, marker_code in (
        ("team", str(parsed.get("team") or "")),
        ("league", str(parsed.get("league") or "")),
    ):
        candidate = _find_event_tab_rule(
            marker_type, marker_code, source, conn
        )
        if candidate is not None:
            matched_rule = candidate
            break

    source_tab_ids = [int(tab["id"]) for tab in source_tabs]
    if matched_rule is None:
        selected_tab_ids = source_tab_ids
        match_type = "source"
    else:
        # Backend tab 58 is the only generic source column. Keep it only when
        # the source already has it; replace every source event column.
        selected_tab_ids = [
            int(tab["id"])
            for tab in source_tabs
            if int(tab.get("backend_tab_id") or 0) == 58
        ]
        target_tab_id = int(matched_rule["tab_id"])
        if target_tab_id not in selected_tab_ids:
            selected_tab_ids.append(target_tab_id)
        match_type = str(matched_rule["marker_type"])
    return {
        "selected_tab_ids": selected_tab_ids,
        "source_tab_ids": source_tab_ids,
        "match_type": match_type,
        "rule_id": None if matched_rule is None else int(matched_rule["id"]),
    }


def _normalise_material(material: Mapping[str, Any]) -> dict:
    source = str(material.get("source") or "").strip()
    source_url = str(material.get("source_url") or "").strip()
    if not source or not source_url:
        raise ValueError("source and source_url are required")
    title = (
        material.get("translate_title")
        or material.get("title")
        or material.get("title_original")
        or material.get("title_current")
        or ""
    )
    body = (
        material.get("translate_body")
        or material.get("body_html")
        or material.get("body")
        or material.get("body_original")
        or material.get("body_current")
        or ""
    )
    litpic = material.get("dqd_litpic", material.get("litpic", material.get("cover_url", ""))) or ""
    channels = _normalise_channels(material.get("channels", []))
    raw = material.get("raw", material.get("raw_payload", material.get("raw_json", material)))
    raw = raw if isinstance(raw, Mapping) else {}
    material_user_name = (
        material.get("material_user_name")
        or raw.get("user_name")
        or raw.get("username")
        or material.get("user_name")
        or material.get("username")
        or ""
    )
    parsed_user_name = parse_material_user_name(material_user_name)
    return {
        "source": source,
        "source_url": source_url,
        "origin_key": source_origin_key(source, source_url),
        "upstream_archive_id": int(material.get("archive_id") or material.get("upstream_archive_id") or 0),
        "title_original": str(material.get("title_original", title) or ""),
        "title_final": str(material.get("title_final", title) or ""),
        # Keep the upstream HTML in raw_json; the stored body retains visible
        # content while clickable anchor wrappers are removed.
        "body_html": remove_clickable_links(str(body)),
        "litpic": str(litpic),
        "channels": channels,
        "raw": raw,
        "material_user_name": parsed_user_name["raw"],
        "route_league": parsed_user_name["league"],
        "route_team": parsed_user_name["team"],
        "parsed_user_name": parsed_user_name,
    }


def _normalise_channels(channels: Any) -> list[int]:
    if isinstance(channels, str):
        channels = _loads(channels, [])
    if not isinstance(channels, (list, tuple, set)):
        return []
    result = []
    for value in channels:
        text = str(value).strip()
        if text.lstrip("-").isdigit():
            number = int(text)
            if number not in result:
                result.append(number)
    return filter_blocked_channels(result)


def upsert_material(material: Mapping[str, Any], connection=None) -> dict:
    """Insert/update one upstream material and return ``{article, created}``.

    Re-fetching an existing key refreshes content and ``last_seen_at`` but does
    not reset workflow status or overwrite manually edited final content.
    """

    conn = _conn(connection)
    item = _normalise_material(material)
    now = _now()
    existing = None
    if item["origin_key"]:
        existing = conn.execute(
            "SELECT * FROM articles WHERE lower(source)=lower(?) AND origin_key=?",
            (item["source"], item["origin_key"]),
        ).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT * FROM articles WHERE source=? AND source_url=?",
            (item["source"], item["source_url"]),
        ).fetchone()
    source_config = conn.execute(
        "SELECT id, tab_id FROM sources WHERE code=?", (item["source"],)
    ).fetchone()
    source_tabs: list[dict] = []
    source_tab_ids = []
    source_tab_id = source_config["tab_id"] if source_config else None
    if source_config:
        source_tabs = _tabs_for_source_id(source_config["id"], conn)
        source_tab_ids = [tab["id"] for tab in source_tabs]
        if not source_tab_ids and source_tab_id is not None:
            source_tab_ids = [source_tab_id]
            legacy_tab = get_tab(source_tab_id, conn)
            source_tabs = [legacy_tab] if legacy_tab is not None else []
    if existing is None:
        routing = _resolve_material_tabs(
            item["parsed_user_name"], item["source"], source_tabs, conn
        )
        selected_tab_ids = routing["selected_tab_ids"]
        selected_tab_id = selected_tab_ids[0] if selected_tab_ids else None
        # Keep one idempotency key for the article for its entire lifecycle.
        # It is generated before the insert and never replaced on re-ingest.
        client_request_id = uuid.uuid4().hex
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO articles
                    (source, source_url, origin_key, upstream_archive_id, client_request_id,
                     title_original, title_final,
                     body_html, litpic, channels_json, tab_id,
                     material_user_name, route_league, route_team,
                     route_match_type, route_rule_id,
                     level, status, quality_json, raw_json,
                     created_at, updated_at, last_seen_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (item["source"], item["source_url"], item["origin_key"], item["upstream_archive_id"],
                     client_request_id,
                     item["title_original"], item["title_final"], item["body_html"],
                     item["litpic"], _json(item["channels"], []), selected_tab_id,
                     item["material_user_name"], item["route_league"], item["route_team"],
                     routing["match_type"], routing["rule_id"], "B", "RECEIVED",
                     "{}", _json(item["raw"], {}), now, now, now),
                )
                article_id = cursor.lastrowid
                _replace_article_tabs(article_id, selected_tab_ids, conn)
                conn.execute(
                    """
                    INSERT INTO article_events
                    (article_id, from_status, to_status, event_type, message, payload_json, created_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        article_id,
                        None,
                        "RECEIVED",
                        "MATERIAL_RECEIVED",
                        "素材接口返回并已入库",
                        _json({
                            "source": item["source"],
                            "source_url": item["source_url"],
                            "routing": {
                                "material_user_name": item["material_user_name"],
                                "league": item["route_league"],
                                "team": item["route_team"],
                                "match_type": routing["match_type"],
                                "rule_id": routing["rule_id"],
                                "source_tab_ids": routing["source_tab_ids"],
                                "selected_tab_ids": selected_tab_ids,
                            },
                        }, {}),
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            # Another ingestion may have inserted the same exact source item
            # after our lookup. Only recover when that identity now exists.
            if item["origin_key"]:
                raced = conn.execute(
                    "SELECT id FROM articles WHERE lower(source)=lower(?) AND origin_key=?",
                    (item["source"], item["origin_key"]),
                ).fetchone()
            else:
                raced = conn.execute(
                    "SELECT id FROM articles WHERE source=? AND source_url=?",
                    (item["source"], item["source_url"]),
                ).fetchone()
            if raced is None:
                raise
            return upsert_material(material, conn)
        return {"article": get_article(article_id, conn), "created": True}

    with conn:
        conn.execute(
            """
            UPDATE articles SET upstream_archive_id=?, title_original=?,
                title_final=CASE WHEN status IN ('RECEIVED','ERROR')
                    THEN ? ELSE title_final END,
                body_html=CASE WHEN status IN ('RECEIVED','ERROR')
                    THEN ? ELSE body_html END,
                litpic=CASE WHEN status IN ('RECEIVED','ERROR')
                    THEN ? ELSE litpic END,
                channels_json=CASE WHEN status IN ('RECEIVED','ERROR')
                    THEN ? ELSE channels_json END,
                raw_json=?, updated_at=?, last_seen_at=?
            WHERE id=?
            """,
            (item["upstream_archive_id"], item["title_original"], item["title_final"],
             item["body_html"], item["litpic"], _json(item["channels"], []),
             _json(item["raw"], {}), now, now, existing["id"]),
        )
    return {"article": get_article(existing["id"], conn), "created": False}


def get_duplicate_canonical(article_id: int, connection=None) -> Optional[dict]:
    """Return the canonical record when ``article_id`` is an exact source duplicate."""

    conn = _conn(connection)
    current = conn.execute(
        "SELECT source, source_url, origin_key, duplicate_of_article_id FROM articles WHERE id=?",
        (article_id,),
    ).fetchone()
    if current is None:
        raise ValueError("article not found")
    duplicate_of = current["duplicate_of_article_id"]
    if duplicate_of is not None:
        return get_article(int(duplicate_of), conn)

    key = current["origin_key"] or source_origin_key(current["source"], current["source_url"])
    if not key:
        return None
    canonical = conn.execute(
        """
        SELECT id FROM articles
        WHERE lower(source)=lower(?) AND origin_key=? AND id<>?
        LIMIT 1
        """,
        (current["source"], key, article_id),
    ).fetchone()
    return get_article(int(canonical["id"]), conn) if canonical else None


def assign_article_tab(article_id: int, tab_id: Optional[int], connection=None) -> dict:
    return assign_article_tabs(article_id, [] if tab_id is None else [tab_id], connection)


def get_article_tabs(article_id: int, connection=None) -> list[dict]:
    return _tabs_for_article_id(article_id, _conn(connection))


def _replace_article_tabs(article_id: int, tab_ids: Iterable[int], conn) -> list[int]:
    ids = _validate_tabs(tab_ids, conn)
    conn.execute("DELETE FROM article_tabs WHERE article_id=?", (article_id,))
    conn.executemany(
        "INSERT INTO article_tabs (article_id, tab_id, sort_order) VALUES (?,?,?)",
        [(article_id, tab_id, index) for index, tab_id in enumerate(ids)],
    )
    conn.execute(
        "UPDATE articles SET tab_id=? WHERE id=?",
        (ids[0] if ids else None, article_id),
    )
    return ids


def assign_article_tabs(article_id: int, tab_ids: Iterable[int], connection=None) -> dict:
    conn = _conn(connection)
    if get_article(article_id, conn) is None:
        raise ValueError("article not found")
    ids = _validate_tabs(tab_ids, conn)
    with conn:
        _replace_article_tabs(article_id, ids, conn)
        conn.execute("UPDATE articles SET updated_at=? WHERE id=?", (_now(), article_id))
    return get_article(article_id, conn)


def resolve_article_publish_mode(article_id: int, connection=None) -> dict:
    """Resolve the mode to use for an article from its mapped tabs.

    The returned mapping always contains ``publish_mode`` (``None`` when
    mappings conflict), ``conflict`` and the participating tab IDs/modes.
    Once an article has a snapshot, that snapshot wins and the current tab
    settings are reported only as diagnostic metadata.  This lets retries and
    result-confirmation workers keep the mode used by the first request.
    """

    numeric_article_id = _positive_int(article_id, "article id")
    conn = _conn(connection)
    row = conn.execute(
        """
        SELECT id, source, route_rule_id, publish_mode, publish_mode_decided_at
        FROM articles WHERE id=?
        """,
        (numeric_article_id,),
    ).fetchone()
    if row is None:
        raise ValueError("article not found")

    source_row = conn.execute(
        "SELECT publish_mode, publish_mode_override FROM sources WHERE code=?",
        (str(row["source"] or "").strip(),),
    ).fetchone()
    source_override = None
    if source_row is not None and source_row["publish_mode_override"] is not None:
        source_override = _publish_mode_int(
            source_row["publish_mode_override"], "publish_mode_override"
        )

    route_rule = None
    rule_override = None
    if row["route_rule_id"] is not None:
        route_rule = conn.execute(
            """
            SELECT id, source_code, marker_type, marker_code,
                   publish_mode_override
            FROM event_tab_rules
            WHERE id=? AND enabled=1 AND tab_id IS NOT NULL
            """,
            (int(row["route_rule_id"]),),
        ).fetchone()
        if route_rule is not None and route_rule["publish_mode_override"] is not None:
            rule_override = _publish_mode_int(
                route_rule["publish_mode_override"], "publish_mode_override"
            )

    tabs = _tabs_for_article_id(numeric_article_id, conn)
    tab_ids = [int(tab["id"]) for tab in tabs]
    tab_modes = [_publish_mode_int(tab.get("publish_mode", 0)) for tab in tabs]
    unique_modes = sorted(set(tab_modes))
    tab_conflict = len(unique_modes) > 1
    if rule_override is not None:
        configured_mode = rule_override
    elif source_override is not None:
        configured_mode = source_override
    else:
        configured_mode = unique_modes[0] if len(unique_modes) == 1 else (
            0 if not tab_modes else None
        )
    snapshot = (
        None if row["publish_mode"] is None
        else _publish_mode_int(row["publish_mode"])
    )
    return {
        "article_id": numeric_article_id,
        "publish_mode": snapshot if snapshot is not None else configured_mode,
        "mode": snapshot if snapshot is not None else configured_mode,
        "snapshot": snapshot,
        "publish_mode_decided_at": row["publish_mode_decided_at"],
        "source": row["source"],
        "source_publish_mode": (
            None if source_row is None or source_row["publish_mode"] is None
            else _publish_mode_int(source_row["publish_mode"])
        ),
        "source_publish_mode_override": source_override,
        "route_rule_id": row["route_rule_id"],
        "route_rule_publish_mode_override": rule_override,
        "route_rule_source_code": (
            None if route_rule is None else route_rule["source_code"]
        ),
        "tab_ids": tab_ids,
        "tab_modes": tab_modes,
        "tab_conflict": tab_conflict,
        "conflict": (
            tab_conflict and rule_override is None and source_override is None
        )
        if snapshot is None else False,
        "configured_mode": configured_mode,
    }


def get_article_publish_mode(article_id: int, connection=None) -> Optional[int]:
    """Return a resolved article mode, or ``None`` for a missing/conflict case."""

    try:
        result = resolve_article_publish_mode(article_id, connection)
    except ValueError as exc:
        if str(exc) != "article not found":
            raise
        return None
    return None if result["conflict"] else result["publish_mode"]


def set_article_publish_mode(article_id: int, publish_mode: int, connection=None,
                             *, decided_at: Optional[str] = None) -> dict:
    """Persist an article's mode snapshot exactly once.

    A second caller may repeat the same value idempotently; changing a
    previously captured value is rejected so an in-flight retry cannot switch
    from publish to draft (or vice versa).
    """

    numeric_article_id = _positive_int(article_id, "article id")
    mode = _publish_mode_int(publish_mode)
    conn = _conn(connection)
    current = get_article(numeric_article_id, conn)
    if current is None:
        raise ValueError("article not found")
    existing = current.get("publish_mode")
    if existing is not None:
        if _publish_mode_int(existing) != mode:
            raise ValueError("article publish_mode is already fixed")
        return current
    timestamp = str(decided_at or _now())
    with conn:
        conn.execute(
            """
            UPDATE articles
            SET publish_mode=?, publish_mode_decided_at=?, updated_at=?
            WHERE id=? AND publish_mode IS NULL
            """,
            (mode, timestamp, _now(), numeric_article_id),
        )
    updated = get_article(numeric_article_id, conn)
    if updated is None:  # pragma: no cover - row was just read
        raise ValueError("article not found")
    if updated.get("publish_mode") is None:
        raise RuntimeError("article publish_mode snapshot was not persisted")
    if _publish_mode_int(updated["publish_mode"]) != mode:
        raise ValueError("article publish_mode is already fixed")
    return updated


def ensure_article_publish_mode(article_id: int, connection=None) -> dict:
    """Atomically capture the effective mode before the first submit.

    A routed event-rule override takes precedence over the source override,
    which takes precedence over tab settings. Without either override, an
    article mapped to multiple tabs with different modes is rejected and no
    snapshot is written.
    """

    numeric_article_id = _positive_int(article_id, "article id")
    conn = _conn(connection)
    with conn:
        # Acquire the writer lock before reading tab settings. Tab updates and
        # this snapshot therefore have a deterministic ordering in SQLite.
        conn.execute(
            "UPDATE articles SET id=id WHERE id=? AND publish_mode IS NULL",
            (numeric_article_id,),
        )
        current = conn.execute(
            "SELECT publish_mode FROM articles WHERE id=?", (numeric_article_id,)
        ).fetchone()
        if current is None:
            raise ValueError("article not found")
        if current["publish_mode"] is not None:
            result = get_article(numeric_article_id, conn)
            if result is None:  # pragma: no cover - row was just read
                raise ValueError("article not found")
            return result
        resolved = resolve_article_publish_mode(numeric_article_id, conn)
        if resolved["conflict"]:
            raise ValueError(
                "article maps to tabs with conflicting publish modes"
            )
        mode = resolved["publish_mode"]
        if mode is None:
            # This should only be possible for malformed legacy rows. Draft is
            # the conservative fallback and preserves existing behavior.
            mode = 0
        now = _now()
        conn.execute(
            "UPDATE articles SET publish_mode=?, publish_mode_decided_at=?, updated_at=? WHERE id=? AND publish_mode IS NULL",
            (_publish_mode_int(mode), now, now, numeric_article_id),
        )
    article = get_article(numeric_article_id, conn)
    if article is None:  # pragma: no cover - row was just updated
        raise ValueError("article not found")
    return article


def update_article_backend_refs(article_id: int, connection=None, *,
                                dqd_source_id: Optional[int] = None,
                                dqd_archive_id: Optional[int] = None,
                                upstream_archive_id: Optional[int] = None,
                                upstream_request_id: Optional[str] = None) -> dict:
    """Persist IDs learned while bridging to the legacy DQD backend."""

    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    values = (
        current["dqd_source_id"] if dqd_source_id is None else int(dqd_source_id),
        current["dqd_archive_id"] if dqd_archive_id is None else int(dqd_archive_id),
        current["upstream_archive_id"] if upstream_archive_id is None else int(upstream_archive_id),
        current.get("upstream_request_id") if upstream_request_id is None else str(upstream_request_id),
        _now(), article_id,
    )
    with conn:
        conn.execute(
            """
            UPDATE articles
            SET dqd_source_id=?, dqd_archive_id=?, upstream_archive_id=?,
                upstream_request_id=?, updated_at=?
            WHERE id=?
            """, values,
        )
    return get_article(article_id, conn)


def mark_published(article_id: int, connection=None, *, dqd_archive_id: Optional[int] = None,
                   message: str = "") -> dict:
    """Mark an article as published after the backend confirms the result."""

    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    archive_id = current["dqd_archive_id"] if dqd_archive_id is None else int(dqd_archive_id)
    now = _now()
    with conn:
        conn.execute(
            "UPDATE articles SET dqd_archive_id=?, status='ALREADY_PUBLISHED', published_at=?, updated_at=?, error=NULL WHERE id=?",
            (archive_id, now, now, article_id),
        )
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, current["status"], "ALREADY_PUBLISHED", "PUBLISHED", message or None,
             _json({"dqd_archive_id": archive_id}, {}), now),
        )
    return get_article(article_id, conn)


def get_article(article_id: int, connection=None) -> Optional[dict]:
    conn = _conn(connection)
    row = conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        WHERE a.id=?
        """,
        (article_id,),
    ).fetchone()
    return _decorate_article(row, conn)


def assign_publish_account(article_id: int, connection=None) -> dict:
    """Assign the least-used enabled account once and retain its snapshot."""

    numeric_article_id = _positive_int(article_id, "article id")
    conn = _conn(connection)
    with conn:
        # A no-op write acquires SQLite's writer lock before account selection.
        # Concurrent callers then re-check the sticky assignment after waiting.
        conn.execute(
            """
            UPDATE articles SET id=id
            WHERE id=? AND publish_account_id IS NULL
            """,
            (numeric_article_id,),
        )
        current = conn.execute(
            """
            SELECT publish_account_id,publish_user_id,publish_user_name,
                   publish_account_assigned_at
            FROM articles WHERE id=?
            """,
            (numeric_article_id,),
        ).fetchone()
        if current is None:
            raise ValueError("article not found")
        if current["publish_account_id"] is not None:
            article = get_article(numeric_article_id, conn)
            if article is None:  # pragma: no cover - row was just read
                raise ValueError("article not found")
            return article

        selected = conn.execute(
            """
            SELECT * FROM publish_accounts
            WHERE enabled=1
            ORDER BY assignment_count ASC, RANDOM()
            LIMIT 1
            """
        ).fetchone()
        if selected is None:
            raise ValueError("没有可用的发布账号，请先启用至少一个账号")

        assigned_at = _now()
        cursor = conn.execute(
            """
            UPDATE articles
            SET publish_account_id=?, publish_user_id=?, publish_user_name=?,
                publish_account_assigned_at=?, updated_at=?
            WHERE id=? AND publish_account_id IS NULL
            """,
            (
                selected["id"],
                selected["dqd_user_id"],
                selected["user_name"],
                assigned_at,
                assigned_at,
                numeric_article_id,
            ),
        )
        if cursor.rowcount != 1:  # pragma: no cover - writer lock prevents this
            raise RuntimeError("publish account assignment conflict")
        conn.execute(
            """
            UPDATE publish_accounts
            SET assignment_count=assignment_count+1,
                last_assigned_at=?, updated_at=?
            WHERE id=?
            """,
            (assigned_at, assigned_at, selected["id"]),
        )

    article = get_article(numeric_article_id, conn)
    if article is None:  # pragma: no cover - row was updated in this transaction
        raise ValueError("article not found")
    return article


def get_article_by_key(source: str, source_url: str, connection=None) -> Optional[dict]:
    conn = _conn(connection)
    row = conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        WHERE a.source=? AND a.source_url=?
        """,
        (source, source_url),
    ).fetchone()
    return _decorate_article(row, conn)


def list_articles(connection=None, *, status: Optional[str] = None,
                  source: Optional[str] = None, tab_id: Optional[int] = None,
                  query: Optional[str] = None, limit: int = 50,
                  offset: int = 0) -> list[dict]:
    conn = _conn(connection)
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if source:
        clauses.append("source=?")
        params.append(source)
    if tab_id is not None:
        clauses.append("EXISTS (SELECT 1 FROM article_tabs at WHERE at.article_id=a.id AND at.tab_id=?)")
        params.append(tab_id)
    if query:
        # 转义 LIKE 通配符，避免用户输入的 % 和 _ 被 SQLite 当作通配符处理
        escaped = str(query).strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(a.title_final LIKE ? ESCAPE '\\' OR a.title_original LIKE ? ESCAPE '\\' OR a.source_url LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows = conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        """ + where + " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [_decorate_article(row, conn) for row in rows]


def count_articles(connection=None, *, status: Optional[str] = None,
                   source: Optional[str] = None, tab_id: Optional[int] = None) -> int:
    conn = _conn(connection)
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if source:
        clauses.append("source=?")
        params.append(source)
    if tab_id is not None:
        clauses.append("EXISTS (SELECT 1 FROM article_tabs at WHERE at.article_id=articles.id AND at.tab_id=?)")
        params.append(tab_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return int(conn.execute("SELECT COUNT(*) FROM articles" + where, params).fetchone()[0])


def direct_publish_report(period_start: str, period_end: str, connection=None) -> dict[str, Any]:
    """Return direct-publish counts for one UTC time window.

    The query keeps the article-to-tab relation used at publish time.  An
    article mapped to multiple tabs is counted once under each tab, while the
    total article count remains deduplicated.
    """

    conn = _conn(connection)
    rows = conn.execute(
        """
        SELECT a.id AS article_id, a.published_tab_names_json,
               a.tab_id AS legacy_tab_id, legacy.name AS legacy_tab_name
        FROM articles a
        LEFT JOIN tabs legacy ON legacy.id=a.tab_id
        WHERE a.status='PUBLISHED'
          AND a.publish_mode=1
          AND a.published_at >= ?
          AND a.published_at < ?
        ORDER BY a.id
        """,
        (str(period_start), str(period_end)),
    ).fetchall()
    article_ids = {int(row["article_id"]) for row in rows}
    counts: dict[str, int] = {}
    for row in rows:
        names = _loads(row["published_tab_names_json"], [])
        if not isinstance(names, list) or not names:
            tabs = _tabs_for_article_id(int(row["article_id"]), conn)
            names = [tab.get("name") for tab in tabs if tab.get("name")]
        if not names and row["legacy_tab_name"]:
            names = [row["legacy_tab_name"]]
        if not names:
            names = ["未配置栏目"]
        for tab_name in dict.fromkeys(str(name).strip() for name in names if str(name).strip()):
            counts[tab_name] = counts.get(tab_name, 0) + 1
    return {
        "article_count": len(article_ids),
        "tab_counts": [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: item[0])
        ],
    }


def list_title_dedup_candidates(since: str, exclude_id: int, connection=None) -> list[dict]:
    """Recent published plus in-flight articles usable as title dedup targets.

    Published articles are bounded by ``published_at`` inside the window, while
    in-flight queue states (including QUALITY_SAVED for articles that just
    finished quality but haven't entered the publish queue yet) are bounded by
    ``created_at`` so two articles of one ingestion batch can still see each
    other before either is published.
    """

    rows = _conn(connection).execute(
        """
        SELECT id, title_final, channels_json, status, published_at, dqd_archive_id
        FROM articles
        WHERE id <> ?
          AND (
            (status = 'PUBLISHED' AND published_at >= ?)
            OR (
              status IN ('READY_TO_PUBLISH', 'PUBLISHING', 'DRAFT_CONFIRMING', 'QUALITY_SAVED')
              AND created_at >= ?
            )
          )
        ORDER BY id
        """,
        (int(exclude_id), str(since), str(since)),
    ).fetchall()
    return [
        {**_row(row), "channels": _loads(row["channels_json"], [])}
        for row in rows
    ]


def article_counts(connection=None) -> dict[str, int]:
    rows = _conn(connection).execute(
        "SELECT status, COUNT(*) AS count FROM articles GROUP BY status"
    ).fetchall()
    counts = {status: 0 for status in VALID_STATUSES}
    counts.update({row["status"]: int(row["count"]) for row in rows})
    counts["TOTAL"] = sum(int(row["count"]) for row in rows)
    return counts


def transition_status(article_id: int, to_status: str, connection=None, *, message: str = "",
                      event_type: str = "STATUS_CHANGED", payload: Any = None,
                      from_status: Optional[str] = None,
                      quality_claim_token: Optional[str] = None) -> dict | None:
    to_status = str(to_status or "").upper().strip()
    if to_status not in VALID_STATUSES:
        raise ValueError("invalid article status")
    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    old_status = from_status or current["status"]
    now = _now()
    preserve_error = to_status in {"ERROR", "PUBLISH_FAILED"}
    published_tab_names = [
        str(tab.get("name") or "").strip()
        for tab in (current.get("tabs") or [])
        if str(tab.get("name") or "").strip()
    ]
    if not published_tab_names and current.get("tab_name"):
        published_tab_names = [str(current["tab_name"]).strip()]
    clauses = ["id=?"]
    params: list[Any] = [article_id]
    if quality_claim_token is not None:
        clauses.append("quality_claim_token=?")
        params.append(str(quality_claim_token))
    with conn:
        cursor = conn.execute(
            "UPDATE articles SET status=?, error=?, published_at=CASE WHEN ?='PUBLISHED' THEN COALESCE(published_at, ?) ELSE published_at END, published_tab_names_json=CASE WHEN ?='PUBLISHED' AND (published_tab_names_json IS NULL OR published_tab_names_json='[]') THEN ? ELSE published_tab_names_json END, updated_at=? WHERE " + " AND ".join(clauses),
            (
                to_status,
                (message or current.get("error")) if preserve_error else None,
                to_status,
                now,
                to_status,
                _json(published_tab_names, []),
                now,
                *params,
            ),
        )
        if quality_claim_token is not None and cursor.rowcount != 1:
            return None
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, old_status, to_status, event_type, message or None, _json(payload, {}), now),
        )
    return get_article(article_id, conn)


def transition_status_if_current(
    article_id: int,
    to_status: str,
    connection=None,
    *,
    allowed_from: Iterable[str] | None = None,
    message: str = "",
    event_type: str = "STATUS_CHANGED",
    payload: Any = None,
    current_updated_at: Optional[str] = None,
) -> dict | None:
    """Transition only if the row is still in one of the expected states.

    This is used for draft creation and timeout recovery so concurrent retries
    cannot both claim the same article.
    """

    to_status = str(to_status or "").upper().strip()
    if to_status not in VALID_STATUSES:
        raise ValueError("invalid article status")
    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")

    allowed = {str(status).upper().strip() for status in allowed_from} if allowed_from is not None else None
    if allowed is not None and current["status"] not in allowed:
        return None
    if current_updated_at is not None and str(current.get("updated_at") or "") != str(current_updated_at):
        return None

    now = _now()
    preserve_error = to_status in {"ERROR", "PUBLISH_FAILED"}
    published_tab_names = [
        str(tab.get("name") or "").strip()
        for tab in (current.get("tabs") or [])
        if str(tab.get("name") or "").strip()
    ]
    if not published_tab_names and current.get("tab_name"):
        published_tab_names = [str(current["tab_name"]).strip()]
    clauses = ["id=?"]
    params: list[Any] = [article_id]
    if allowed is not None:
        placeholders = ",".join("?" for _ in allowed)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(allowed))
    if current_updated_at is not None:
        clauses.append("updated_at=?")
        params.append(str(current_updated_at))

    with conn:
        cursor = conn.execute(
            "UPDATE articles SET status=?, error=?, published_at=CASE WHEN ?='PUBLISHED' THEN COALESCE(published_at, ?) ELSE published_at END, published_tab_names_json=CASE WHEN ?='PUBLISHED' AND (published_tab_names_json IS NULL OR published_tab_names_json='[]') THEN ? ELSE published_tab_names_json END, updated_at=? WHERE " + " AND ".join(clauses),
            (
                to_status,
                (message or current.get("error")) if preserve_error else None,
                to_status,
                now,
                to_status,
                _json(published_tab_names, []),
                now,
                *params,
            ),
        )
        if cursor.rowcount == 0:
            return None
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, current["status"], to_status, event_type, message or None, _json(payload, {}), now),
        )
    return get_article(article_id, conn)


def ensure_client_request_id(article_id: int, connection=None) -> str:
    """Return the article's stable idempotency key, creating it exactly once."""

    numeric_article_id = _positive_int(article_id, "article id")
    conn = _conn(connection)
    with conn:
        row = conn.execute(
            "SELECT client_request_id FROM articles WHERE id=?",
            (numeric_article_id,),
        ).fetchone()
        if row is None:
            raise ValueError("article not found")
        current = str(row["client_request_id"] or "").strip()
        if current:
            return current
        candidate = uuid.uuid4().hex
        conn.execute(
            """
            UPDATE articles SET client_request_id=?
            WHERE id=? AND (client_request_id IS NULL OR trim(client_request_id)='')
            """,
            (candidate, numeric_article_id),
        )
        row = conn.execute(
            "SELECT client_request_id FROM articles WHERE id=?",
            (numeric_article_id,),
        ).fetchone()
    return str(row["client_request_id"] or candidate).strip()


def _confirmation_next_at(value: Optional[str], *, now: Optional[str] = None) -> str:
    if value not in (None, ""):
        return str(value)
    base = now or _now()
    parsed = base[:-1] + "+00:00" if str(base).endswith("Z") else str(base)
    try:
        current = datetime.fromisoformat(parsed)
    except ValueError:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current + timedelta(seconds=15)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def mark_draft_result_unknown(
    article_id: int,
    connection=None,
    *,
    request_id: Optional[str] = None,
    next_confirm_at: Optional[str] = None,
    message: str = "创建草稿结果暂不可确认",
    payload: Any = None,
    allowed_from: Iterable[str] = ("PUBLISHING",),
    schedule_confirmation: bool = True,
) -> dict:
    """Move an in-flight draft into automatic result confirmation.

    This transition is deliberately separate from ``PUBLISH_FAILED``. A 5xx,
    timeout, or response without an archive ID may have succeeded upstream, so
    callers must confirm the result before sending another create request.
    """

    numeric_article_id = _positive_int(article_id, "article id")
    conn = _conn(connection)
    current = get_article(numeric_article_id, conn)
    if current is None:
        raise ValueError("article not found")
    allowed = {str(value).upper().strip() for value in allowed_from}
    if current["status"] not in allowed:
        return current
    now = _now()
    confirm_at = (
        _confirmation_next_at(next_confirm_at, now=now)
        if schedule_confirmation
        else None
    )
    client_request_id = ensure_client_request_id(numeric_article_id, conn)
    upstream_value = (
        current.get("upstream_request_id")
        if request_id in (None, "")
        else str(request_id).strip()[:200]
    )
    clauses = ["id=?", "status IN (" + ",".join("?" for _ in allowed) + ")"]
    params: list[Any] = [numeric_article_id, *sorted(allowed)]
    with conn:
        cursor = conn.execute(
            """
            UPDATE articles
            SET status='DRAFT_CONFIRMING', upstream_request_id=?,
                draft_uncertain_since=COALESCE(draft_uncertain_since, ?),
                draft_last_attempt_at=?, draft_next_confirm_at=?,
                draft_confirm_claimed_at=NULL, draft_confirm_claim_token=NULL,
                error=?, updated_at=?
            WHERE """ + " AND ".join(clauses),
            (upstream_value, now, now, confirm_at, message or None, now, *params),
        )
        if cursor.rowcount:
            conn.execute(
                """
                INSERT INTO article_events
                (article_id, from_status, to_status, event_type, message, payload_json, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    numeric_article_id,
                    current["status"],
                    "DRAFT_CONFIRMING",
                    "DRAFT_RESULT_UNKNOWN",
                    message or None,
                    _json(
                        {
                            **(payload if isinstance(payload, Mapping) else {}),
                            "client_request_id": client_request_id,
                            "upstream_request_id": upstream_value,
                            "next_confirm_at": confirm_at,
                        },
                        {},
                    ),
                    now,
                ),
            )
    return get_article(numeric_article_id, conn)


def list_due_draft_confirmations(
    connection=None,
    *,
    limit: int = 100,
    now: Optional[str] = None,
    claim_stale_after_seconds: int = 300,
) -> list[dict]:
    """List confirmation jobs whose scheduled check is due.

    Claims older than the lease are considered abandoned, allowing recovery
    after a worker process exits between claiming and recording its result.
    """

    conn = _conn(connection)
    now_value = str(now or _now())
    parsed = now_value[:-1] + "+00:00" if now_value.endswith("Z") else now_value
    try:
        current = datetime.fromisoformat(parsed)
    except ValueError:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    stale_before = (
        current - timedelta(seconds=max(1, int(claim_stale_after_seconds)))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    safe_limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        WHERE a.status='DRAFT_CONFIRMING'
          AND (a.dqd_archive_id IS NULL OR a.dqd_archive_id=0)
          AND a.draft_next_confirm_at IS NOT NULL
          AND a.draft_next_confirm_at<=?
          AND (a.draft_confirm_claimed_at IS NULL OR a.draft_confirm_claimed_at<=?)
        ORDER BY a.draft_next_confirm_at ASC, a.draft_uncertain_since ASC,
                 a.updated_at ASC, a.id ASC
        LIMIT ?
        """,
        (now_value, stale_before, safe_limit),
    ).fetchall()
    return [_decorate_article(row, conn) for row in rows]


def claim_due_draft_confirmation(
    article_id: int,
    expected_updated_at: str,
    connection=None,
    *,
    now: Optional[str] = None,
    claim_stale_after_seconds: int = 300,
) -> dict | None:
    """Atomically lease one due confirmation job for a worker."""

    numeric_article_id = _positive_int(article_id, "article id")
    if expected_updated_at in (None, ""):
        raise ValueError("expected_updated_at is required")
    conn = _conn(connection)
    now_value = str(now or _now())
    parsed = now_value[:-1] + "+00:00" if now_value.endswith("Z") else now_value
    try:
        current = datetime.fromisoformat(parsed)
    except ValueError:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    stale_before = (
        current - timedelta(seconds=max(1, int(claim_stale_after_seconds)))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    claim_token = uuid.uuid4().hex
    with conn:
        cursor = conn.execute(
            """
            UPDATE articles
            SET draft_confirm_claimed_at=?, draft_confirm_claim_token=?,
                draft_confirm_attempts=draft_confirm_attempts+1,
                draft_last_attempt_at=?, updated_at=?
            WHERE id=? AND status='DRAFT_CONFIRMING'
              AND (dqd_archive_id IS NULL OR dqd_archive_id=0)
              AND updated_at=?
              AND draft_next_confirm_at IS NOT NULL
              AND draft_next_confirm_at<=?
              AND (draft_confirm_claimed_at IS NULL OR draft_confirm_claimed_at<=?)
            """,
            (
                now_value,
                claim_token,
                now_value,
                now_value,
                numeric_article_id,
                str(expected_updated_at),
                now_value,
                stale_before,
            ),
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                numeric_article_id,
                "DRAFT_CONFIRMING",
                "DRAFT_CONFIRMING",
                "DRAFT_CONFIRMATION_CLAIMED",
                "领取草稿结果确认任务",
                _json({"attempt": "incremented"}, {}),
                now_value,
            ),
        )
    article = get_article(numeric_article_id, conn)
    if article is not None:
        article["draft_confirm_claim_token"] = claim_token
    return article


def record_draft_confirmation_result(
    article_id: int,
    connection=None,
    *,
    outcome: str,
    dqd_archive_id: Optional[int] = None,
    request_id: Optional[str] = None,
    next_confirm_at: Optional[str] = None,
    message: str = "",
    payload: Any = None,
    expected_updated_at: Optional[str] = None,
    claim_token: Optional[str] = None,
    schedule_confirmation: bool = True,
    publish_mode: Optional[int] = None,
    target_status: Optional[str] = None,
) -> dict | None:
    """Atomically persist a confirmation outcome and release its lease."""

    numeric_article_id = _positive_int(article_id, "article id")
    outcome_key = str(outcome or "").upper().strip()
    if outcome_key not in {"CREATED", "PENDING", "FAILED", "NOT_FOUND"}:
        raise ValueError("invalid draft confirmation outcome")
    conn = _conn(connection)
    current = get_article(numeric_article_id, conn)
    if current is None:
        raise ValueError("article not found")
    if current["status"] != "DRAFT_CONFIRMING":
        return current
    if expected_updated_at is not None and str(current.get("updated_at") or "") != str(expected_updated_at):
        return None
    current_claim_token = str(current.get("draft_confirm_claim_token") or "")
    if current_claim_token and str(claim_token or "") != current_claim_token:
        return None
    now = _now()
    published_tab_names = [
        str(tab.get("name") or "").strip()
        for tab in (current.get("tabs") or [])
        if str(tab.get("name") or "").strip()
    ]
    if not published_tab_names and current.get("tab_name"):
        published_tab_names = [str(current["tab_name"]).strip()]
    archive_id = 0 if dqd_archive_id in (None, "") else int(dqd_archive_id)
    if outcome_key == "CREATED" and archive_id <= 0:
        raise ValueError("dqd_archive_id is required when outcome is CREATED")
    if outcome_key == "CREATED":
        if publish_mode is not None:
            publish_mode = _publish_mode_int(publish_mode)
        requested_status = str(target_status or "").upper().strip()
        if requested_status not in {"", "DRAFT_CREATED", "PUBLISHED"}:
            raise ValueError("invalid confirmation target status")
        target_status = requested_status or ("PUBLISHED" if publish_mode == 1 else "DRAFT_CREATED")
        if target_status == "PUBLISHED" and publish_mode == 0:
            raise ValueError("publish_mode does not match confirmation target status")
        if target_status == "DRAFT_CREATED" and publish_mode == 1:
            raise ValueError("publish_mode does not match confirmation target status")
        confirm_at = None
        uncertain_since = current.get("draft_uncertain_since")
        error = None
        archive_value: Any = archive_id
    elif outcome_key == "FAILED":
        target_status = "PUBLISH_FAILED"
        confirm_at = None
        uncertain_since = current.get("draft_uncertain_since")
        error = message or current.get("error")
        archive_value = current.get("dqd_archive_id")
    else:
        target_status = "DRAFT_CONFIRMING"
        confirm_at = (
            _confirmation_next_at(next_confirm_at, now=now)
            if schedule_confirmation
            else None
        )
        uncertain_since = current.get("draft_uncertain_since") or now
        error = message or current.get("error")
        archive_value = current.get("dqd_archive_id")
    request_value = (
        current.get("upstream_request_id")
        if request_id in (None, "")
        else str(request_id).strip()[:200]
    )
    clauses = ["id=?", "status='DRAFT_CONFIRMING'"]
    params: list[Any] = [numeric_article_id]
    if expected_updated_at is not None:
        clauses.append("updated_at=?")
        params.append(str(expected_updated_at))
    if claim_token is not None:
        clauses.append("draft_confirm_claim_token=?")
        params.append(str(claim_token))
    with conn:
        cursor = conn.execute(
            """
            UPDATE articles
            SET status=?, dqd_archive_id=?, upstream_request_id=?,
                draft_next_confirm_at=?, draft_uncertain_since=?,
                draft_confirm_claimed_at=NULL, draft_confirm_claim_token=NULL,
                error=?,
                published_at=CASE WHEN ?='PUBLISHED' THEN COALESCE(published_at, ?) ELSE published_at END,
                published_tab_names_json=CASE WHEN ?='PUBLISHED' AND (published_tab_names_json IS NULL OR published_tab_names_json='[]') THEN ? ELSE published_tab_names_json END,
                updated_at=?
            WHERE """ + " AND ".join(clauses),
            (
                target_status,
                archive_value,
                request_value,
                confirm_at,
                uncertain_since,
                error,
                target_status,
                now,
                target_status,
                _json(published_tab_names, []),
                now,
                *params,
            ),
        )
        if cursor.rowcount != 1:
            return None
        event_payload = {
            "outcome": outcome_key,
            "dqd_archive_id": archive_id or None,
            "next_confirm_at": confirm_at,
            **(payload if isinstance(payload, Mapping) else {}),
        }
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                numeric_article_id,
                "DRAFT_CONFIRMING",
                target_status,
                "DRAFT_CONFIRMATION_RECORDED",
                message or None,
                _json(event_payload, {}),
                now,
            ),
        )
    return get_article(numeric_article_id, conn)


def save_quality(article_id: int, quality: Mapping[str, Any], connection=None, *,
                 status: Optional[str] = None, error: Optional[str] = None,
                 quality_claim_token: Optional[str] = None,
                 body_html: Optional[str] = None) -> dict:
    conn = _conn(connection)
    if status is not None and str(status).upper() not in VALID_STATUSES:
        raise ValueError("invalid article status")
    now = _now()
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    target = str(status).upper() if status else current["status"]
    clauses = ["id=?"]
    params: list[Any] = [article_id]
    if quality_claim_token is not None:
        clauses.append("quality_claim_token=?")
        params.append(str(quality_claim_token))
    assignments = ["quality_json=?", "status=?", "error=?", "updated_at=?"]
    values: list[Any] = [_json(dict(quality), {}), target, error, now]
    if body_html is not None:
        assignments.insert(0, "body_html=?")
        values.insert(0, str(body_html))
    with conn:
        cursor = conn.execute(
            "UPDATE articles SET " + ", ".join(assignments) + " WHERE " + " AND ".join(clauses),
            (*values, *params),
        )
        if quality_claim_token is not None and cursor.rowcount != 1:
            raise RuntimeError("文章质检租约已失效，放弃写入旧结果")
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, current["status"], target, "QUALITY_SAVED", error, _json(dict(quality), {}), now),
        )
    return get_article(article_id, conn)


def claim_quality_article(
    article_id: int,
    expected_updated_at: Optional[str] = None,
    connection=None,
    *,
    now: Optional[str] = None,
    claim_stale_after_seconds: int = 900,
) -> dict | None:
    """Atomically lease an article for quality work across processes.

    A token-less ``QUALITY_CHECKING`` row is a legacy/incomplete run and may
    be recovered. Active leases are protected until they expire, so a second
    worker cannot run the same article concurrently.
    """

    numeric_article_id = _positive_int(article_id, "article id")
    conn = _conn(connection)
    existing = get_article(numeric_article_id, conn)
    old_status = existing.get("status") if existing else "QUALITY_CHECKING"
    now_value = str(now or _now())
    parsed = now_value[:-1] + "+00:00" if now_value.endswith("Z") else now_value
    try:
        current_time = datetime.fromisoformat(parsed)
    except ValueError:
        current_time = datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    stale_before = (
        current_time - timedelta(seconds=max(1, int(claim_stale_after_seconds)))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    claim_token = uuid.uuid4().hex
    clauses = [
        "id=?",
        "status IN ('RECEIVED','QUALITY_CHECKING','ERROR')",
        "(quality_claim_token IS NULL OR quality_claimed_at IS NULL OR quality_claimed_at<=?)",
    ]
    params: list[Any] = [numeric_article_id, stale_before]
    if expected_updated_at not in (None, ""):
        clauses.append("updated_at=?")
        params.append(str(expected_updated_at))
    with conn:
        cursor = conn.execute(
            """
            UPDATE articles
            SET status='QUALITY_CHECKING', quality_claimed_at=?,
                quality_claim_token=?, updated_at=?
            WHERE """ + " AND ".join(clauses),
            (now_value, claim_token, now_value, *params),
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                numeric_article_id,
                old_status,
                "QUALITY_CHECKING",
                "QUALITY_CLAIMED",
                "领取文章质检任务",
                _json({"lease_seconds": max(1, int(claim_stale_after_seconds))}, {}),
                now_value,
            ),
        )
    article = get_article(numeric_article_id, conn)
    if article is not None:
        article["quality_claim_token"] = claim_token
    return article


def release_quality_claim(
    article_id: int,
    claim_token: Optional[str],
    connection=None,
) -> bool:
    """Release a quality lease without disturbing the article result."""

    if not claim_token:
        return False
    conn = _conn(connection)
    with conn:
        cursor = conn.execute(
            """
            UPDATE articles
            SET quality_claimed_at=NULL, quality_claim_token=NULL
            WHERE id=? AND quality_claim_token=?
            """,
            (_positive_int(article_id, "article id"), str(claim_token)),
        )
    return cursor.rowcount == 1


def claim_quality_recheck(article_id: int, connection=None) -> dict | None:
    """Atomically return one reviewed article to the automatic quality stage."""

    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    if current["status"] != "NEEDS_REVIEW":
        return None
    stored_quality = current.get("quality")
    quality = dict(stored_quality) if isinstance(stored_quality, Mapping) else {}
    quality.pop("promotion_repair", None)
    now = _now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE articles
            SET status='RECEIVED', quality_json=?, error=NULL,
                quality_claimed_at=NULL, quality_claim_token=NULL, updated_at=?
            WHERE id=? AND status='NEEDS_REVIEW'
            """,
            (_json(quality, {}), now, article_id),
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                article_id,
                "NEEDS_REVIEW",
                "RECEIVED",
                "QUALITY_RECHECK_REQUESTED",
                "人工触发重新优化与完整质检",
                _json({"previous_quality_reason": current.get("quality_reason") or ""}, {}),
                now,
            ),
        )
    return get_article(article_id, conn)


def manual_review_update(article_id: int, action: str, connection=None, *,
                         title: Optional[str] = None, body_html: Optional[str] = None,
                         litpic: Optional[str] = None, channels: Optional[Iterable[int]] = None,
                         tab_id: Optional[int] = None, note: str = "") -> dict:
    """Apply a manual decision and optional edits in one transaction."""

    action_key = str(action or "").strip().lower().replace("-", "_")
    action_status = {
        "pass": "READY_TO_PUBLISH",
        "approve": "READY_TO_PUBLISH",
        "reject": "REJECTED",
        "manual_fix_then_pass": "READY_TO_PUBLISH",
        "manual_fix": "READY_TO_PUBLISH",
    }.get(action_key)
    if action_status is None:
        raise ValueError("action must be pass, reject, or manual_fix_then_pass")
    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    if current["status"] != "NEEDS_REVIEW":
        raise ValueError("article is not awaiting manual review")
    if tab_id is not None and get_tab(tab_id, conn) is None:
        raise ValueError("tab not found")
    final_title = current["title_final"] if title is None else str(title)
    final_body = (
        current["body_html"]
        if body_html is None
        else remove_clickable_links(str(body_html))
    )
    final_litpic = current["litpic"] if litpic is None else str(litpic)
    final_channels = _normalise_channels(
        current["channels"] if channels is None else channels
    )
    if action_status == "READY_TO_PUBLISH" and int(current.get("upstream_archive_id") or 0) > 0:
        action_status = "ALREADY_PUBLISHED"
    quality = dict(current.get("quality") or {})
    if action_status in {"READY_TO_PUBLISH", "ALREADY_PUBLISHED"}:
        quality["pass"] = True
        quality["needs_review"] = False
        quality["manual_review"] = True
        quality["reason"] = note or "人工审核通过"
        quality["issues"] = {
            "title_problems": [],
            "completeness_problems": [],
            "dirty_content": [],
            "channel_problems": [],
            "semantic_problems": [],
        }
    now = _now()
    payload = {"action": action_key, "title": title, "body_html": body_html,
               "litpic": litpic, "channels": final_channels, "tab_id": tab_id}
    with conn:
        conn.execute(
            """
            UPDATE articles SET title_final=?, body_html=?, litpic=?, channels_json=?,
                tab_id=COALESCE(?, tab_id), status=?, quality_json=?, review_note=?, reviewed_at=?, updated_at=?, error=NULL
            WHERE id=?
            """,
            (final_title, final_body, final_litpic, _json(final_channels, []), tab_id,
             action_status, _json(quality, {}), note or None, now, now, article_id),
        )
        if tab_id is not None:
            _replace_article_tabs(article_id, [tab_id], conn)
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, current["status"], action_status, "MANUAL_REVIEW", note or action_key,
             _json(payload, {}), now),
        )
    return get_article(article_id, conn)


def list_article_events(article_id: int, connection=None) -> list[dict]:
    result = _rows(_conn(connection).execute(
        "SELECT * FROM article_events WHERE article_id=? ORDER BY created_at ASC, id ASC",
        (article_id,),
    ).fetchall())
    for item in result:
        item["payload"] = _loads(item.get("payload_json"), {})
    return result


def add_article_event(article_id: int, event_type: str, connection=None, *,
                      message: str = "", payload: Any = None,
                      from_status: Optional[str] = None, to_status: Optional[str] = None,
                      quality_claim_token: Optional[str] = None) -> dict:
    conn = _conn(connection)
    if get_article(article_id, conn) is None:
        raise ValueError("article not found")
    with conn:
        now = _now()
        event_values = (
            article_id,
            from_status,
            to_status,
            str(event_type),
            message or None,
            _json(payload, {}),
            now,
        )
        if quality_claim_token is None:
            cursor = conn.execute(
                """
                INSERT INTO article_events
                (article_id, from_status, to_status, event_type, message, payload_json, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                event_values,
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO article_events
                (article_id, from_status, to_status, event_type, message, payload_json, created_at)
                SELECT ?,?,?,?,?,?,?
                WHERE EXISTS (
                    SELECT 1 FROM articles
                    WHERE id=? AND quality_claim_token=?
                )
                """,
                (*event_values, article_id, str(quality_claim_token)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("文章质检租约已失效，放弃写入旧事件")
    return _row(conn.execute("SELECT * FROM article_events WHERE id=?", (cursor.lastrowid,)).fetchone())


def start_run_log(run_type: str = "INGEST", connection=None, *, details: Any = None) -> int:
    conn = _conn(connection)
    with conn:
        cursor = conn.execute(
            "INSERT INTO run_logs (run_type, status, started_at, details_json) VALUES (?,?,?,?)",
            (str(run_type), "RUNNING", _now(), _json(details, {})),
        )
    return int(cursor.lastrowid)


def finish_run_log(run_id: int, connection=None, *, status: str = "SUCCESS",
                   fetched_count: int = 0, inserted_count: int = 0,
                   updated_count: int = 0, error_count: int = 0,
                   details: Any = None, error: Optional[str] = None) -> dict:
    conn = _conn(connection)
    with conn:
        conn.execute(
            """
            UPDATE run_logs SET status=?, finished_at=?, fetched_count=?, inserted_count=?,
                updated_count=?, error_count=?, details_json=?, error=? WHERE id=?
            """,
            (str(status).upper(), _now(), int(fetched_count), int(inserted_count), int(updated_count),
             int(error_count), _json(details, {}), error, run_id),
        )
    return _row(conn.execute("SELECT * FROM run_logs WHERE id=?", (run_id,)).fetchone())


def list_run_logs(connection=None, *, limit: int = 30) -> list[dict]:
    conn = _conn(connection)
    limit = max(1, min(int(limit), 200))
    result = _rows(conn.execute(
        "SELECT * FROM run_logs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall())
    for item in result:
        item["details"] = _loads(item.get("details_json"), {})
    return result


def get_setting(key: str, default: Any = None, connection=None):
    row = _conn(connection).execute("SELECT value_json FROM settings WHERE key=?", (str(key),)).fetchone()
    return default if row is None else _loads(row["value_json"], default)


def set_setting(key: str, value: Any, connection=None) -> Any:
    conn = _conn(connection)
    with conn:
        conn.execute(
            """
            INSERT INTO settings (key, value_json, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (str(key), _json(value, None), _now()),
        )
    return value


def claim_report_delivery(
    period_start: str,
    period_end: str,
    connection=None,
    *,
    retry_after_seconds: int = 300,
    stale_after_seconds: int = 900,
) -> dict[str, Any] | None:
    """Claim one report period so only one process sends it."""

    conn = _conn(connection)
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    token = uuid.uuid4().hex
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO report_deliveries
            (period_start, period_end, status, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, 'PENDING', ?, ?, ?)
            """,
            (str(period_start), str(period_end), now_text, now_text, now_text),
        )
        row = conn.execute(
            "SELECT * FROM report_deliveries WHERE period_start=? AND period_end=?",
            (str(period_start), str(period_end)),
        ).fetchone()
        if row is None:
            return None
        retry_at = row["next_attempt_at"]
        due = not retry_at or str(retry_at) <= now_text
        stale = bool(
            row["status"] == "SENDING"
            and row["claimed_at"]
            and row["next_attempt_at"]
            and str(row["claimed_at"]) <= (
                now - timedelta(seconds=max(1, int(stale_after_seconds)))
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        if row["status"] not in {"PENDING", "FAILED"} and not stale:
            return None
        if row["status"] in {"PENDING", "FAILED"} and not due:
            return None
        cursor = conn.execute(
            """
            UPDATE report_deliveries
            SET status='SENDING', attempts=attempts+1, claim_token=?, claimed_at=?,
                next_attempt_at=?, error=NULL, updated_at=?
            WHERE id=? AND (
                status IN ('PENDING','FAILED')
                OR (status='SENDING' AND claimed_at=?)
            )
            """,
            (
                token,
                now_text,
                (now + timedelta(seconds=max(1, int(retry_after_seconds))))
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
                now_text,
                int(row["id"]),
                row["claimed_at"],
            ),
        )
        if cursor.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM report_deliveries WHERE id=?", (int(row["id"]),)
        ).fetchone()
    return _row(claimed)


def next_report_period(
    latest_period_start: str,
    latest_period_end: str,
    connection=None,
) -> tuple[str, str]:
    """Choose a failed period first, otherwise the next unsent daily period."""

    conn = _conn(connection)
    pending = conn.execute(
        """
        SELECT period_start, period_end
        FROM report_deliveries
        WHERE status IN ('PENDING', 'FAILED')
           OR (status='SENDING' AND claimed_at IS NOT NULL AND next_attempt_at IS NOT NULL)
        ORDER BY period_end, id
        LIMIT 1
        """
    ).fetchone()
    if pending is not None:
        pending_end = datetime.fromisoformat(str(pending["period_end"]).replace("Z", "+00:00"))
        latest_end = datetime.fromisoformat(str(latest_period_end).replace("Z", "+00:00"))
        if (pending_end.hour, pending_end.minute) == (latest_end.hour, latest_end.minute):
            return str(pending["period_start"]), str(pending["period_end"])
    latest_record = conn.execute(
        "SELECT MAX(period_end) AS period_end FROM report_deliveries"
    ).fetchone()
    previous_end = str(latest_record["period_end"] or "") if latest_record else ""
    if previous_end and previous_end < str(latest_period_end):
        parsed = datetime.fromisoformat(previous_end.replace("Z", "+00:00"))
        latest_end = datetime.fromisoformat(str(latest_period_end).replace("Z", "+00:00"))
        if (parsed.hour, parsed.minute) != (latest_end.hour, latest_end.minute):
            return str(latest_period_start), str(latest_period_end)
        next_end = (parsed + timedelta(days=1)).astimezone(timezone.utc)
        if next_end <= latest_end:
            return (
                previous_end,
                next_end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
    return str(latest_period_start), str(latest_period_end)


def has_report_deliveries(connection=None) -> bool:
    row = _conn(connection).execute("SELECT 1 FROM report_deliveries LIMIT 1").fetchone()
    return row is not None


def finish_report_delivery(
    delivery_id: int,
    claim_token: str,
    connection=None,
    *,
    response: Any = None,
) -> bool:
    conn = _conn(connection)
    now = _now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE report_deliveries
            SET status='SENT', sent_at=?, updated_at=?, response_json=?, error=NULL
            WHERE id=? AND status='SENDING' AND claim_token=?
            """,
            (now, now, _json(response, {}), int(delivery_id), str(claim_token)),
        )
    return cursor.rowcount == 1


def fail_report_delivery(
    delivery_id: int,
    claim_token: str,
    error: str,
    connection=None,
    *,
    retry_after_seconds: int = 300,
    response: Any = None,
) -> bool:
    conn = _conn(connection)
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    next_attempt = (now + timedelta(seconds=max(1, int(retry_after_seconds)))) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    with conn:
        cursor = conn.execute(
            """
            UPDATE report_deliveries
            SET status='FAILED', next_attempt_at=?, updated_at=?, response_json=?, error=?
            WHERE id=? AND status='SENDING' AND claim_token=?
            """,
            (
                next_attempt,
                now_text,
                _json(response, {}),
                str(error or "发送失败")[:1000],
                int(delivery_id),
                str(claim_token),
            ),
        )
    return cursor.rowcount == 1


def mark_report_delivery_unknown(
    delivery_id: int,
    claim_token: str,
    error: str,
    connection=None,
) -> bool:
    """Keep an ambiguous request claimed so it is never sent automatically again."""

    conn = _conn(connection)
    now = _now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE report_deliveries
            SET updated_at=?, next_attempt_at=NULL, error=?
            WHERE id=? AND status='SENDING' AND claim_token=?
            """,
            (now, str(error or "发送结果未知")[:1000], int(delivery_id), str(claim_token)),
        )
    return cursor.rowcount == 1


# Friendly aliases used by route/worker code.
get_article_counts = article_counts
get_events = list_article_events
save_manual_review = manual_review_update
create_run_log = start_run_log
update_run_log = finish_run_log
