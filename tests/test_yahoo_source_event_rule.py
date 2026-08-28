from __future__ import annotations

import sqlite3

import pytest

from app import repository as repo
from app.db import _connect, init_db


def _tabs():
    return {tab["backend_tab_id"]: tab for tab in repo.list_tabs()}


def _material(source: str, suffix: str, *, league: str, team: str = "_"):
    return {
        "source": source,
        "source_url": f"https://example.com/source-event-rule/{source}/{suffix}",
        "translate_title": "来源级赛事规则测试",
        "translate_body": "<p>这是一篇只用于本地测试的完整体育新闻正文。</p>",
        "channels": [100, 200],
        "user_name": f"{source}:fb.{league}.{team}",
    }


def _configure_sources():
    tabs = _tabs()
    repo.update_source("yahoojp", tab_ids=[tabs[348]["id"]], enabled=True)
    repo.update_source("nikkan", tab_ids=[tabs[348]["id"]], enabled=True)
    return tabs


def test_source_rule_is_isolated_and_other_source_inherits_global_rule(app):
    with app.app_context():
        tabs = _configure_sources()
        global_rule = repo.create_event_tab_rule(
            "league", "j1", tabs[359]["id"]
        )
        yahoo_rule = repo.create_event_tab_rule(
            "league", "j1", tabs[349]["id"], source_code="yahoojp"
        )

        yahoo = repo.upsert_material(
            _material("yahoojp", "scoped", league="j1")
        )["article"]
        nikkan = repo.upsert_material(
            _material("nikkan", "global", league="j1")
        )["article"]

        assert yahoo["backend_tab_ids"] == [349]
        assert yahoo["route_rule_id"] == yahoo_rule["id"]
        assert nikkan["backend_tab_ids"] == [359]
        assert nikkan["route_rule_id"] == global_rule["id"]


def test_rule_priority_keeps_team_above_league_across_scopes(app):
    with app.app_context():
        tabs = _configure_sources()
        source_league = repo.create_event_tab_rule(
            "league", "j1_priority", tabs[349]["id"], source_code="yahoojp"
        )
        global_team = repo.create_event_tab_rule(
            "team", "club_priority", tabs[359]["id"]
        )

        article = repo.upsert_material(
            _material(
                "yahoojp", "global-team-over-source-league",
                league="j1_priority", team="club_priority",
            )
        )["article"]

        assert article["backend_tab_ids"] == [359]
        assert article["route_match_type"] == "team"
        assert article["route_rule_id"] == global_team["id"]
        assert article["route_rule_id"] != source_league["id"]


def test_source_team_wins_over_global_team(app):
    with app.app_context():
        tabs = _configure_sources()
        repo.create_event_tab_rule("team", "club_scope", tabs[359]["id"])
        source_team = repo.create_event_tab_rule(
            "team", "club_scope", tabs[349]["id"], source_code="yahoojp"
        )

        article = repo.upsert_material(
            _material(
                "yahoojp", "source-team-over-global-team",
                league="_", team="club_scope",
            )
        )["article"]

        assert article["backend_tab_ids"] == [349]
        assert article["route_match_type"] == "team"
        assert article["route_rule_id"] == source_team["id"]


@pytest.mark.parametrize("state", ["disabled", "pending", "tab_disabled"])
def test_unusable_source_rule_falls_through_to_global_rule(app, state):
    with app.app_context():
        tabs = _configure_sources()
        marker = f"j1_fallback_{state}"
        global_rule = repo.create_event_tab_rule(
            "league", marker, tabs[359]["id"]
        )
        source_rule = repo.create_event_tab_rule(
            "league", marker, tabs[349]["id"], source_code="yahoojp"
        )
        if state == "disabled":
            repo.update_event_tab_rule(source_rule["id"], enabled=False)
        elif state == "pending":
            repo.update_event_tab_rule(source_rule["id"], tab_id=None)
        else:
            repo.update_tab(tabs[349]["id"], enabled=False)

        article = repo.upsert_material(
            _material("yahoojp", f"source-fallback-{state}", league=marker)
        )["article"]

        assert article["backend_tab_ids"] == [359]
        assert article["route_rule_id"] == global_rule["id"]


def test_unknown_league_keeps_source_tabs_and_creates_global_pending_rule(app):
    with app.app_context():
        tabs = _configure_sources()

        article = repo.upsert_material(
            _material("yahoojp", "unknown", league="j1_unknown")
        )["article"]
        pending = [
            rule for rule in repo.list_event_tab_rules(pending_only=True)
            if rule["marker_type"] == "league"
            and rule["marker_code"] == "j1_unknown"
        ]

        assert article["backend_tab_ids"] == [348]
        assert article["route_match_type"] == "source"
        assert article["route_rule_id"] is None
        assert len(pending) == 1
        assert pending[0]["source_code"] is None
        assert pending[0]["sample_source"] == "yahoojp"
        assert tabs[348]["id"] in article["tab_ids"]


def test_existing_article_is_not_rerouted_after_source_rule_is_added(app):
    with app.app_context():
        tabs = _configure_sources()
        source_url_suffix = "existing-before-rule"
        first = repo.upsert_material(
            _material("yahoojp", source_url_suffix, league="j1_late")
        )["article"]
        source_rule = repo.create_event_tab_rule(
            "league", "j1_late", tabs[349]["id"], source_code="yahoojp"
        )

        repeated = repo.upsert_material(
            _material("yahoojp", source_url_suffix, league="j1_late")
        )["article"]
        fresh = repo.upsert_material(
            _material("yahoojp", "fresh-after-rule", league="j1_late")
        )["article"]

        assert repeated["id"] == first["id"]
        assert repeated["backend_tab_ids"] == [348]
        assert repeated["route_rule_id"] is None
        assert fresh["backend_tab_ids"] == [349]
        assert fresh["route_rule_id"] == source_rule["id"]


def test_rule_publish_action_is_editable_before_snapshot_and_stable_after(app):
    with app.app_context():
        tabs = _configure_sources()
        repo.update_tab(tabs[349]["id"], publish_mode=0)
        repo.update_source("yahoojp", publish_mode_override=0)
        rule = repo.create_event_tab_rule(
            "league", "j1_action", tabs[349]["id"], source_code="yahoojp",
            publish_mode_override=1,
        )
        article = repo.upsert_material(
            _material("yahoojp", "publish-action", league="j1_action")
        )["article"]

        before = repo.resolve_article_publish_mode(article["id"])
        assert before["publish_mode"] == 1
        assert before["route_rule_publish_mode_override"] == 1

        repo.update_event_tab_rule(rule["id"], publish_mode_override=0)
        captured = repo.ensure_article_publish_mode(article["id"])
        assert captured["publish_mode"] == 0
        assert captured["publish_mode_decided_at"]

        repo.update_event_tab_rule(rule["id"], publish_mode_override=1)
        repo.update_source("yahoojp", publish_mode_override=1)
        repo.update_tab(tabs[349]["id"], publish_mode=1)
        after = repo.resolve_article_publish_mode(article["id"])
        assert after["snapshot"] == 0
        assert after["publish_mode"] == 0
        assert after["conflict"] is False


def test_rule_publish_action_precedes_source_override_and_tab_conflict(app):
    with app.app_context():
        tabs = _configure_sources()
        repo.update_source(
            "yahoojp",
            tab_ids=[tabs[58]["id"], tabs[348]["id"]],
            publish_mode_override=0,
        )
        repo.update_tab(tabs[58]["id"], publish_mode=0)
        repo.update_tab(tabs[349]["id"], publish_mode=1)
        rule = repo.create_event_tab_rule(
            "league", "j1_conflict", tabs[349]["id"], source_code="yahoojp",
            publish_mode_override=1,
        )
        article = repo.upsert_material(
            _material("yahoojp", "publish-conflict", league="j1_conflict")
        )["article"]

        resolved = repo.resolve_article_publish_mode(article["id"])

        assert article["backend_tab_ids"] == [58, 349]
        assert article["route_rule_id"] == rule["id"]
        assert resolved["tab_conflict"] is True
        assert resolved["source_publish_mode_override"] == 0
        assert resolved["route_rule_publish_mode_override"] == 1
        assert resolved["publish_mode"] == 1
        assert resolved["conflict"] is False


@pytest.mark.parametrize("rule_change", ["disabled", "tab_cleared"])
def test_unusable_rule_action_follows_source_before_first_snapshot(app, rule_change):
    with app.app_context():
        tabs = _configure_sources()
        repo.update_source("yahoojp", publish_mode_override=0)
        rule = repo.create_event_tab_rule(
            "league", f"j1_action_{rule_change}", tabs[349]["id"],
            source_code="yahoojp", publish_mode_override=1,
        )
        article = repo.upsert_material(
            _material(
                "yahoojp", f"action-{rule_change}",
                league=f"j1_action_{rule_change}",
            )
        )["article"]
        assert repo.resolve_article_publish_mode(article["id"])["publish_mode"] == 1

        if rule_change == "disabled":
            repo.update_event_tab_rule(rule["id"], enabled=False)
        else:
            repo.update_event_tab_rule(rule["id"], tab_id=None)

        resolved = repo.resolve_article_publish_mode(article["id"])
        captured = repo.ensure_article_publish_mode(article["id"])
        assert resolved["route_rule_publish_mode_override"] is None
        assert resolved["publish_mode"] == 0
        assert captured["publish_mode"] == 0


def test_global_and_multiple_source_rules_can_share_one_marker(app):
    with app.app_context():
        tabs = _configure_sources()
        repo.create_event_tab_rule("league", "shared_j1", tabs[348]["id"])
        repo.create_event_tab_rule(
            "league", "shared_j1", tabs[349]["id"], source_code="yahoojp"
        )
        repo.create_event_tab_rule(
            "league", "shared_j1", tabs[359]["id"], source_code="nikkan"
        )

        rules = [
            rule for rule in repo.list_event_tab_rules()
            if rule["marker_type"] == "league"
            and rule["marker_code"] == "shared_j1"
        ]
        assert {rule["source_code"] for rule in rules} == {
            None, "yahoojp", "nikkan"
        }

        with pytest.raises(sqlite3.IntegrityError):
            repo.create_event_tab_rule(
                "league", "shared_j1", tabs[349]["id"], source_code="yahoojp"
            )


def test_source_rule_api_is_generic_and_preserves_global_filter(app, client):
    with app.app_context():
        tabs = _configure_sources()

    scoped_response = client.post(
        "/api/event-tab-rules",
        json={
            "source_code": "yahoojp",
            "marker_type": "league",
            "marker_code": "j1_api",
            "tab_id": tabs[349]["id"],
            "publish_mode_override": 1,
            "enabled": True,
        },
    )
    global_response = client.post(
        "/api/event-tab-rules",
        json={
            "source_code": None,
            "marker_type": "league",
            "marker_code": "j1_api",
            "tab_id": tabs[359]["id"],
            "publish_mode_override": None,
            "enabled": True,
        },
    )

    assert scoped_response.status_code == 200
    assert global_response.status_code == 200
    scoped = scoped_response.get_json()["event_tab_rule"]
    assert scoped["source_code"] == "yahoojp"
    assert scoped["publish_mode_override"] == 1

    scoped_list = client.get(
        "/api/event-tab-rules?source_code=yahoojp&search=j1_api"
    ).get_json()["event_tab_rules"]
    global_list = client.get(
        "/api/event-tab-rules?source_code=&search=j1_api"
    ).get_json()["event_tab_rules"]
    assert [rule["source_code"] for rule in scoped_list] == ["yahoojp"]
    assert [rule["source_code"] for rule in global_list] == [None]

    invalid_source = client.post(
        "/api/event-tab-rules",
        json={
            "source_code": "missing-source",
            "marker_type": "league",
            "marker_code": "j1_missing_source",
        },
    )
    assert invalid_source.status_code == 400
    assert "source not found" in invalid_source.get_json()["error"]


def test_legacy_global_rule_migration_keeps_rule_id_and_article_reference(tmp_path):
    database = tmp_path / "legacy-source-event-rule.sqlite3"
    init_db(database)
    conn = _connect(database)
    try:
        now = "2026-08-27T00:00:00.000Z"
        tab_id = conn.execute(
            """
            INSERT INTO tabs
            (backend_tab_id, name, enabled, publish_mode, fallback_litpic,
             created_at, updated_at)
            VALUES (349, '旧库日职联', 1, 0, '', ?, ?)
            """,
            (now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO sources
            (code, display_name, enabled, publish_mode, created_at, updated_at)
            VALUES ('yahoojp', '旧库 Yahoo', 0, 0, ?, ?)
            """,
            (now, now),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("DROP INDEX idx_event_tab_rules_pending")
        conn.execute("DROP INDEX idx_event_tab_rules_global_marker")
        conn.execute("DROP INDEX idx_event_tab_rules_source_marker")
        conn.execute("ALTER TABLE event_tab_rules RENAME TO event_tab_rules_current")
        conn.execute(
            """
            CREATE TABLE event_tab_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marker_type TEXT NOT NULL,
                marker_code TEXT NOT NULL,
                tab_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT,
                last_seen_at TEXT,
                sample_source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(marker_type, marker_code)
            )
            """
        )
        rule_id = conn.execute(
            """
            INSERT INTO event_tab_rules
            (id, marker_type, marker_code, tab_id, enabled, created_at, updated_at)
            VALUES (7001, 'league', 'legacy_j1', ?, 1, ?, ?)
            """,
            (tab_id, now, now),
        ).lastrowid
        article_id = conn.execute(
            """
            INSERT INTO articles
            (source, source_url, route_rule_id, created_at, updated_at, last_seen_at)
            VALUES ('yahoojp', 'https://example.com/legacy/source-rule', ?, ?, ?, ?)
            """,
            (rule_id, now, now, now),
        ).lastrowid
        conn.execute("DROP TABLE event_tab_rules_current")
        conn.commit()
    finally:
        conn.close()

    init_db(database)
    conn = _connect(database)
    try:
        migrated = conn.execute(
            "SELECT * FROM event_tab_rules WHERE id=?", (rule_id,)
        ).fetchone()
        article = conn.execute(
            "SELECT route_rule_id FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        assert migrated["source_code"] is None
        assert migrated["publish_mode_override"] is None
        assert article["route_rule_id"] == rule_id
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        conn.execute(
            """
            INSERT INTO event_tab_rules
            (source_code, marker_type, marker_code, tab_id, enabled)
            VALUES ('yahoojp', 'league', 'legacy_j1', ?, 1)
            """,
            (tab_id,),
        )
        conn.commit()
    finally:
        conn.close()
