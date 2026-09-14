"""SQLite connection and schema helpers for the automatic-post application.

The application deliberately keeps its own article primary key.  IDs returned
by the upstream material API or by the DQD backend are external references and
are stored in separate columns.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from flask import current_app, g

from .origin_identity import source_origin_key


PathLike = Union[str, Path]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tabs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_tab_id  INTEGER NOT NULL UNIQUE,
    name            TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    publish_mode    INTEGER NOT NULL DEFAULT 0 CHECK (publish_mode IN (0, 1)),
    fallback_litpic TEXT NOT NULL DEFAULT '',
    ai_league_guard_enabled INTEGER NOT NULL DEFAULT 0 CHECK (ai_league_guard_enabled IN (0, 1)),
    ai_league_guard_definition TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS event_tab_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code     TEXT REFERENCES sources(code) ON UPDATE CASCADE ON DELETE CASCADE,
    marker_type     TEXT NOT NULL CHECK (marker_type IN ('league', 'team')),
    marker_code     TEXT NOT NULL CHECK (length(trim(marker_code)) > 0),
    tab_id          INTEGER REFERENCES tabs(id) ON DELETE SET NULL,
    publish_mode_override INTEGER CHECK (publish_mode_override IN (0, 1, 2)),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    first_seen_at   TEXT,
    last_seen_at    TEXT,
    sample_source   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    tab_id          INTEGER REFERENCES tabs(id) ON DELETE SET NULL,
    publish_mode    INTEGER NOT NULL DEFAULT 0 CHECK (publish_mode IN (0, 1)),
    publish_mode_override INTEGER CHECK (publish_mode_override IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS source_tabs (
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    tab_id          INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
    sort_order      INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    PRIMARY KEY (source_id, tab_id)
);

CREATE INDEX IF NOT EXISTS idx_source_tabs_tab_source
    ON source_tabs(tab_id, source_id);

CREATE TABLE IF NOT EXISTS publish_accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dqd_user_id      INTEGER NOT NULL UNIQUE CHECK (dqd_user_id > 0),
    user_name        TEXT NOT NULL CHECK (length(trim(user_name)) > 0),
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    assignment_count INTEGER NOT NULL DEFAULT 0 CHECK (assignment_count >= 0),
    last_assigned_at TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_publish_accounts_assignment
    ON publish_accounts(enabled, assignment_count, id);

CREATE TABLE IF NOT EXISTS articles (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source               TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    origin_key           TEXT,
    duplicate_of_article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    upstream_archive_id  INTEGER NOT NULL DEFAULT 0,
    dqd_source_id        INTEGER,
    dqd_archive_id       INTEGER,
    client_request_id    TEXT,
    upstream_request_id  TEXT,
    draft_confirm_attempts INTEGER NOT NULL DEFAULT 0 CHECK (draft_confirm_attempts >= 0),
    draft_next_confirm_at TEXT,
    draft_uncertain_since TEXT,
    draft_last_attempt_at TEXT,
    draft_confirm_claimed_at TEXT,
    draft_confirm_claim_token TEXT,
    quality_claimed_at    TEXT,
    quality_claim_token   TEXT,
    publish_account_id   INTEGER REFERENCES publish_accounts(id) ON DELETE SET NULL,
    publish_user_id      INTEGER,
    publish_user_name    TEXT,
    publish_account_assigned_at TEXT,
    title_original       TEXT NOT NULL DEFAULT '',
    title_final          TEXT NOT NULL DEFAULT '',
    body_html            TEXT NOT NULL DEFAULT '',
    litpic               TEXT NOT NULL DEFAULT '',
    channels_json        TEXT NOT NULL DEFAULT '[]',
    tab_id               INTEGER REFERENCES tabs(id) ON DELETE SET NULL,
    publish_mode         INTEGER CHECK (publish_mode IN (0, 1, 2)),
    publish_mode_decided_at TEXT,
    material_user_name   TEXT NOT NULL DEFAULT '',
    route_league         TEXT NOT NULL DEFAULT '',
    route_team           TEXT NOT NULL DEFAULT '',
    route_match_type     TEXT NOT NULL DEFAULT 'source'
        CHECK (route_match_type IN ('source', 'league', 'team')),
    route_rule_id        INTEGER REFERENCES event_tab_rules(id) ON DELETE SET NULL,
    level                TEXT NOT NULL DEFAULT 'B',
    status               TEXT NOT NULL DEFAULT 'RECEIVED',
    quality_json         TEXT NOT NULL DEFAULT '{}',
    raw_json             TEXT NOT NULL DEFAULT '{}',
    error                TEXT,
    review_note          TEXT,
    reviewed_at          TEXT,
    published_at         TEXT,
    published_tab_names_json TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS article_tabs (
    article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tab_id          INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
    sort_order      INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    PRIMARY KEY (article_id, tab_id)
);

CREATE INDEX IF NOT EXISTS idx_article_tabs_tab_article
    ON article_tabs(tab_id, article_id);

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

CREATE TABLE IF NOT EXISTS report_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claim_token     TEXT,
    claimed_at      TEXT,
    next_attempt_at TEXT,
    sent_at         TEXT,
    response_json   TEXT NOT NULL DEFAULT '{}',
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_report_deliveries_status_retry
    ON report_deliveries(status, next_attempt_at);

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
        _migrate_event_tab_rule_scope(connection)
        _migrate_article_origin_identity(connection)
        _migrate_article_publish_assignment(connection)
        _migrate_article_draft_confirmation(connection)
        _migrate_article_quality_claim(connection)
        _migrate_source_publish_mode(connection)
        _migrate_tab_and_article_publish_mode(connection)
        _migrate_article_published_tabs(connection)
        _migrate_tab_fallback_litpic(connection)
        _migrate_tab_ai_league_guard(connection)
        _seed_tab_ai_league_guard(connection)
        _migrate_event_tab_routing(connection)
        _migrate_abandon_publish_mode(connection)
        seed_event_tab_rules(connection)
        # Preserve the old single-tab fields as the first item in the new
        # relations. INSERT OR IGNORE makes this safe on every application
        # startup and when upgrading an existing database in place.
        connection.execute(
            """
            INSERT OR IGNORE INTO source_tabs (source_id, tab_id, sort_order)
            SELECT s.id, s.tab_id, 0
            FROM sources s JOIN tabs t ON t.id=s.tab_id
            WHERE s.tab_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM source_tabs st WHERE st.source_id=s.id
              )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO article_tabs (article_id, tab_id, sort_order)
            SELECT a.id, a.tab_id, 0
            FROM articles a JOIN tabs t ON t.id=a.tab_id
            WHERE a.tab_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM article_tabs at WHERE at.article_id=a.id
              )
            """
        )
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


def _migrate_article_origin_identity(connection: sqlite3.Connection) -> None:
    """Add and backfill exact source identities without deleting old rows."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    if "origin_key" not in columns:
        connection.execute("ALTER TABLE articles ADD COLUMN origin_key TEXT")
    if "duplicate_of_article_id" not in columns:
        connection.execute(
            "ALTER TABLE articles ADD COLUMN duplicate_of_article_id INTEGER "
            "REFERENCES articles(id) ON DELETE SET NULL"
        )

    rows = connection.execute(
        """
        SELECT id, source, source_url, status, dqd_archive_id, upstream_archive_id
        FROM articles
        WHERE lower(source)='kbs'
        ORDER BY id
        """
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = source_origin_key(row["source"], row["source_url"])
        if key:
            groups.setdefault(key, []).append(row)

    status_priority = {
        "PUBLISHED": 9,
        "DRAFT_CREATED": 8,
        "ALREADY_PUBLISHED": 7,
        "DRAFT_CONFIRMING": 6,
        "PUBLISHING": 5,
        "READY_TO_PUBLISH": 4,
        "NEEDS_REVIEW": 3,
        "QUALITY_CHECKING": 2,
        "RECEIVED": 1,
    }
    with connection:
        for key, candidates in groups.items():
            assigned = connection.execute(
                "SELECT id FROM articles WHERE lower(source)='kbs' AND origin_key=? LIMIT 1",
                (key,),
            ).fetchone()
            canonical = (
                next(row for row in candidates if int(row["id"]) == int(assigned["id"]))
                if assigned is not None
                else min(
                    candidates,
                    key=lambda row: (
                        -int(int(row["dqd_archive_id"] or 0) > 0),
                        -int(int(row["upstream_archive_id"] or 0) > 0),
                        -status_priority.get(str(row["status"] or ""), 0),
                        int(row["id"]),
                    ),
                )
            )
            canonical_id = int(canonical["id"])
            connection.execute(
                "UPDATE articles SET origin_key=?, duplicate_of_article_id=NULL WHERE id=?",
                (key, canonical_id),
            )
            for duplicate in candidates:
                duplicate_id = int(duplicate["id"])
                if duplicate_id == canonical_id:
                    continue
                previous_status = str(duplicate["status"] or "")
                if previous_status != "SOURCE_DUPLICATE":
                    changed_at = now_iso()
                    connection.execute(
                        """
                        UPDATE articles
                        SET origin_key=NULL, duplicate_of_article_id=?, status='SOURCE_DUPLICATE',
                            error=NULL, updated_at=?
                        WHERE id=?
                        """,
                        (canonical_id, changed_at, duplicate_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO article_events
                        (article_id,from_status,to_status,event_type,message,payload_json,created_at)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            duplicate_id,
                            previous_status,
                            "SOURCE_DUPLICATE",
                            "SOURCE_DUPLICATE_DETECTED",
                            f"与本地文章 #{canonical_id} 的来源文章 ID 相同，已自动拦截",
                            json.dumps(
                                {"origin_key": key, "duplicate_of_article_id": canonical_id},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            changed_at,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE articles
                        SET origin_key=NULL, duplicate_of_article_id=?
                        WHERE id=?
                        """,
                        (canonical_id, duplicate_id),
                    )

        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_origin_key
            ON articles(lower(source), origin_key)
            WHERE origin_key IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_articles_duplicate_of
            ON articles(duplicate_of_article_id)
            """
        )


def _migrate_article_publish_assignment(connection: sqlite3.Connection) -> None:
    """Add publish-account snapshots to existing article databases."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    additions = {
        "publish_account_id": (
            "INTEGER REFERENCES publish_accounts(id) ON DELETE SET NULL"
        ),
        "publish_user_id": "INTEGER",
        "publish_user_name": "TEXT",
        "publish_account_assigned_at": "TEXT",
    }
    with connection:
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE articles ADD COLUMN {name} {declaration}"
                )


def _migrate_article_draft_confirmation(connection: sqlite3.Connection) -> None:
    """Add idempotency and draft-result confirmation fields in place."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    additions = {
        "client_request_id": "TEXT",
        "upstream_request_id": "TEXT",
        "draft_confirm_attempts": (
            "INTEGER NOT NULL DEFAULT 0 CHECK (draft_confirm_attempts >= 0)"
        ),
        "draft_next_confirm_at": "TEXT",
        "draft_uncertain_since": "TEXT",
        "draft_last_attempt_at": "TEXT",
        "draft_confirm_claimed_at": "TEXT",
        "draft_confirm_claim_token": "TEXT",
    }
    with connection:
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE articles ADD COLUMN {name} {declaration}"
                )

        missing_ids = connection.execute(
            """
            SELECT id FROM articles
            WHERE client_request_id IS NULL OR trim(client_request_id)=''
            """
        ).fetchall()
        for row in missing_ids:
            connection.execute(
                "UPDATE articles SET client_request_id=? WHERE id=?",
                (uuid.uuid4().hex, int(row["id"])),
            )

        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_client_request_id
            ON articles(client_request_id)
            WHERE client_request_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_articles_draft_confirmation_due
            ON articles(status, draft_next_confirm_at, draft_confirm_claimed_at)
            """
        )


def _migrate_article_quality_claim(connection: sqlite3.Connection) -> None:
    """Add a lease so quality work is single-flight across processes."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    with connection:
        if "quality_claimed_at" not in columns:
            connection.execute("ALTER TABLE articles ADD COLUMN quality_claimed_at TEXT")
        if "quality_claim_token" not in columns:
            connection.execute("ALTER TABLE articles ADD COLUMN quality_claim_token TEXT")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_articles_quality_claim
            ON articles(status, quality_claimed_at, quality_claim_token)
            """
        )


def _migrate_source_publish_mode(connection: sqlite3.Connection) -> None:
    """Add source-level publish mode fields while preserving old behavior.

    ``publish_mode`` is the legacy non-null field.  The nullable override is
    used by newer callers: ``NULL`` means follow the mapped tab, while 0/1
    explicitly force draft/publish. Existing rows are initialized to their
    legacy value only when the new column is first added, so their behavior
    does not change during migration.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(sources)").fetchall()
    }
    publish_mode_added = "publish_mode" not in columns
    override_added = "publish_mode_override" not in columns
    if publish_mode_added or override_added:
        with connection:
            if publish_mode_added:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN publish_mode INTEGER NOT NULL DEFAULT 0 CHECK (publish_mode IN (0, 1))"
                )
            if override_added:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN publish_mode_override INTEGER CHECK (publish_mode_override IN (0, 1))"
                )
                # Preserve legacy direct-publish settings. Legacy 0 is also
                # the old default and cannot be distinguished from an
                # intentional draft choice, so it follows the tab instead of
                # overriding a newly configured tab-level publish mode.
                connection.execute(
                    "UPDATE sources SET publish_mode_override=1 WHERE publish_mode=1 AND publish_mode_override IS NULL"
                )


def _migrate_tab_and_article_publish_mode(connection: sqlite3.Connection) -> None:
    """Add per-tab mode and immutable article mode snapshot fields.

    ``tabs.publish_mode`` is an operator setting and defaults to draft mode.
    Article fields intentionally remain nullable: a mode is captured only
    when an article is first submitted to the DQD backend, so changing a tab
    setting cannot rewrite an in-flight article's mode.
    """

    tab_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(tabs)").fetchall()
    }
    article_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    tab_column_added = "publish_mode" not in tab_columns
    with connection:
        if tab_column_added:
            connection.execute(
                "ALTER TABLE tabs ADD COLUMN publish_mode INTEGER NOT NULL DEFAULT 0 CHECK (publish_mode IN (0, 1))"
            )
            # Preserve the old source-level direct-publish setting when an
            # existing database is upgraded. Only unambiguous one-mode
            # mappings are promoted; mixed legacy mappings stay in the safe
            # draft mode and can be corrected per tab in the UI.
            connection.execute(
                """
                UPDATE tabs
                SET publish_mode=1
                WHERE id IN (
                    SELECT tab_id
                    FROM sources
                    WHERE tab_id IS NOT NULL
                    GROUP BY tab_id
                    HAVING MIN(publish_mode)=1 AND MAX(publish_mode)=1
                )
                """
            )
        if "publish_mode" not in article_columns:
            connection.execute(
                "ALTER TABLE articles ADD COLUMN publish_mode INTEGER CHECK (publish_mode IN (0, 1))"
            )
        if "publish_mode_decided_at" not in article_columns:
            connection.execute(
                "ALTER TABLE articles ADD COLUMN publish_mode_decided_at TEXT"
            )


def _migrate_article_published_tabs(connection: sqlite3.Connection) -> None:
    """Add the immutable column-name snapshot used by historical reports."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    if "published_tab_names_json" not in columns:
        with connection:
            connection.execute(
                "ALTER TABLE articles ADD COLUMN published_tab_names_json TEXT NOT NULL DEFAULT '[]'"
            )


def _migrate_tab_fallback_litpic(connection: sqlite3.Connection) -> None:
    """Add the optional per-tab CDN fallback cover field."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(tabs)").fetchall()
    }
    if "fallback_litpic" not in columns:
        with connection:
            connection.execute(
                "ALTER TABLE tabs ADD COLUMN fallback_litpic TEXT NOT NULL DEFAULT ''"
            )


def _migrate_tab_ai_league_guard(connection: sqlite3.Connection) -> None:
    """Add per-tab AI league-membership guard settings.

    ``ai_league_guard_enabled`` toggles the pre-publish check; when on, an
    article that reached this fallback tab with an empty league and a direct
    publish mode is verified by the model before publishing.  The optional
    ``ai_league_guard_definition`` describes what the tab actually covers so
    the prompt can be specific.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(tabs)").fetchall()
    }
    with connection:
        if "ai_league_guard_enabled" not in columns:
            connection.execute(
                "ALTER TABLE tabs ADD COLUMN ai_league_guard_enabled "
                "INTEGER NOT NULL DEFAULT 0 CHECK (ai_league_guard_enabled IN (0, 1))"
            )
        if "ai_league_guard_definition" not in columns:
            connection.execute(
                "ALTER TABLE tabs ADD COLUMN ai_league_guard_definition TEXT NOT NULL DEFAULT ''"
            )


_TAB_GUARD_SEEDS: dict[str, str] = {
    "CBA": (
        "本栏目收录以中国男子篮球职业联赛（CBA，China Basketball Association）为主要报道对象的文章，"
        "包括常规赛、季后赛、总决赛及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及 CBA 不算。"
        "2. 主要内容属于 NBA、WNBA、NBL、欧洲篮球或国家队赛事的，不收录。"
        "3. CBA 球队参加亚洲俱乐部赛事时，只有核心报道对象仍是该 CBA 球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "NBA": (
        "本栏目收录以美国男子职业篮球联赛（NBA，National Basketball Association）为主要报道对象的文章，"
        "包括常规赛、季后赛、总决赛、选秀及各参赛球队与球员的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及 NBA 不算。"
        "2. 主要内容属于 WNBA、NBL、CBA、欧洲篮球或国家队赛事的，不收录。"
        "3. NBA 球员参加国家队或表演赛时，只有核心报道对象仍是 NBA 相关才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "WNBA": (
        "本栏目收录以美国女子职业篮球联赛（WNBA，Women's National Basketball Association）为主要报道对象的文章，"
        "包括常规赛、季后赛、总决赛及各参赛球队与球员的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及 WNBA 不算。"
        "2. 主要内容属于 NBA、NBL、CBA 或其他女子联赛的，不收录。"
        "3. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "中超": (
        "本栏目收录以中国足球协会超级联赛（中超，Chinese Super League）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及中超不算。"
        "2. 主要内容属于中甲、亚冠、其他国家联赛或国家队赛事的，不收录。"
        "3. 中超球队参加亚冠等杯赛时，只有核心报道对象仍是该中超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "日职联": (
        "本栏目收录以日本职业足球甲级联赛（J1联赛，J1 League）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及 J联赛不算。"
        "2. 主要内容属于日职乙（J2）、日职丙（J3）、天皇杯、亚冠或国家队赛事的，不收录。"
        "3. J1球队参加杯赛时，只有核心报道对象仍是该 J1 球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "日职乙": (
        "本栏目收录以日本职业足球乙级联赛（J2联赛，J2 League）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于日职联（J1）、日职丙（J3）、天皇杯或国家队赛事的，不收录。"
        "3. J2球队参加杯赛时，只有核心报道对象仍是该 J2 球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "韩K": (
        "本栏目收录以韩国职业足球甲级联赛（K League 1，韩K联）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于韩K2联（K League 2）、韩国足协杯、亚冠或国家队赛事的，不收录。"
        "3. K1球队参加杯赛时，只有核心报道对象仍是该 K1 球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "韩K2联": (
        "本栏目收录以韩国职业足球乙级联赛（K League 2）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于韩K联（K League 1）、韩国足协杯或国家队赛事的，不收录。"
        "3. K2球队参加杯赛时，只有核心报道对象仍是该 K2 球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "沙特联": (
        "本栏目收录以沙特阿拉伯职业足球联赛（Saudi Pro League，沙特联）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于沙特国王杯、亚冠或国家队赛事的，不收录。"
        "3. 沙特联球队参加杯赛时，只有核心报道对象仍是该沙特联球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "澳超": (
        "本栏目收录以澳大利亚足球超级联赛（A-League Men，澳超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于澳大利亚足总杯、亚冠或国家队赛事的，不收录。"
        "3. 澳超球队参加杯赛时，只有核心报道对象仍是该澳超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "印尼超": (
        "本栏目收录以印度尼西亚足球超级联赛（Liga 1，印尼超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于印尼杯、亚冠或国家队赛事的，不收录。"
        "3. 印尼超球队参加杯赛时，只有核心报道对象仍是该印尼超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "巴甲": (
        "本栏目收录以巴西足球甲级联赛（Campeonato Brasileiro Série A，巴甲）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于巴西杯、南美解放者杯、各州联赛或国家队赛事的，不收录。"
        "3. 巴甲球队参加杯赛时，只有核心报道对象仍是该巴甲球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "哥伦甲": (
        "本栏目收录以哥伦比亚足球甲级联赛（Categoría Primera A，哥伦甲）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于哥伦比亚杯、南美解放者杯或国家队赛事的，不收录。"
        "3. 哥伦甲球队参加杯赛时，只有核心报道对象仍是该哥伦甲球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "墨甲": (
        "本栏目收录以墨西哥足球甲级联赛（Liga MX，墨甲）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于墨西哥杯、中北美冠军联赛或国家队赛事的，不收录。"
        "3. 墨甲球队参加杯赛时，只有核心报道对象仍是该墨甲球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "美职联": (
        "本栏目收录以美国足球大联盟（Major League Soccer，MLS 美职联）为主要报道对象的文章，"
        "包括常规赛、季后赛及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于美国公开赛杯、中北美冠军联赛或国家队赛事的，不收录。"
        "3. 美职联球队参加杯赛时，只有核心报道对象仍是该美职联球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "法甲": (
        "本栏目收录以法国足球甲级联赛（Ligue 1，法甲）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于法乙、法国杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 法甲球队参加杯赛时，只有核心报道对象仍是该法甲球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "德乙": (
        "本栏目收录以德国足球乙级联赛（2. Bundesliga，德乙）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于德甲、德丙、德国杯或国家队赛事的，不收录。"
        "3. 德乙球队参加杯赛时，只有核心报道对象仍是该德乙球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "英冠": (
        "本栏目收录以英格兰足球冠军联赛（EFL Championship，英冠）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于英超、英甲、足总杯、联赛杯或国家队赛事的，不收录。"
        "3. 英冠球队参加杯赛时，只有核心报道对象仍是该英冠球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "荷甲": (
        "本栏目收录以荷兰足球甲级联赛（Eredivisie，荷甲）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于荷乙、荷兰杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 荷甲球队参加杯赛时，只有核心报道对象仍是该荷甲球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "葡超": (
        "本栏目收录以葡萄牙足球超级联赛（Primeira Liga，葡超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于葡萄牙杯、葡萄牙联赛杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 葡超球队参加杯赛时，只有核心报道对象仍是该葡超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "土超": (
        "本栏目收录以土耳其足球超级联赛（Süper Lig，土超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于土耳其杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 土超球队参加杯赛时，只有核心报道对象仍是该土超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "挪超": (
        "本栏目收录以挪威足球超级联赛（Eliteserien，挪超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于挪威杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 挪超球队参加杯赛时，只有核心报道对象仍是该挪超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "瑞典超": (
        "本栏目收录以瑞典足球超级联赛（Allsvenskan，瑞典超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于瑞典杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 瑞典超球队参加杯赛时，只有核心报道对象仍是该瑞典超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "芬超": (
        "本栏目收录以芬兰足球超级联赛（Veikkausliiga，芬超）为主要报道对象的文章，"
        "包括联赛赛程、战报及各参赛俱乐部的相关新闻。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于芬兰杯、欧冠、欧联或国家队赛事的，不收录。"
        "3. 芬超球队参加杯赛时，只有核心报道对象仍是该芬超球队才收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "友谊赛": (
        "本栏目收录以足球友谊赛/热身赛（包括俱乐部季前赛、国家队热身赛等非正式竞赛性比赛）为主要报道对象的文章。"
        "判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于任何正式联赛、杯赛、洲际赛或世界大赛正赛的，不收录。"
        "3. 一场明确定性为友谊赛/热身赛/表演赛的比赛才收录。"
        "4. 正文信息不足以确认是否为友谊赛时，按“不属于”处理。"
    ),
    "世俱杯": (
        "本栏目收录以国际足联世俱杯（FIFA Club World Cup，世俱杯）为主要报道对象的文章，"
        "包括赛程、战报及参赛球队在该赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 参赛球队的国内联赛新闻即使提到本赛事也不收录。"
        "3. 主要内容属于欧冠、亚冠、解放者杯等其他洲际赛事的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "亚冠精英": (
        "本栏目收录以亚足联冠军联赛精英联赛（AFC Champions League Elite，亚冠精英）为主要报道对象的文章，"
        "包括赛程、战报及参赛球队在该赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 参赛球队的国内联赛新闻即使提到本赛事也不收录。"
        "3. 主要内容属于亚冠2、世俱杯或其他洲际赛事的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "亚冠2": (
        "本栏目收录以亚足联冠军联赛二级赛事（AFC Champions League Two，亚冠2）为主要报道对象的文章，"
        "包括赛程、战报及参赛球队在该赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 参赛球队的国内联赛新闻即使提到本赛事也不收录。"
        "3. 主要内容属于亚冠精英、亚足联挑战联赛或其他洲际赛事的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "欧冠": (
        "本栏目收录以欧足联冠军联赛（UEFA Champions League，欧冠）为主要报道对象的文章，"
        "包括赛程、战报及参赛球队在该赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 参赛球队的国内联赛新闻即使提到本赛事也不收录。"
        "3. 主要内容属于欧联、欧协联或其他洲际赛事的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "欧联": (
        "本栏目收录以欧足联欧洲联赛（UEFA Europa League，欧联）为主要报道对象的文章，"
        "包括赛程、战报及参赛球队在该赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 参赛球队的国内联赛新闻即使提到本赛事也不收录。"
        "3. 主要内容属于欧冠、欧协联或其他洲际赛事的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "欧协联": (
        "本栏目收录以欧足联欧洲协会联赛（UEFA Europa Conference League，欧协联）为主要报道对象的文章，"
        "包括赛程、战报及参赛球队在该赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 参赛球队的国内联赛新闻即使提到本赛事也不收录。"
        "3. 主要内容属于欧冠、欧联或其他洲际赛事的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
    "欧洲预选": (
        "本栏目收录以欧洲区世界杯预选赛及欧洲杯预选赛（含国家联赛相关的欧洲区预选赛事）为主要报道对象的文章，"
        "包括赛程、战报及各国家队在该预选赛事中的表现。判定要点："
        "1. 只看文章的主要报道对象，不做关键词匹配；仅顺带提及不算。"
        "2. 主要内容属于俱乐部联赛、洲际俱乐部赛事或世界杯/欧洲杯正赛的，不收录。"
        "3. 主要内容属于其他大洲预选赛的，不收录。"
        "4. 正文信息不足以确认时，按“不属于”处理。"
    ),
}


def _seed_tab_ai_league_guard(connection: sqlite3.Connection) -> None:
    """Fill built-in guard definitions for tabs whose definition is still blank.

    Only tabs listed in ``_TAB_GUARD_SEEDS`` are touched, and only when their
    ``ai_league_guard_definition`` is empty, so manual edits are never
    overwritten.  Seeded tabs also have the guard switched on by default.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(tabs)").fetchall()
    }
    if "ai_league_guard_definition" not in columns:
        return
    with connection:
        for name, definition in _TAB_GUARD_SEEDS.items():
            connection.execute(
                "UPDATE tabs SET ai_league_guard_definition=?, "
                "ai_league_guard_enabled=1 "
                "WHERE name=? AND TRIM(ai_league_guard_definition)=''",
                (definition, name),
            )


def _migrate_event_tab_rule_scope(connection: sqlite3.Connection) -> None:
    """Upgrade legacy global rules to source-aware rules without changing IDs."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(event_tab_rules)").fetchall()
    }
    if {"source_code", "publish_mode_override"}.issubset(columns):
        _ensure_event_tab_rule_indexes(connection)
        return
    if "source_code" in columns:
        with connection:
            connection.execute(
                "ALTER TABLE event_tab_rules ADD COLUMN publish_mode_override "
                "INTEGER CHECK (publish_mode_override IN (0, 1))"
            )
        _ensure_event_tab_rule_indexes(connection)
        return

    # The legacy table has an inline UNIQUE(marker_type, marker_code), which
    # cannot be removed with ALTER TABLE. Rebuild it once so a source-specific
    # rule can coexist with its global fallback. Existing IDs remain stable for
    # articles.route_rule_id audit snapshots.
    connection.commit()
    foreign_keys_enabled = bool(
        connection.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    if foreign_keys_enabled:
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE event_tab_rules_v2 (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_code     TEXT REFERENCES sources(code) ON UPDATE CASCADE ON DELETE CASCADE,
                marker_type     TEXT NOT NULL CHECK (marker_type IN ('league', 'team')),
                marker_code     TEXT NOT NULL CHECK (length(trim(marker_code)) > 0),
                tab_id          INTEGER REFERENCES tabs(id) ON DELETE SET NULL,
                publish_mode_override INTEGER CHECK (publish_mode_override IN (0, 1, 2)),
                enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                first_seen_at   TEXT,
                last_seen_at    TEXT,
                sample_source   TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            INSERT INTO event_tab_rules_v2
            (id, source_code, marker_type, marker_code, tab_id,
             publish_mode_override, enabled, first_seen_at, last_seen_at,
             sample_source, created_at, updated_at)
            SELECT id, NULL, marker_type, marker_code, tab_id,
                   NULL, enabled, first_seen_at, last_seen_at,
                   sample_source, created_at, updated_at
            FROM event_tab_rules;
            DROP TABLE event_tab_rules;
            ALTER TABLE event_tab_rules_v2 RENAME TO event_tab_rules;
            CREATE INDEX idx_event_tab_rules_pending
                ON event_tab_rules(marker_type, enabled, tab_id, marker_code, source_code);
            CREATE UNIQUE INDEX idx_event_tab_rules_global_marker
                ON event_tab_rules(marker_type, marker_code)
                WHERE source_code IS NULL;
            CREATE UNIQUE INDEX idx_event_tab_rules_source_marker
                ON event_tab_rules(source_code, marker_type, marker_code)
                WHERE source_code IS NOT NULL;
            COMMIT;
            """
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if foreign_keys_enabled:
            connection.execute("PRAGMA foreign_keys = ON")


def _ensure_event_tab_rule_indexes(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_tab_rules_pending "
            "ON event_tab_rules(marker_type, enabled, tab_id, marker_code, source_code)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_tab_rules_global_marker "
            "ON event_tab_rules(marker_type, marker_code) WHERE source_code IS NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_tab_rules_source_marker "
            "ON event_tab_rules(source_code, marker_type, marker_code) "
            "WHERE source_code IS NOT NULL"
        )


def _migrate_event_tab_routing(connection: sqlite3.Connection) -> None:
    """Add immutable material-routing diagnostics to existing articles."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    additions = {
        "material_user_name": "TEXT NOT NULL DEFAULT ''",
        "route_league": "TEXT NOT NULL DEFAULT ''",
        "route_team": "TEXT NOT NULL DEFAULT ''",
        "route_match_type": (
            "TEXT NOT NULL DEFAULT 'source' "
            "CHECK (route_match_type IN ('source', 'league', 'team'))"
        ),
        "route_rule_id": (
            "INTEGER REFERENCES event_tab_rules(id) ON DELETE SET NULL"
        ),
    }
    with connection:
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE articles ADD COLUMN {name} {declaration}"
                )


def _migrate_abandon_publish_mode(connection: sqlite3.Connection) -> None:
    """Relax the publish-mode CHECK constraints so ``2`` (abandon) is allowed.

    Only event-tab-rule overrides and the resulting article snapshot may use the
    abandon mode; tabs/sources keep their ``IN (0, 1)`` constraint.  SQLite
    cannot ALTER a CHECK constraint, so rebuild the affected tables in place for
    existing databases.  Fresh databases already carry the relaxed definition.
    """

    targets = {
        "event_tab_rules": (
            "CHECK (publish_mode_override IN (0, 1))",
            "CHECK (publish_mode_override IN (0, 1, 2))",
        ),
        "articles": (
            "CHECK (publish_mode IN (0, 1))",
            "CHECK (publish_mode IN (0, 1, 2))",
        ),
    }
    for name, (old_clause, new_clause) in targets.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if row is None or not row["sql"] or old_clause not in str(row["sql"]):
            continue
        new_table_sql = str(row["sql"]).replace(old_clause, new_clause)
        _rebuild_table_with_new_schema(connection, name, new_table_sql)


def _rebuild_table_with_new_schema(
    connection: sqlite3.Connection, name: str, new_table_sql: str
) -> None:
    """Recreate ``name`` from ``new_table_sql`` preserving rows and indexes.

    Follows the SQLite-recommended table rebuild: copy into a temp table, drop
    the original, recreate it and copy the data back by matching column names so
    the primary key and existing IDs stay stable.
    """

    columns = [
        str(col_row["name"])
        for col_row in connection.execute(f"PRAGMA table_info({name})").fetchall()
    ]
    column_list = ", ".join(columns)
    index_sql = [
        str(idx_row["sql"])
        for idx_row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL",
            (name,),
        ).fetchall()
    ]
    temp_name = f"{name}_abandon_rebuild"
    # 表名可能带引号或不带引号，都要尝试替换
    new_table_sql = new_table_sql.replace(f'TABLE "{name}"', f'TABLE "{temp_name}"', 1)
    new_table_sql = new_table_sql.replace(f"TABLE {name}", f"TABLE {temp_name}", 1)

    connection.commit()
    foreign_keys_enabled = bool(
        connection.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    if foreign_keys_enabled:
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 清理可能残留的临时表（之前失败的迁移）
        connection.execute(f"DROP TABLE IF EXISTS {temp_name}")
        connection.execute(new_table_sql)
        connection.execute(
            f"INSERT INTO {temp_name} ({column_list}) "
            f"SELECT {column_list} FROM {name}"
        )
        connection.execute(f"DROP TABLE {name}")
        connection.execute(f"ALTER TABLE {temp_name} RENAME TO {name}")
        for statement in index_sql:
            connection.execute(statement)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if foreign_keys_enabled:
            connection.execute("PRAGMA foreign_keys = ON")


_DEFAULT_EVENT_TAB_RULES = (
    ("ered", 380),
    ("ucl", 357),
    ("bl2", 370),
    ("ligue1", 12),
    ("uel", 358),
    ("superlig", 377),
    ("brasil", 378),
    ("svensk", 361),
    ("elite", 363),
    ("mls", 362),
    ("aleague", 348),
    ("csl", 56),
    ("primeira", 379),
    ("efl_ch", 373),
    ("jleague", 349),
    ("j2league", 376),
    ("kleague1", 359),
    ("ligamx", 383),
    ("nba", 289),
    ("cba", 290),
    ("cwc", 360),
)


def seed_event_tab_rules(connection: sqlite3.Connection) -> None:
    """Preconfigure only unambiguous rules whose local tab already exists.

    The unique key plus ``INSERT OR IGNORE`` is intentional: startup must
    never overwrite an operator's later edit or a deliberately cleared rule.
    """

    current = now_iso()
    with connection:
        for marker_code, backend_tab_id in _DEFAULT_EVENT_TAB_RULES:
            connection.execute(
                """
                INSERT OR IGNORE INTO event_tab_rules
                (source_code, marker_type, marker_code, tab_id,
                 publish_mode_override, enabled, created_at, updated_at)
                SELECT NULL, 'league', ?, id, NULL, 1, ?, ?
                FROM tabs
                WHERE backend_tab_id=?
                """,
                (marker_code, current, current, backend_tab_id),
            )


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
