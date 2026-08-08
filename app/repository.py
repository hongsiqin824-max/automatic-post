"""Persistence operations for articles, configuration, and workflow history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .db import get_db, _prepare_connection


VALID_STATUSES = {
    "RECEIVED",
    "QUALITY_CHECKING",
    "NEEDS_REVIEW",
    "READY_TO_PUBLISH",
    "PUBLISHING",
    "DRAFT_CREATED",
    "PUBLISH_FAILED",
    "REJECTED",
    "ALREADY_PUBLISHED",
    "MAPPING_BLOCKED",
    "ERROR",
}
VALID_LEVELS = {"S", "A", "B", "C"}


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


def _decorate_article(item):
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
    item["tab_name"] = item.get("tab_name") or ""
    return item


def _status_label(value: str) -> str:
    labels = {
        "RECEIVED": "已获取",
        "QUALITY_CHECKING": "质检中",
        "NEEDS_REVIEW": "待人工审核",
        "READY_TO_PUBLISH": "待发队列",
        "PUBLISHING": "创建草稿中",
        "DRAFT_CREATED": "草稿已创建",
        "PUBLISH_FAILED": "创建草稿失败",
        "REJECTED": "已驳回",
        "ALREADY_PUBLISHED": "已存在后台文章",
        "MAPPING_BLOCKED": "待匹配后台素材",
        "ERROR": "处理失败",
    }
    return labels.get(value, value or "未知")


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
            else:
                code, display_name = source[0], source[1]
                enabled, tab_id = 0, None
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


def create_tab(name: str, backend_tab_id: int, enabled: bool = True, connection=None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("tab name is required")
    conn = _conn(connection)
    now = _now()
    with conn:
        cursor = conn.execute(
            "INSERT INTO tabs (backend_tab_id, name, enabled, created_at, updated_at) VALUES (?,?,?,?,?)",
            (int(backend_tab_id), name, int(bool(enabled)), now, now),
        )
    return get_tab(cursor.lastrowid, conn)


def update_tab(tab_id: int, connection=None, *, name: Optional[str] = None,
               backend_tab_id: Optional[int] = None, enabled: Optional[bool] = None) -> dict:
    conn = _conn(connection)
    current = get_tab(tab_id, conn)
    if current is None:
        raise ValueError("tab not found")
    values = {
        "name": str(name).strip() if name is not None else current["name"],
        "backend_tab_id": int(backend_tab_id) if backend_tab_id is not None else current["backend_tab_id"],
        "enabled": int(bool(enabled)) if enabled is not None else current["enabled"],
        "updated_at": _now(),
    }
    if not values["name"]:
        raise ValueError("tab name is required")
    if current["enabled"] and not values["enabled"]:
        _assert_tab_can_disable(tab_id, conn)
    with conn:
        conn.execute(
            "UPDATE tabs SET name=?, backend_tab_id=?, enabled=?, updated_at=? WHERE id=?",
            (values["name"], values["backend_tab_id"], values["enabled"], values["updated_at"], tab_id),
        )
    return get_tab(tab_id, conn)


def _assert_tab_can_disable(tab_id: int, conn) -> None:
    row = conn.execute(
        "SELECT 1 FROM sources WHERE tab_id=? AND enabled=1 LIMIT 1", (tab_id,)
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
        clauses.append("s.tab_id=?")
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
    return result


def get_source(code: str, connection=None) -> Optional[dict]:
    item = _row(_conn(connection).execute(
        """
        SELECT s.*, t.name AS tab_name, t.backend_tab_id
        FROM sources s LEFT JOIN tabs t ON t.id=s.tab_id WHERE s.code=?
        """, (str(code).strip(),)
    ).fetchone())
    if item is not None:
        item["name"] = item["display_name"]
    return item


def update_source(code: str, connection=None, *, display_name: Optional[str] = None,
                  enabled: Optional[bool] = None, tab_id: Optional[int] = None,
                  clear_tab: bool = False) -> dict:
    conn = _conn(connection)
    current = get_source(code, conn)
    if current is None:
        raise ValueError("source not found")
    new_tab_id = None if clear_tab else (tab_id if tab_id is not None else current["tab_id"])
    new_enabled = int(bool(enabled)) if enabled is not None else current["enabled"]
    if new_tab_id is not None:
        tab = get_tab(new_tab_id, conn)
        if tab is None:
            raise ValueError("tab not found")
        if new_enabled and not tab["enabled"]:
            raise ValueError("enabled source must use an enabled tab")
    if new_enabled and new_tab_id is None:
        raise ValueError("an enabled source must be assigned to a tab")
    name = str(display_name).strip() if display_name is not None else current["display_name"]
    if not name:
        raise ValueError("source display_name is required")
    with conn:
        conn.execute(
            "UPDATE sources SET display_name=?, enabled=?, tab_id=?, updated_at=? WHERE code=?",
            (name, new_enabled, new_tab_id, _now(), str(code).strip()),
        )
    return get_source(code, conn)


def set_source_enabled(code: str, enabled: bool, connection=None) -> dict:
    return update_source(code, connection, enabled=enabled)


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
    return {
        "source": source,
        "source_url": source_url,
        "upstream_archive_id": int(material.get("archive_id") or material.get("upstream_archive_id") or 0),
        "title_original": str(material.get("title_original", title) or ""),
        "title_final": str(material.get("title_final", title) or ""),
        "body_html": str(body),
        "litpic": str(litpic),
        "channels": channels,
        "raw": raw if isinstance(raw, Mapping) else {},
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
    return result


def upsert_material(material: Mapping[str, Any], connection=None) -> dict:
    """Insert/update one upstream material and return ``{article, created}``.

    Re-fetching an existing key refreshes content and ``last_seen_at`` but does
    not reset workflow status or overwrite manually edited final content.
    """

    conn = _conn(connection)
    item = _normalise_material(material)
    now = _now()
    existing = conn.execute(
        "SELECT * FROM articles WHERE source=? AND source_url=?",
        (item["source"], item["source_url"]),
    ).fetchone()
    source_config = conn.execute(
        "SELECT tab_id FROM sources WHERE code=?", (item["source"],)
    ).fetchone()
    source_tab_id = source_config["tab_id"] if source_config else None
    if existing is None:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO articles
                (source, source_url, upstream_archive_id, title_original, title_final,
                 body_html, litpic, channels_json, tab_id, level, status, quality_json,
                 raw_json, created_at, updated_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (item["source"], item["source_url"], item["upstream_archive_id"],
                 item["title_original"], item["title_final"], item["body_html"],
                 item["litpic"], _json(item["channels"], []), source_tab_id, "B", "RECEIVED",
                 "{}", _json(item["raw"], {}), now, now, now),
            )
            article_id = cursor.lastrowid
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
                    _json({"source": item["source"], "source_url": item["source_url"]}, {}),
                    now,
                ),
            )
        return {"article": get_article(article_id, conn), "created": True}

    with conn:
        conn.execute(
            """
            UPDATE articles SET upstream_archive_id=?, title_original=?,
                title_final=CASE WHEN status IN ('RECEIVED','QUALITY_CHECKING','ERROR')
                    THEN ? ELSE title_final END,
                body_html=CASE WHEN status IN ('RECEIVED','QUALITY_CHECKING','ERROR')
                    THEN ? ELSE body_html END,
                litpic=CASE WHEN status IN ('RECEIVED','QUALITY_CHECKING','ERROR')
                    THEN ? ELSE litpic END,
                channels_json=CASE WHEN status IN ('RECEIVED','QUALITY_CHECKING','ERROR')
                    THEN ? ELSE channels_json END,
                tab_id=COALESCE(tab_id, ?), raw_json=?, updated_at=?, last_seen_at=?
            WHERE id=?
            """,
            (item["upstream_archive_id"], item["title_original"], item["title_final"],
             item["body_html"], item["litpic"], _json(item["channels"], []), source_tab_id,
             _json(item["raw"], {}), now, now, existing["id"]),
        )
    return {"article": get_article(existing["id"], conn), "created": False}


def assign_article_tab(article_id: int, tab_id: Optional[int], connection=None) -> dict:
    conn = _conn(connection)
    if tab_id is not None and get_tab(tab_id, conn) is None:
        raise ValueError("tab not found")
    with conn:
        conn.execute("UPDATE articles SET tab_id=?, updated_at=? WHERE id=?", (tab_id, _now(), article_id))
    return get_article(article_id, conn)


def update_article_backend_refs(article_id: int, connection=None, *,
                                dqd_source_id: Optional[int] = None,
                                dqd_archive_id: Optional[int] = None,
                                upstream_archive_id: Optional[int] = None) -> dict:
    """Persist IDs learned while bridging to the legacy DQD backend."""

    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    values = (
        current["dqd_source_id"] if dqd_source_id is None else int(dqd_source_id),
        current["dqd_archive_id"] if dqd_archive_id is None else int(dqd_archive_id),
        current["upstream_archive_id"] if upstream_archive_id is None else int(upstream_archive_id),
        _now(), article_id,
    )
    with conn:
        conn.execute(
            """
            UPDATE articles SET dqd_source_id=?, dqd_archive_id=?, upstream_archive_id=?, updated_at=?
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
    return _decorate_article(conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        WHERE a.id=?
        """,
        (article_id,),
    ).fetchone())


def get_article_by_key(source: str, source_url: str, connection=None) -> Optional[dict]:
    conn = _conn(connection)
    return _decorate_article(conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        WHERE a.source=? AND a.source_url=?
        """,
        (source, source_url),
    ).fetchone())


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
        clauses.append("a.tab_id=?")
        params.append(tab_id)
    if query:
        clauses.append("(a.title_final LIKE ? OR a.title_original LIKE ? OR a.source_url LIKE ?)")
        pattern = f"%{str(query).strip()}%"
        params.extend([pattern, pattern, pattern])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows = conn.execute(
        """
        SELECT a.*, t.name AS tab_name, t.backend_tab_id
        FROM articles a LEFT JOIN tabs t ON t.id=a.tab_id
        """ + where + " ORDER BY a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [_decorate_article(row) for row in rows]


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
        clauses.append("tab_id=?")
        params.append(tab_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return int(conn.execute("SELECT COUNT(*) FROM articles" + where, params).fetchone()[0])


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
                      from_status: Optional[str] = None) -> dict:
    to_status = str(to_status or "").upper().strip()
    if to_status not in VALID_STATUSES:
        raise ValueError("invalid article status")
    conn = _conn(connection)
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    old_status = from_status or current["status"]
    now = _now()
    with conn:
        conn.execute(
            "UPDATE articles SET status=?, error=?, updated_at=? WHERE id=?",
            (to_status, None if to_status != "ERROR" else (message or current.get("error")), now, article_id),
        )
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, old_status, to_status, event_type, message or None, _json(payload, {}), now),
        )
    return get_article(article_id, conn)


def save_quality(article_id: int, quality: Mapping[str, Any], connection=None, *,
                 status: Optional[str] = None, error: Optional[str] = None) -> dict:
    conn = _conn(connection)
    if status is not None and str(status).upper() not in VALID_STATUSES:
        raise ValueError("invalid article status")
    now = _now()
    current = get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    target = str(status).upper() if status else current["status"]
    with conn:
        conn.execute(
            "UPDATE articles SET quality_json=?, status=?, error=?, updated_at=? WHERE id=?",
            (_json(dict(quality), {}), target, error, now, article_id),
        )
        conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, current["status"], target, "QUALITY_SAVED", error, _json(dict(quality), {}), now),
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
    final_body = current["body_html"] if body_html is None else str(body_html)
    final_litpic = current["litpic"] if litpic is None else str(litpic)
    final_channels = current["channels"] if channels is None else _normalise_channels(channels)
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
                      from_status: Optional[str] = None, to_status: Optional[str] = None) -> dict:
    conn = _conn(connection)
    if get_article(article_id, conn) is None:
        raise ValueError("article not found")
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO article_events
            (article_id, from_status, to_status, event_type, message, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (article_id, from_status, to_status, str(event_type), message or None, _json(payload, {}), _now()),
        )
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


# Friendly aliases used by route/worker code.
get_article_counts = article_counts
get_events = list_article_events
save_manual_review = manual_review_update
create_run_log = start_run_log
update_run_log = finish_run_log
