from __future__ import annotations

from copy import deepcopy

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once
from app.services.publisher import publish_ready_articles
from app.services.quality import LLMCallError, LLMService


TITLE_A = "巴西足球：安切洛蒂公布澳印26人名单"
TITLE_B = "安切洛蒂：毛罗-儒尼奥尔入选巴西名单"
BODY = "<p>巴西国家队名单公布，报道包含名单要点、主帅说明和球员反应。</p>"

CLEAN = {
    "pass": True,
    "needs_review": False,
    "score": 100,
    "level": "B",
    "issues": {
        "title_problems": [],
        "dirty_content": [],
        "completeness_problems": [],
        "channel_problems": [],
        "semantic_problems": [],
    },
    "reason": "内容正常",
    "title_fix_method": "unchanged",
}


def _item(suffix: str, title: str, channels: list[int]) -> dict:
    return {
        "translate_title": title,
        "archive_id": 0,
        "translate_body": BODY,
        "source": "marca",
        "source_url": f"https://example.com/dedup/{suffix}",
        "dqd_litpic": "/fastdfs8/dedup.jpg",
        "channels": channels,
    }


def _config(database: str, **overrides) -> AppConfig:
    base = {
        "database_path": database,
        "material_api_key": "test-key",
        "material_caller": "test-caller",
        "llm_api_key": "test-llm",
        "publisher_enabled": False,
        "scheduler_enabled": False,
    }
    base.update(overrides)
    return AppConfig(**base)


def _open_config(database: str) -> AppConfig:
    return _config(
        database,
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )


def _enable_direct_source(database: str) -> None:
    conn = _connect(database)
    try:
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=1)
        repo.update_source("marca", conn, tab_id=tab["id"], enabled=True)
    finally:
        conn.close()


def _publish_article(database: str, suffix: str, title: str, channels: list[int]) -> int:
    conn = _connect(database)
    try:
        article = repo.upsert_material(_item(suffix, title, channels), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        repo.transition_status(article["id"], "PUBLISHED", conn)
        return int(article["id"])
    finally:
        conn.close()


def _fetch(monkeypatch, items: list[dict]) -> None:
    monkeypatch.setattr(
        "app.services.pipeline.MaterialClient.fetch_all",
        lambda self, sources, **kwargs: MaterialFetchResult(
            items=items, total=len(items), pages=1
        ),
    )


def _clean_evaluate(monkeypatch) -> None:
    def fake_evaluate(**kwargs):
        quality = deepcopy(CLEAN)
        quality["title_before"] = kwargs.get("title", "")
        quality["title_after"] = kwargs.get("title", "")
        return quality

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)


def test_pipeline_blocks_later_duplicate_before_queue(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_direct_source(database)
    published_id = _publish_article(database, "a", TITLE_A, [100])
    _fetch(monkeypatch, [_item("b", TITLE_B, [100])])
    _clean_evaluate(monkeypatch)
    monkeypatch.setattr(
        LLMService,
        "chat_json",
        lambda self, prompt: {"duplicate": True, "matched_id": str(published_id), "reason": "同一次名单公布"},
    )

    result = run_once(_config(database))

    assert result["status_counts"] == {"TITLE_DUPLICATE": 1}
    conn = _connect(database)
    try:
        blocked = [a for a in repo.list_articles(conn) if int(a["id"]) != published_id][0]
        assert blocked["status"] == "TITLE_DUPLICATE"
        events = {e["event_type"]: e for e in repo.list_article_events(blocked["id"], conn)}
        event = events["TITLE_DUPLICATE_DETECTED"]
        assert "已取消自动发布" in event["message"]
        assert TITLE_A in event["message"]
        assert event["payload"]["matched"]["id"] == published_id
        assert event["payload"]["shared_channels"] == [100]
    finally:
        conn.close()


def test_pipeline_routes_to_review_when_dedup_llm_fails(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_direct_source(database)
    _publish_article(database, "a", TITLE_A, [100])
    _fetch(monkeypatch, [_item("b", TITLE_B, [100])])
    _clean_evaluate(monkeypatch)

    def failing(self, prompt):
        raise LLMCallError("timeout", category="timeout", retryable=True)

    monkeypatch.setattr(LLMService, "chat_json", failing)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    conn = _connect(database)
    try:
        article = [a for a in repo.list_articles(conn) if a["status"] == "NEEDS_REVIEW"][0]
        events = {e["event_type"]: e for e in repo.list_article_events(article["id"], conn)}
        assert "转人工审核" in events["TITLE_DUPLICATE_REVIEW"]["message"]
    finally:
        conn.close()


def test_pipeline_keeps_article_when_channels_disjoint(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_direct_source(database)
    _publish_article(database, "a", TITLE_A, [100])
    _fetch(monkeypatch, [_item("b", TITLE_B, [200])])
    _clean_evaluate(monkeypatch)
    calls = []

    def recording(self, prompt):
        calls.append(prompt)
        return {"duplicate": True, "matched_id": "1", "reason": "不应被调用"}

    monkeypatch.setattr(LLMService, "chat_json", recording)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert calls == []


def test_publisher_gate_blocks_duplicate_before_draft(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_direct_source(database)
    published_id = _publish_article(database, "a", TITLE_A, [100])
    conn = _connect(database)
    try:
        article = repo.upsert_material(_item("b", TITLE_B, [100]), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        article_id = int(article["id"])
    finally:
        conn.close()

    def no_draft(self, *args, **kwargs):
        raise AssertionError("查重拦截后不应调用开放平台")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", no_draft)
    monkeypatch.setattr(
        LLMService,
        "chat_json",
        lambda self, prompt: {"duplicate": True, "matched_id": str(published_id), "reason": "同一事件"},
    )

    conn = _connect(database)
    try:
        result = publish_ready_articles(_open_config(database), conn)
    finally:
        conn.close()

    assert result["title_duplicate_skipped"] == 1
    assert result["draft_created"] == 0
    conn = _connect(database)
    try:
        updated = repo.get_article(article_id, conn)
        assert updated["status"] == "TITLE_DUPLICATE"
        assert not updated["dqd_archive_id"]
    finally:
        conn.close()
