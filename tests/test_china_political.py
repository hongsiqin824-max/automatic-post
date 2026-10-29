"""涉华政治轮：所有稿子进质检先单独判一次，命中直接放弃，其余流程不变。"""
from __future__ import annotations

import pytest

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once
from app.services.quality import LLMCallError, check_china_political

TITLE = "韩媒：姆巴佩称毛泽东独裁者后道歉"
BODY = (
    "<p>姆巴佩因调侃毛泽东引发争议，中国外交部发言人在例行记者会上批评其言论，"
    "多家中国企业随后宣布终止合作，央视暂停转播西甲。</p>"
)


class _FakeLLM:
    configured = True
    model = "fake"

    def __init__(self, answer=None, error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.prompts: list[str] = []

    def chat_json(self, prompt: str):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.answer


def test_check_china_political_prompt_and_verdict():
    llm = _FakeLLM({"china_political": True, "reason": "领导人评价争议"})
    verdict = check_china_political(TITLE, BODY, llm)
    assert verdict == {"china_political": True, "reason": "领导人评价争议"}
    prompt = llm.prompts[0]
    # 国足/足协负面是正常体育新闻，必须在提示词里明确排除；只判政治这一件事。
    assert "中国足协的人事和管理" in prompt
    assert "只判断一件事" in prompt
    assert "毛泽东" in prompt


def test_check_china_political_ignores_non_strict_flag():
    verdict = check_china_political(TITLE, BODY, _FakeLLM({"china_political": "true"}))
    assert verdict["china_political"] is False


def test_check_china_political_uses_fallback_model():
    primary = _FakeLLM(error=LLMCallError("timeout", category="timeout", retryable=True))
    fallback = _FakeLLM({"china_political": True, "reason": "外交部表态"})
    assert check_china_political(TITLE, BODY, primary, fallback)["china_political"] is True


def test_check_china_political_raises_without_answer():
    primary = _FakeLLM(error=LLMCallError("timeout", category="timeout", retryable=True))
    with pytest.raises(LLMCallError):
        check_china_political(TITLE, BODY, primary)


def _run(app, monkeypatch, verdict_fn):
    database = app.config["DATABASE"]
    conn = _connect(database)
    try:
        tab = repo.list_tabs(conn)[0]
        repo.update_source("naversp", conn, tab_id=tab["id"], enabled=True)
    finally:
        conn.close()
    item = {
        "translate_title": TITLE,
        "archive_id": 0,
        "translate_body": BODY,
        "source": "naversp",
        "source_url": "https://example.com/naversp/mbappe",
        "channels": [100],
    }
    monkeypatch.setattr(
        "app.services.pipeline.MaterialClient.fetch_all",
        lambda self, sources, **kwargs: MaterialFetchResult(items=[item], total=1, pages=1),
    )
    monkeypatch.setattr("app.services.pipeline.check_china_political", verdict_fn)
    evaluate_calls: list[dict] = []

    def fake_evaluate(**kwargs):
        evaluate_calls.append(kwargs)
        return {
            "pass": False,
            "needs_review": True,
            "score": 50,
            "level": "B",
            "issues": {"semantic_problems": ["需人工确认"]},
            "reason": "需人工确认",
            "title_before": TITLE,
            "title_after": TITLE,
            "title_fix_method": "unchanged",
        }

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    run_once(AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        llm_api_key="test-llm-key",
        publisher_enabled=False,
        scheduler_enabled=False,
    ))
    conn = _connect(database)
    try:
        article = next(
            row for row in repo.list_articles(conn, limit=50)
            if row["source_url"] == item["source_url"]
        )
        events = [event["event_type"] for event in repo.list_article_events(article["id"], conn)]
    finally:
        conn.close()
    return article, events, evaluate_calls


def test_china_political_article_abandoned_before_normal_quality(app, monkeypatch):
    article, events, evaluate_calls = _run(
        app, monkeypatch,
        lambda *args, **kwargs: {"china_political": True, "reason": "领导人评价争议"},
    )
    assert article["status"] == "ABANDONED"
    assert "ARTICLE_ABANDONED" in events
    # 命中后原质检一次都不跑。
    assert evaluate_calls == []


def test_non_political_article_continues_normal_quality(app, monkeypatch):
    article, events, evaluate_calls = _run(
        app, monkeypatch,
        lambda *args, **kwargs: {"china_political": False, "reason": ""},
    )
    assert len(evaluate_calls) == 1
    assert article["status"] == "NEEDS_REVIEW"
    assert "ARTICLE_ABANDONED" not in events


def test_china_political_check_failure_falls_through(app, monkeypatch):
    def _boom(*args, **kwargs):
        raise LLMCallError("timeout", category="timeout", retryable=True)

    article, events, evaluate_calls = _run(app, monkeypatch, _boom)
    assert len(evaluate_calls) == 1
    assert article["status"] == "NEEDS_REVIEW"
    assert "CHINA_POLITICAL_CHECK_FAILED" in events
