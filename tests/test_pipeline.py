from __future__ import annotations

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once


ITEM = {
    "translate_title": "客队在杯赛中完成逆转并顺利晋级",
    "archive_id": 0,
    "translate_body": "<p>客队在比赛下半场连进两球完成逆转，最终晋级下一轮，赛后双方主教练接受采访。</p>",
    "source": "marca",
    "source_url": "https://example.com/pipeline/1",
    "dqd_litpic": "/fastdfs8/pipeline.jpg",
    "channels": [100, 200],
}


def test_pipeline_ingests_deduplicates_and_queues(app, monkeypatch):
    database = app.config["DATABASE"]
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)

    def fake_fetch_all(self, sources, **kwargs):
        assert sources == ["marca"]
        return MaterialFetchResult(items=[ITEM], total=1, pages=1)

    monkeypatch.setattr("app.services.pipeline.MaterialClient.fetch_all", fake_fetch_all)
    config = AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        scheduler_enabled=False,
    )
    first = run_once(config)
    second = run_once(config)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["updated"] == 1
    conn = _connect(database)
    try:
        articles = repo.list_articles(conn)
        assert len(articles) == 1
        assert articles[0]["status"] == "READY_TO_PUBLISH"
        assert repo.count_articles(conn) == 1
    finally:
        conn.close()


def test_pipeline_records_missing_credentials(app):
    config = AppConfig(database_path=app.config["DATABASE"], scheduler_enabled=False)
    conn = _connect(app.config["DATABASE"])
    try:
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", conn, tab_id=tab["id"], enabled=True)
    finally:
        conn.close()
    result = run_once(config)
    assert result["errors"] == 1
    assert "SK/caller" in result["message"]


def test_pipeline_recovers_article_left_quality_checking(app, monkeypatch):
    database = app.config["DATABASE"]
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(ITEM)["article"]
        repo.transition_status(article["id"], "QUALITY_CHECKING")

    monkeypatch.setattr(
        "app.services.pipeline.MaterialClient.fetch_all",
        lambda self, sources, **kwargs: MaterialFetchResult(items=[ITEM], total=1, pages=1),
    )
    result = run_once(AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        scheduler_enabled=False,
    ))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    conn = _connect(database)
    try:
        assert repo.get_article(article["id"], conn)["status"] == "READY_TO_PUBLISH"
    finally:
        conn.close()


def test_quality_item_error_marks_run_partial(app, monkeypatch):
    database = app.config["DATABASE"]
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)

    monkeypatch.setattr(
        "app.services.pipeline.MaterialClient.fetch_all",
        lambda self, sources, **kwargs: MaterialFetchResult(items=[ITEM], total=1, pages=1),
    )
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("quality exploded")))
    result = run_once(AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        scheduler_enabled=False,
    ))
    assert result["errors"] == 1
    conn = _connect(database)
    try:
        assert repo.list_run_logs(conn, limit=1)[0]["status"] == "PARTIAL"
    finally:
        conn.close()
