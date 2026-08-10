from __future__ import annotations

from dataclasses import replace

from app import repository as repo
from app.db import get_db
from app.services.dqd_open_client import DqdOpenDraftResult
from app.web import format_beijing_time


def test_format_beijing_time_converts_utc_and_handles_invalid_values():
    assert format_beijing_time("2026-08-10T02:18:06.681Z") == "2026-08-10 10:18:06"
    assert format_beijing_time("2026-08-10T02:18:06+00:00") == "2026-08-10 10:18:06"
    assert format_beijing_time("") == ""
    assert format_beijing_time("not-a-timestamp") == "not-a-timestamp"


def test_core_pages_render(client):
    for path in ("/", "/articles", "/review", "/config", "/health"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_dashboard_renders_beijing_time(app, client):
    with app.app_context():
        repo.set_setting("last_run_at", "2026-08-10T02:18:06.681Z", get_db())

    html = client.get("/").get_data(as_text=True)
    assert "2026-08-10 10:18:06" in html
    assert "2026-08-10T02:18:06.681Z" not in html


def test_source_and_tab_configuration_api(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
    response = client.post(f"/api/sources/marca", json={"tab_id": tab["id"], "enabled": True})
    assert response.status_code == 200
    assert response.get_json()["source"]["enabled"] == 1


def test_source_update_without_tab_field_preserves_mapping(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
    response = client.post("/api/sources/marca", json={"enabled": False})
    assert response.status_code == 200
    assert response.get_json()["source"]["tab_id"] == tab["id"]


def test_review_api_moves_article_to_ready_queue(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-review",
            "translate_title": "需要人工处理的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证人工审核接口是否可以把文章放入待发队列。</p>",
            "channels": [],
        })["article"]
        repo.transition_status(article["id"], "NEEDS_REVIEW")
    response = client.post(f"/api/articles/{article['id']}/review", json={"action": "pass"})
    assert response.status_code == 200
    assert response.get_json()["article"]["status"] == "READY_TO_PUBLISH"


def test_create_draft_retry_api_records_attempt_and_archive_id(app, client, monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab):
            assert article["status"] == "PUBLISH_FAILED"
            assert tab["backend_tab_id"]
            return DqdOpenDraftResult(
                archive_id=3802222,
                payload={"code": 0, "data": {"archive_id": 3802222}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("title", article["title_final"]), ("tabs[]", str(tab["backend_tab_id"]))],
            )

    cfg = replace(
        app.extensions["app_config"],
        database_path=app.config["DATABASE"],
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    app.extensions["app_config"] = cfg
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-retry",
            "translate_title": "可重试创建草稿的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证单篇草稿重试接口是否可以写入时间线。</p>",
            "dqd_litpic": "/fastdfs8/web-draft.jpg",
            "channels": [11],
        })["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")
        repo.transition_status(article["id"], "PUBLISH_FAILED")

    response = client.post(f"/api/articles/{article['id']}/create-draft")
    assert response.status_code == 200
    body = response.get_json()
    assert body["article"]["status"] == "DRAFT_CREATED"
    assert body["result"]["archive_id"] == 3802222
    assert body["result"]["draft_url"] == "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"
    assert body["article"]["draft_url"] == "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"

    with app.app_context():
        events = repo.list_article_events(article["id"], get_db())
    assert events[-1]["event_type"] == "DRAFT_RETRY_SUCCEEDED"
    assert "draft_url" in events[-1]["payload_json"]


def test_article_detail_renders_clickable_draft_link(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-link",
            "translate_title": "可点击草稿链接的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证详情页草稿链接展示。</p>",
            "dqd_litpic": "/fastdfs8/web-draft-link.jpg",
            "channels": [11],
        })["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3802222)

    response = client.get(f"/articles/{article['id']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'href="https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"' in html
    assert ">3802222<" in html


def test_article_detail_shows_recovered_draft_hint_when_failed_but_archive_exists(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-hint",
            "translate_title": "失败但已有草稿链接的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证失败态下的草稿提示。</p>",
            "dqd_litpic": "/fastdfs8/web-draft-hint.jpg",
            "channels": [11],
        })["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3802444)
        repo.transition_status(article["id"], "PUBLISH_FAILED")

    response = client.get(f"/articles/{article['id']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "这条文章其实已经创建过懂球帝草稿" in html
    assert 'href="https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802444"' in html


def test_run_endpoint_is_non_blocking(client):
    response = client.post("/api/run", json={})
    assert response.status_code == 200
    assert response.get_json()["success"] is True
