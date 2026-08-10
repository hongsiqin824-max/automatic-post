from __future__ import annotations

from datetime import datetime, timedelta, timezone
from app import repository as repo
from app.config import AppConfig
from app.db import get_db
from app.services.dqd_open_client import (
    DqdOpenDraftResult,
    build_create_article_form,
)
from app.services.publisher import create_draft_for_article, publish_ready_articles


def _ready_article():
    return {
        "source": "marca",
        "source_url": "https://example.com/publisher",
        "translate_title": "马卡：球队确认新赛季重要安排",
        "translate_body": "<p>球队确认了新赛季的重要安排，训练计划、热身赛和球迷活动都已经公布。</p>",
        "archive_id": 0,
        "dqd_litpic": "/fastdfs8/publisher.jpg",
        "channels": [11, 22],
    }


def _open_config(database: str) -> AppConfig:
    return AppConfig(
        database_path=database,
        scheduler_enabled=False,
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
        dqd_open_archive_level="B",
        dqd_open_status=0,
    )


def test_create_article_form_uses_backend_tab_and_draft_status(app):
    config = _open_config(":memory:")
    article = {
        "title_final": "完整标题",
        "body_html": "<p>完整正文，信息充分。</p>",
        "litpic": "/fastdfs8/cover.jpg",
        "channels": [11, 22],
    }
    form = build_create_article_form(article, {"backend_tab_id": 284}, config)
    assert ("dqd_enname", "hongsiqin") in form
    assert ("archive_level", "B") in form
    assert ("status", "0") in form
    assert ("tabs[]", "284") in form
    assert ("channels", "11,22") in form


def test_publish_ready_article_creates_draft_and_saves_archive_id(app, monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab):
            assert article["status"] == "READY_TO_PUBLISH"
            assert tab["backend_tab_id"]
            return DqdOpenDraftResult(
                archive_id=3801234,
                payload={"code": 0, "data": {"archive_id": 3801234}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("title", article["title_final"]), ("tabs[]", str(tab["backend_tab_id"]))],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())
        updated = repo.get_article(article["id"])

    assert result["draft_created"] == 1
    assert result["failed"] == 0
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3801234


def test_publish_retry_reuses_existing_archive_id_without_remote_call(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3802222)
        repo.transition_status(article["id"], "PUBLISH_FAILED")

        result = create_draft_for_article(_open_config(app.config["DATABASE"]), get_db(), article["id"])
        updated = repo.get_article(article["id"])

    assert result["reused_existing_archive"] is True
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3802222


def test_publish_ready_articles_recovers_stale_publishing_with_archive_id(app, monkeypatch):
    class ForbiddenClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab):  # pragma: no cover - must not be called
            raise AssertionError("stale publishing recovery should not call remote create")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", ForbiddenClient)
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3803333)
        repo.transition_status(article["id"], "PUBLISHING")
        conn = get_db()
        with conn:
            conn.execute("UPDATE articles SET updated_at=? WHERE id=?", (stale_time, article["id"]))

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())
        updated = repo.get_article(article["id"])

    assert result["recovered"] == 1
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3803333
