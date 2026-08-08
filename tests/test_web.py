from __future__ import annotations

from app import repository as repo


def test_core_pages_render(client):
    for path in ("/", "/articles", "/review", "/config", "/health"):
        response = client.get(path)
        assert response.status_code == 200, path


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


def test_run_endpoint_is_non_blocking(client):
    response = client.post("/api/run", json={})
    assert response.status_code == 200
    assert response.get_json()["success"] is True
