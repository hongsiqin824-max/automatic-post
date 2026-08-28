from __future__ import annotations

from app import repository as repo
from app.db import get_db


def test_config_page_renders_event_tab_rule_controls(app, client):
    with app.app_context():
        tab = repo.list_tabs(get_db(), include_disabled=False)[0]
        rule = repo.create_event_tab_rule(
            "league", "webtestleague", tab["id"], True, get_db(),
            source_code="marca", publish_mode_override=1,
        )

    html = client.get("/config").get_data(as_text=True)

    assert "赛事栏目规则" in html
    assert 'data-competition-rule-search' in html
    assert 'data-pending-rule-filter' in html
    assert f'data-id="{rule["id"]}"' in html
    assert "webtestleague" in html
    assert tab["name"] in html
    assert 'name="source_code"' in html
    assert 'name="publish_mode_override"' in html
    assert 'data-source-code="marca"' in html
    assert 'data-publish-mode-override="1"' in html
    assert "跟随原配置" in html
    assert "创建草稿" in html
    assert "直接发布" in html


def test_event_tab_rule_api_create_update_clear_and_filter(app, client):
    with app.app_context():
        tab = repo.list_tabs(get_db(), include_disabled=False)[0]

    created = client.post(
        "/api/event-tab-rules",
        json={
            "marker_type": "league",
            "marker_code": "webapileague",
            "tab_id": tab["id"],
            "source_code": "marca",
            "publish_mode_override": 1,
            "enabled": True,
        },
    )
    assert created.status_code == 200
    rule = created.get_json()["event_tab_rule"]
    assert rule["tab_id"] == tab["id"]
    assert rule["configured"] is True
    assert rule["source_code"] == "marca"
    assert rule["publish_mode_override"] == 1

    scoped_list = client.get("/api/event-tab-rules?source_code=marca")
    assert scoped_list.status_code == 200
    assert rule["id"] in {
        item["id"] for item in scoped_list.get_json()["event_tab_rules"]
    }

    disabled = client.post(
        f'/api/event-tab-rules/{rule["id"]}', json={"enabled": False}
    )
    assert disabled.status_code == 200
    assert disabled.get_json()["event_tab_rule"]["enabled"] == 0

    follow_original = client.post(
        f'/api/event-tab-rules/{rule["id"]}',
        json={"source_code": None, "publish_mode_override": None},
    )
    assert follow_original.status_code == 200
    global_rule = follow_original.get_json()["event_tab_rule"]
    assert global_rule["source_code"] is None
    assert global_rule["publish_mode_override"] is None

    cleared = client.post(
        f'/api/event-tab-rules/{rule["id"]}', json={"tab_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["event_tab_rule"]["tab_id"] is None

    pending = client.get("/api/event-tab-rules?pending=true&search=webapi")
    assert pending.status_code == 200
    pending_rules = pending.get_json()["event_tab_rules"]
    assert [item["id"] for item in pending_rules] == [rule["id"]]


def test_event_tab_rule_api_rejects_invalid_and_duplicate_payload(client):
    invalid = client.post(
        "/api/event-tab-rules",
        json={"marker_type": "league", "marker_code": "bad code", "enabled": True},
    )
    assert invalid.status_code == 400
    assert "marker_code" in invalid.get_json()["error"]

    payload = {"marker_type": "team", "marker_code": "duplicate_team"}
    assert client.post("/api/event-tab-rules", json=payload).status_code == 200
    duplicate = client.post("/api/event-tab-rules", json=payload)
    assert duplicate.status_code == 400
    assert "已存在" in duplicate.get_json()["error"]

    non_boolean = client.post(
        "/api/event-tab-rules",
        json={"marker_type": "league", "marker_code": "invalid_enabled", "enabled": 1},
    )
    assert non_boolean.status_code == 400
    assert "JSON boolean" in non_boolean.get_json()["error"]

    invalid_source = client.post(
        "/api/event-tab-rules",
        json={"marker_type": "league", "marker_code": "invalid_source", "source_code": 1},
    )
    assert invalid_source.status_code == 400
    assert "source_code" in invalid_source.get_json()["error"]

    invalid_publish_mode = client.post(
        "/api/event-tab-rules",
        json={
            "marker_type": "league",
            "marker_code": "invalid_publish_mode",
            "publish_mode_override": True,
        },
    )
    assert invalid_publish_mode.status_code == 400
    assert "publish_mode_override" in invalid_publish_mode.get_json()["error"]


def test_event_tab_rule_allows_global_and_source_scoped_variants(client):
    global_rule = {
        "marker_type": "league",
        "marker_code": "shared_scope_marker",
        "source_code": None,
        "publish_mode_override": None,
    }
    scoped_rule = {
        **global_rule,
        "source_code": "marca",
        "publish_mode_override": 0,
    }

    assert client.post("/api/event-tab-rules", json=global_rule).status_code == 200
    scoped = client.post("/api/event-tab-rules", json=scoped_rule)

    assert scoped.status_code == 200
    saved = scoped.get_json()["event_tab_rule"]
    assert saved["source_code"] == "marca"
    assert saved["publish_mode_override"] == 0
