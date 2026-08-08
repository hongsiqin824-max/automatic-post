from __future__ import annotations

import sqlite3

import pytest

from app import repository as repo
from app.db import get_db


def _material(**overrides):
    value = {
        "source": "marca",
        "source_url": "https://example.com/article/1",
        "translate_title": "马卡：主队在联赛中取得关键胜利",
        "translate_body": "<p>这是一篇完整的体育新闻正文，包含足够的信息用于自动质量检测和后续审核流程。</p>",
        "archive_id": 0,
        "dqd_litpic": "/fastdfs8/example.jpg",
        "channels": [11, 22, 11],
    }
    value.update(overrides)
    return value


def test_catalog_is_seeded_and_disabled_by_default(app):
    with app.app_context():
        assert len(repo.list_tabs()) == 14
        sources = repo.list_sources()
        assert len(sources) == 85
        assert not any(row["enabled"] for row in sources)


def test_source_must_have_tab_before_enable(app):
    with app.app_context():
        with pytest.raises(ValueError, match="assigned to a tab"):
            repo.update_source("marca", enabled=True)
        tab = repo.list_tabs(include_disabled=False)[0]
        source = repo.update_source("marca", tab_id=tab["id"], enabled=True)
        assert source["enabled"] == 1
        assert source["tab_id"] == tab["id"]


def test_material_unique_key_and_external_ids_are_separate(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        first = repo.upsert_material(_material())
        second = repo.upsert_material(_material(archive_id=77))
        assert first["created"] is True
        assert second["created"] is False
        assert first["article"]["id"] == second["article"]["id"]
        assert second["article"]["upstream_archive_id"] == 77
        assert second["article"]["dqd_archive_id"] is None
        assert repo.count_articles() == 1
        assert second["article"]["channels"] == [11, 22]


def test_disabling_referenced_tab_is_rejected(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        with pytest.raises(ValueError, match="referenced"):
            repo.disable_tab(tab["id"])


def test_manual_review_updates_content_and_status(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_material())["article"]
        repo.transition_status(article["id"], "NEEDS_REVIEW")
        updated = repo.manual_review_update(
            article["id"],
            "manual_fix_then_pass",
            title="修正后的完整标题",
            body_html="<p>修正后的完整正文，信息充分且没有广告内容，可以进入后续待发队列。</p>",
            note="人工已核对",
        )
        assert updated["status"] == "READY_TO_PUBLISH"
        assert updated["title_final"] == "修正后的完整标题"
        assert updated["quality"]["pass"] is True
        assert updated["quality"]["needs_review"] is False
        assert len(repo.list_article_events(article["id"])) >= 3


def test_manual_review_only_accepts_articles_waiting_for_review(app):
    with app.app_context():
        article = repo.upsert_material(_material())['article']
        with pytest.raises(ValueError, match="awaiting manual review"):
            repo.manual_review_update(article["id"], "pass")


def test_catalog_seed_survives_operator_tab_edits_and_restart(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_tab(tab["id"], name="自定义栏目名称", backend_tab_id=900001)
    restarted = __import__("app.web", fromlist=["create_app"]).create_app({
        "TESTING": True,
        "DATABASE": app.config["DATABASE"],
    })
    with restarted.app_context():
        edited = repo.get_tab(tab["id"])
        assert edited["name"] == "自定义栏目名称"
        assert edited["backend_tab_id"] == 900001
