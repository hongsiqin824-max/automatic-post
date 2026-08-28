from __future__ import annotations

from app import repository as repo


def _material(url_suffix: str, marker: str) -> dict:
    return {
        "source": "marca",
        "source_url": f"https://example.com/source-rule/{url_suffix}",
        "translate_title": "来源赛事规则测试文章",
        "translate_body": "<p>这是一篇用于验证来源赛事栏目规则的中文正文。</p>",
        "channels": [11, 22],
        "user_name": f"marca:fb.{marker}",
    }


def test_source_and_global_rules_follow_team_before_league_priority(app):
    with app.app_context():
        generic = repo.get_tab_by_backend_id(58) or repo.create_tab("规则精选", 58)
        original = repo.create_tab("规则原栏目", 99001)
        global_league_tab = repo.create_tab("全局联赛栏目", 99002)
        source_league_tab = repo.create_tab("来源联赛栏目", 99003)
        global_team_tab = repo.create_tab("全局球队栏目", 99004)
        source_team_tab = repo.create_tab("来源球队栏目", 99005)
        repo.update_source(
            "marca", tab_ids=[generic["id"], original["id"]], enabled=True
        )

        global_league = repo.create_event_tab_rule(
            "league", "scopeleague", global_league_tab["id"]
        )
        source_league = repo.create_event_tab_rule(
            "league", "scopeleague", source_league_tab["id"],
            source_code="marca",
        )
        global_team = repo.create_event_tab_rule(
            "team", "scopeteam", global_team_tab["id"]
        )
        source_team = repo.create_event_tab_rule(
            "team", "scopeteam", source_team_tab["id"],
            source_code="marca",
        )

        first = repo.upsert_material(
            _material("source-team", "scopeleague.scopeteam")
        )["article"]
        assert first["backend_tab_ids"] == [58, 99005]
        assert first["route_rule_id"] == source_team["id"]

        repo.update_event_tab_rule(source_team["id"], enabled=False)
        second = repo.upsert_material(
            _material("global-team", "scopeleague.scopeteam")
        )["article"]
        assert second["backend_tab_ids"] == [58, 99004]
        assert second["route_rule_id"] == global_team["id"]

        repo.update_event_tab_rule(global_team["id"], enabled=False)
        third = repo.upsert_material(
            _material("source-league", "scopeleague.scopeteam")
        )["article"]
        assert third["backend_tab_ids"] == [58, 99003]
        assert third["route_rule_id"] == source_league["id"]

        repo.update_event_tab_rule(source_league["id"], enabled=False)
        fourth = repo.upsert_material(
            _material("global-league", "scopeleague.scopeteam")
        )["article"]
        assert fourth["backend_tab_ids"] == [58, 99002]
        assert fourth["route_rule_id"] == global_league["id"]


def test_rule_publish_action_is_live_until_first_submit_then_snapshotted(app):
    with app.app_context():
        original = repo.create_tab("动作原栏目", 99101, publish_mode=1)
        target = repo.create_tab("动作目标栏目", 99102, publish_mode=1)
        repo.update_source(
            "marca", tab_ids=[original["id"]], enabled=True,
            publish_mode_override=0,
        )
        rule = repo.create_event_tab_rule(
            "league", "actionleague", target["id"],
            source_code="marca", publish_mode_override=1,
        )
        article = repo.upsert_material(
            _material("action-snapshot", "actionleague._")
        )["article"]

        before = repo.resolve_article_publish_mode(article["id"])
        assert before["snapshot"] is None
        assert before["publish_mode"] == 1
        assert before["route_rule_publish_mode_override"] == 1

        repo.update_event_tab_rule(rule["id"], publish_mode_override=0)
        configured = repo.resolve_article_publish_mode(article["id"])
        assert configured["publish_mode"] == 0

        frozen = repo.ensure_article_publish_mode(article["id"])
        assert frozen["publish_mode"] == 0
        decided_at = frozen["publish_mode_decided_at"]

        repo.update_event_tab_rule(rule["id"], publish_mode_override=1)
        repo.update_source("marca", publish_mode_override=1)
        repo.update_tab(target["id"], publish_mode=0)

        retry = repo.ensure_article_publish_mode(article["id"])
        resolution = repo.resolve_article_publish_mode(article["id"])
        assert retry["publish_mode"] == 0
        assert retry["publish_mode_decided_at"] == decided_at
        assert resolution["snapshot"] == 0
        assert resolution["publish_mode"] == 0


def test_follow_action_defers_to_source_then_tabs(app):
    with app.app_context():
        original = repo.create_tab("跟随原栏目", 99201, publish_mode=0)
        target = repo.create_tab("跟随目标栏目", 99202, publish_mode=1)
        repo.update_source(
            "marca", tab_ids=[original["id"]], enabled=True,
            publish_mode_override=0,
        )
        rule = repo.create_event_tab_rule(
            "league", "followleague", target["id"],
            source_code="marca", publish_mode_override=None,
        )
        article = repo.upsert_material(
            _material("follow-source", "followleague._")
        )["article"]

        resolution = repo.resolve_article_publish_mode(article["id"])
        assert resolution["route_rule_id"] == rule["id"]
        assert resolution["route_rule_publish_mode_override"] is None
        assert resolution["publish_mode"] == 0

        repo.update_source("marca", publish_mode_override=None)
        assert repo.resolve_article_publish_mode(article["id"])["publish_mode"] == 1


def test_source_rule_changes_do_not_reroute_existing_articles(app):
    with app.app_context():
        original = repo.create_tab("历史原栏目", 99301)
        first_target = repo.create_tab("历史第一栏目", 99302)
        second_target = repo.create_tab("历史第二栏目", 99303)
        repo.update_source("marca", tab_ids=[original["id"]], enabled=True)
        rule = repo.create_event_tab_rule(
            "league", "historyleague", first_target["id"],
            source_code="marca",
        )
        first = repo.upsert_material(
            _material("history-first", "historyleague._")
        )["article"]

        repo.update_event_tab_rule(rule["id"], tab_id=second_target["id"])
        repeated = repo.upsert_material(
            _material("history-first", "historyleague._")
        )["article"]
        second = repo.upsert_material(
            _material("history-second", "historyleague._")
        )["article"]

        assert repeated["id"] == first["id"]
        assert repeated["backend_tab_ids"] == [99302]
        assert second["backend_tab_ids"] == [99303]
