from __future__ import annotations

import pytest

from app import repository as repo
from app.db import init_db
from app.services.material_client import normalize_item


def _material(source_url: str, user_name: str, **overrides):
    item = {
        "source": "marca",
        "source_url": source_url,
        "translate_title": "测试赛事栏目路由",
        "translate_body": "<p>这是一篇用于验证赛事栏目路由的完整测试正文。</p>",
        "channels": [11, 22],
        "user_name": user_name,
    }
    item.update(overrides)
    return item


def _routing_tabs():
    by_backend_id = {tab["backend_tab_id"]: tab for tab in repo.list_tabs()}
    generic = by_backend_id[58]
    aleague = by_backend_id[348]
    jleague = by_backend_id[349]
    kleague = by_backend_id[359]
    return generic, aleague, jleague, kleague


def _configure_rule(marker_type: str, marker_code: str, tab_id: int, enabled=True):
    existing = next(
        (
            rule
            for rule in repo.list_event_tab_rules()
            if rule["marker_type"] == marker_type
            and rule["marker_code"] == marker_code
        ),
        None,
    )
    if existing:
        return repo.update_event_tab_rule(
            existing["id"], tab_id=tab_id, enabled=enabled
        )
    return repo.create_event_tab_rule(
        marker_type, marker_code, tab_id, enabled
    )


@pytest.mark.parametrize(
    ("value", "valid", "league", "team"),
    [
        ("nikkan:fb.jleague.marinos", True, "jleague", "marinos"),
        (" nikkan:fb.JLEAGUE.MARINOS ", True, "jleague", "marinos"),
        ("nikkan:fb._._", True, "", ""),
        ("nikkan:fb.xxx.null", True, "", ""),
        ("nikkan:fb..none", True, "", ""),
        ("", False, "", ""),
        ("nikkan", False, "", ""),
        ("nikkan:fb.jleague", False, "", ""),
        ("nikkan:fb.jleague.team.extra", False, "", ""),
        ("nikkan:fb.jleague.bad team", True, "jleague", ""),
    ],
)
def test_parse_material_user_name_handles_contract_and_missing_markers(
    value, valid, league, team
):
    parsed = repo.parse_material_user_name(value)

    assert parsed["valid"] is valid
    assert parsed["league"] == league
    assert parsed["team"] == team


@pytest.mark.parametrize("field_name", ["user_name", "username"])
def test_material_client_preserves_upstream_marker_separately(field_name):
    raw = {
        "source": "marca",
        "source_url": f"https://example.com/routing/normalize/{field_name}",
        "translate_title": "素材字段透传测试",
        "translate_body": "<p>素材字段需要完整透传到栏目路由。</p>",
        "channels": [],
        field_name: "marca:fb.jleague.marinos",
    }

    normalized = normalize_item(raw)

    assert normalized["material_user_name"] == "marca:fb.jleague.marinos"
    assert normalized["raw_payload"] is raw
    assert "publish_user_name" not in normalized


def test_team_rule_wins_and_unconfigured_team_falls_back_to_league(app):
    with app.app_context():
        generic, aleague, jleague, kleague = _routing_tabs()
        repo.update_source(
            "marca", tab_ids=[generic["id"], aleague["id"]], enabled=True
        )
        league_rule = _configure_rule(
            "league", "jleague", jleague["id"]
        )
        team_rule = repo.create_event_tab_rule("team", "marinos", kleague["id"])

        team_match = repo.upsert_material(
            _material(
                "https://example.com/routing/team",
                "nikkan:fb.jleague.marinos",
            )
        )["article"]
        league_match = repo.upsert_material(
            _material(
                "https://example.com/routing/league-fallback",
                "nikkan:fb.jleague.unknown_team",
            )
        )["article"]

        assert team_match["backend_tab_ids"] == [58, 359]
        assert team_match["route_match_type"] == "team"
        assert team_match["route_rule_id"] == team_rule["id"]
        assert team_match["route_league"] == "jleague"
        assert team_match["route_team"] == "marinos"
        assert team_match["channels"] == [11, 22]

        assert league_match["backend_tab_ids"] == [58, 349]
        assert league_match["route_match_type"] == "league"
        assert league_match["route_rule_id"] == league_rule["id"]
        assert not any(
            rule["marker_type"] == "team"
            and rule["marker_code"] == "unknown_team"
            for rule in repo.list_event_tab_rules()
        )


@pytest.mark.parametrize("team_state", ["disabled", "pending", "tab_disabled"])
def test_unusable_team_rule_falls_back_to_league(app, team_state):
    with app.app_context():
        _, aleague, jleague, kleague = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)
        league_rule = _configure_rule("league", "jleague", jleague["id"])
        team_rule = repo.create_event_tab_rule(
            "team", f"unusable_{team_state}", kleague["id"]
        )
        if team_state == "disabled":
            repo.update_event_tab_rule(team_rule["id"], enabled=False)
        elif team_state == "pending":
            repo.update_event_tab_rule(team_rule["id"], tab_id=None)
        else:
            repo.update_tab(kleague["id"], enabled=False)

        article = repo.upsert_material(
            _material(
                f"https://example.com/routing/team-{team_state}",
                f"nikkan:fb.jleague.unusable_{team_state}",
            )
        )["article"]

        assert article["backend_tab_ids"] == [349]
        assert article["route_match_type"] == "league"
        assert article["route_rule_id"] == league_rule["id"]


@pytest.mark.parametrize("league_state", ["disabled", "pending", "tab_disabled"])
def test_unusable_league_rule_falls_back_to_source(app, league_state):
    with app.app_context():
        _, aleague, jleague, _ = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)
        rule = repo.create_event_tab_rule(
            "league", f"unusable_{league_state}", jleague["id"]
        )
        if league_state == "disabled":
            repo.update_event_tab_rule(rule["id"], enabled=False)
        elif league_state == "pending":
            repo.update_event_tab_rule(rule["id"], tab_id=None)
        else:
            repo.update_tab(jleague["id"], enabled=False)

        article = repo.upsert_material(
            _material(
                f"https://example.com/routing/league-{league_state}",
                f"nikkan:fb.unusable_{league_state}._",
            )
        )["article"]

        assert article["backend_tab_ids"] == [348]
        assert article["route_match_type"] == "source"
        assert article["route_rule_id"] is None


def test_team_rule_can_match_when_league_is_missing(app):
    with app.app_context():
        _, aleague, _, kleague = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)
        team_rule = repo.create_event_tab_rule("team", "marinos_only", kleague["id"])

        article = repo.upsert_material(
            _material(
                "https://example.com/routing/team-without-league",
                "nikkan:fb._.marinos_only",
            )
        )["article"]

        assert article["backend_tab_ids"] == [359]
        assert article["route_match_type"] == "team"
        assert article["route_rule_id"] == team_rule["id"]


def test_match_keeps_only_existing_backend_58_before_target(app):
    with app.app_context():
        generic, aleague, jleague, _ = _routing_tabs()
        extra = repo.create_tab("测试额外赛事", 99901)
        repo.update_source(
            "marca",
            tab_ids=[aleague["id"], generic["id"], extra["id"]],
            enabled=True,
        )
        _configure_rule("league", "jleague", jleague["id"])

        routed = repo.upsert_material(
            _material(
                "https://example.com/routing/generic-retention",
                "nikkan:fb.jleague._",
            )
        )["article"]

        assert routed["backend_tab_ids"] == [58, 349]


def test_match_does_not_add_backend_58_when_source_did_not_have_it(app):
    with app.app_context():
        _, aleague, jleague, _ = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)
        _configure_rule("league", "jleague", jleague["id"])

        routed = repo.upsert_material(
            _material(
                "https://example.com/routing/no-generic",
                "nikkan:fb.jleague._",
            )
        )["article"]

        assert routed["backend_tab_ids"] == [349]


@pytest.mark.parametrize(
    "user_name",
    [
        "nikkan:fb._._",
        "nikkan:fb.unknown_league._",
        "malformed-value",
        "",
    ],
)
def test_missing_unknown_or_malformed_marker_keeps_all_source_tabs(app, user_name):
    with app.app_context():
        generic, aleague, _, _ = _routing_tabs()
        repo.update_source(
            "marca", tab_ids=[generic["id"], aleague["id"]], enabled=True
        )

        article = repo.upsert_material(
            _material(
                f"https://example.com/routing/fallback/{user_name or 'empty'}",
                user_name,
            )
        )["article"]

        assert article["backend_tab_ids"] == [58, 348]
        assert article["route_match_type"] == "source"
        assert article["route_rule_id"] is None

        pending = [
            rule
            for rule in repo.list_event_tab_rules(pending_only=True)
            if rule["marker_code"] == "unknown_league"
        ]
        assert len(pending) == (1 if user_name == "nikkan:fb.unknown_league._" else 0)


def test_rule_update_is_immediate_but_does_not_rewrite_existing_article(app):
    with app.app_context():
        _, aleague, jleague, kleague = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)
        rule = _configure_rule("league", "jleague", jleague["id"])

        first = repo.upsert_material(
            _material(
                "https://example.com/routing/before-rule-update",
                "nikkan:fb.jleague._",
            )
        )["article"]
        repo.update_event_tab_rule(rule["id"], tab_id=kleague["id"])
        repeated = repo.upsert_material(
            _material(
                "https://example.com/routing/before-rule-update",
                "nikkan:fb.jleague._",
                translate_title="重复抓取不应改栏目",
            )
        )["article"]
        second = repo.upsert_material(
            _material(
                "https://example.com/routing/after-rule-update",
                "nikkan:fb.jleague._",
            )
        )["article"]

        assert first["backend_tab_ids"] == [349]
        assert repeated["id"] == first["id"]
        assert repeated["backend_tab_ids"] == [349]
        assert repeated["route_rule_id"] == first["route_rule_id"]
        assert second["backend_tab_ids"] == [359]
        assert second["route_rule_id"] == rule["id"]


def test_reingest_does_not_route_or_observe_new_marker_for_existing_article(app):
    with app.app_context():
        _, aleague, _, _ = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)
        source_url = "https://example.com/routing/reingest-new-marker"
        first = repo.upsert_material(
            _material(source_url, "nikkan:fb._._")
        )["article"]

        repeated = repo.upsert_material(
            _material(source_url, "nikkan:fb.lateleague._")
        )["article"]

        assert repeated["id"] == first["id"]
        assert repeated["backend_tab_ids"] == [348]
        assert repeated["route_league"] == ""
        assert not any(
            rule["marker_type"] == "league"
            and rule["marker_code"] == "lateleague"
            for rule in repo.list_event_tab_rules()
        )


def test_unknown_league_creates_pending_rule_and_later_configuration_applies(app):
    with app.app_context():
        _, aleague, jleague, _ = _routing_tabs()
        repo.update_source("marca", tab_ids=[aleague["id"]], enabled=True)

        first = repo.upsert_material(
            _material(
                "https://example.com/routing/new-league-first",
                "nikkan:fb.futureleague._",
            )
        )["article"]
        pending = next(
            rule
            for rule in repo.list_event_tab_rules(pending_only=True)
            if rule["marker_code"] == "futureleague"
        )
        assert pending["marker_type"] == "league"
        assert pending["sample_source"] == "marca"
        assert pending["first_seen_at"]
        assert pending["last_seen_at"]
        assert first["backend_tab_ids"] == [348]

        repo.update_event_tab_rule(pending["id"], tab_id=jleague["id"])
        second = repo.upsert_material(
            _material(
                "https://example.com/routing/new-league-second",
                "nikkan:fb.futureleague._",
            )
        )["article"]

        assert second["backend_tab_ids"] == [349]
        assert second["route_match_type"] == "league"


def test_fresh_app_seeds_confirmed_rules_and_does_not_overwrite_operator_change(app):
    database = app.config["DATABASE"]
    with app.app_context():
        _, _, jleague, kleague = _routing_tabs()
        confirmed = {
            rule["marker_code"]: rule["backend_tab_id"]
            for rule in repo.list_event_tab_rules(marker_type="league")
            if rule["tab_id"] is not None
        }
        assert len(confirmed) == 21
        assert confirmed["jleague"] == 349
        seeded = next(
            rule
            for rule in repo.list_event_tab_rules()
            if rule["marker_type"] == "league" and rule["marker_code"] == "jleague"
        )
        assert seeded["tab_id"] == jleague["id"]
        repo.update_event_tab_rule(seeded["id"], tab_id=kleague["id"], enabled=False)

    init_db(database)
    with app.app_context():
        preserved = repo.get_event_tab_rule(seeded["id"])
        assert preserved["tab_id"] == kleague["id"]
        assert preserved["enabled"] == 0


def test_event_tab_rule_api_supports_create_list_update_and_clear(app, client):
    with app.app_context():
        target = repo.create_tab("API 测试栏目", 99911)

    created_response = client.post(
        "/api/event-tab-rules",
        json={
            "marker_type": "league",
            "marker_code": "futureleague",
            "tab_id": target["id"],
            "enabled": True,
        },
    )
    assert created_response.status_code == 200
    created = created_response.get_json()["event_tab_rule"]
    assert created["configured"] is True
    assert created["backend_tab_id"] == 99911

    listed = client.get(
        "/api/event-tab-rules?marker_type=league&search=future&pending=false"
    )
    assert listed.status_code == 200
    assert [rule["id"] for rule in listed.get_json()["event_tab_rules"]] == [
        created["id"]
    ]

    cleared_response = client.post(
        f"/api/event-tab-rules/{created['id']}",
        json={"tab_id": None, "enabled": False},
    )
    assert cleared_response.status_code == 200
    cleared = cleared_response.get_json()["event_tab_rule"]
    assert cleared["configured"] is False
    assert cleared["tab_id"] is None
    assert cleared["enabled"] == 0

    pending = client.get("/api/event-tab-rules?pending=true")
    assert pending.status_code == 200
    assert created["id"] in {
        rule["id"] for rule in pending.get_json()["event_tab_rules"]
    }


def test_event_tab_rule_api_rejects_duplicate_and_invalid_payloads(app, client):
    with app.app_context():
        target = repo.create_tab("API 校验栏目", 99912)

    payload = {
        "marker_type": "league",
            "marker_code": "api_duplicate_league",
        "tab_id": target["id"],
        "enabled": True,
    }
    assert client.post("/api/event-tab-rules", json=payload).status_code == 200

    duplicate = client.post("/api/event-tab-rules", json=payload)
    assert duplicate.status_code == 400
    assert "已存在" in duplicate.get_json()["error"]

    for invalid_payload in (
        {**payload, "marker_code": "other", "marker_type": "sport"},
        {**payload, "marker_code": "bad code"},
        {**payload, "marker_code": "_"},
        {**payload, "marker_code": "other", "enabled": 1},
        {**payload, "marker_code": "other", "tab_id": 987654321},
    ):
        response = client.post("/api/event-tab-rules", json=invalid_payload)
        assert response.status_code == 400

    assert client.get("/api/event-tab-rules?pending=maybe").status_code == 400
    assert client.get("/api/event-tab-rules?marker_type=sport").status_code == 400
    assert client.post(
        "/api/event-tab-rules", data="[]", content_type="application/json"
    ).status_code == 400
