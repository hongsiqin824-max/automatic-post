"""SQLite connection and schema helpers for the automatic-post application.

The application deliberately keeps its own article primary key.  IDs returned
by the upstream material API or by the DQD backend are external references and
are stored in separate columns.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from flask import current_app, g


PathLike = Union[str, Path]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tabs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_tab_id  INTEGER NOT NULL UNIQUE,
    name            TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    tab_id          INTEGER REFERENCES tabs(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS articles (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source               TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    upstream_archive_id  INTEGER NOT NULL DEFAULT 0,
    dqd_source_id        INTEGER,
    dqd_archive_id       INTEGER,
    title_original       TEXT NOT NULL DEFAULT '',
    title_final          TEXT NOT NULL DEFAULT '',
    body_html            TEXT NOT NULL DEFAULT '',
    litpic               TEXT NOT NULL DEFAULT '',
    channels_json        TEXT NOT NULL DEFAULT '[]',
    tab_id               INTEGER REFERENCES tabs(id) ON DELETE SET NULL,
    level                TEXT NOT NULL DEFAULT 'B',
    status               TEXT NOT NULL DEFAULT 'RECEIVED',
    quality_json         TEXT NOT NULL DEFAULT '{}',
    raw_json             TEXT NOT NULL DEFAULT '{}',
    error                TEXT,
    review_note          TEXT,
    reviewed_at          TEXT,
    published_at         TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source, source_url)
);

CREATE INDEX IF NOT EXISTS idx_articles_status_updated
    ON articles(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source_last_seen
    ON articles(source, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_tab_status
    ON articles(tab_id, status);

CREATE TABLE IF NOT EXISTS article_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id   INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    from_status  TEXT,
    to_status    TEXT,
    event_type   TEXT NOT NULL,
    message      TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_article_events_article_time
    ON article_events(article_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS run_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type       TEXT NOT NULL DEFAULT 'INGEST',
    status         TEXT NOT NULL DEFAULT 'RUNNING',
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at    TEXT,
    fetched_count  INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count  INTEGER NOT NULL DEFAULT 0,
    error_count    INTEGER NOT NULL DEFAULT 0,
    details_json   TEXT NOT NULL DEFAULT '{}',
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_logs_started
    ON run_logs(started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT 'null',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS open_platform_auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    auth_status TEXT NOT NULL DEFAULT 'UNAUTHORIZED',
    pending_state TEXT NOT NULL DEFAULT '',
    pending_state_expires_at TEXT,
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    token_type TEXT NOT NULL DEFAULT 'Bearer',
    token_expires_at TEXT,
    refresh_token_expires_at TEXT,
    authorized_user_json TEXT NOT NULL DEFAULT '{}',
    last_authorize_url TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _database_path() -> str:
    """Resolve the configured SQLite path without exposing secrets in code."""

    try:
        configured = current_app.config.get("DATABASE") or current_app.config.get("DB_PATH")
    except RuntimeError:
        configured = None
    if configured:
        return str(configured)
    return str(Path(__file__).resolve().parents[1] / "instance" / "automatic_post.sqlite3")


def _connect(database: PathLike) -> sqlite3.Connection:
    database_text = str(database)
    if database_text != ":memory:":
        Path(database_text).expanduser().parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_text, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _prepare_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Make externally supplied connections behave like ``get_db`` ones."""

    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def get_db() -> sqlite3.Connection:
    """Return the request/application-context SQLite connection."""

    if "db" not in g:
        g.db = _connect(_database_path())
    return g.db


def close_db(error: Optional[BaseException] = None) -> None:
    """Close and remove the Flask-context connection."""

    del error  # Flask passes the teardown exception; it is not persisted here.
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db(database: Optional[PathLike] = None) -> None:
    """Create the schema.

    With a path, a short-lived connection is used (handy for scripts/tests).
    Without one, the current Flask-context connection is initialized.
    """

    owns_connection = database is not None
    connection = _connect(database) if owns_connection else get_db()
    try:
        connection.executescript(SCHEMA_SQL)
        current = now_iso()
        connection.execute(
            """INSERT OR IGNORE INTO open_platform_auth
               (id,auth_status,pending_state,pending_state_expires_at,access_token,refresh_token,token_type,token_expires_at,refresh_token_expires_at,authorized_user_json,last_authorize_url,last_error,created_at,updated_at)
               VALUES(1,'UNAUTHORIZED','','', '','','Bearer',NULL,NULL,'{}','','',?,?)""",
            (current, current),
        )
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


def _decode_open_platform_auth(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["authorized_user"] = json.loads(item.pop("authorized_user_json") or "{}")
    except json.JSONDecodeError:
        item["authorized_user"] = {}
    return item


def get_open_platform_auth(db_path: PathLike) -> dict | None:
    with _connect(db_path) as conn:
        return _decode_open_platform_auth(conn.execute("SELECT * FROM open_platform_auth WHERE id=1").fetchone())


def update_open_platform_auth(db_path: PathLike, **fields: Any) -> dict:
    allowed = {
        "auth_status",
        "pending_state",
        "pending_state_expires_at",
        "access_token",
        "refresh_token",
        "token_type",
        "token_expires_at",
        "refresh_token_expires_at",
        "authorized_user",
        "last_authorize_url",
        "last_error",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        current = get_open_platform_auth(db_path)
        if current is None:
            raise KeyError("open_platform_auth 不存在")
        return current
    encoded: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "authorized_user":
            encoded["authorized_user_json"] = json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))
        else:
            encoded[key] = value
    encoded["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in encoded)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE open_platform_auth SET {assignments} WHERE id=1", [*encoded.values()])
        row = conn.execute("SELECT * FROM open_platform_auth WHERE id=1").fetchone()
    result = _decode_open_platform_auth(row)
    if result is None:
        raise KeyError("open_platform_auth 不存在")
    return result


def reset_open_platform_auth(db_path: PathLike) -> dict:
    current = now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE open_platform_auth
               SET auth_status='UNAUTHORIZED', pending_state='', pending_state_expires_at=NULL,
                   access_token='', refresh_token='', token_type='Bearer', token_expires_at=NULL,
                   refresh_token_expires_at=NULL, authorized_user_json='{}',
                   last_authorize_url='', last_error='', updated_at=?
               WHERE id=1""",
            (current,),
        )
    current_row = get_open_platform_auth(db_path)
    if current_row is None:
        raise KeyError("open_platform_auth 不存在")
    return current_row


def init_app(app) -> None:
    """Register database teardown and an optional ``flask init-db`` command."""

    app.teardown_appcontext(close_db)

    try:
        import click

        @app.cli.command("init-db")
        def _init_db_command():
            init_db()
            click.echo("Initialized automatic-post database.")
    except ImportError:  # pragma: no cover - Flask installations include click
        pass
