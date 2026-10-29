"""涉华政治稿：质检模型判 china_political=true 时直接放弃。"""
from __future__ import annotations

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once
from app.services.quality import evaluate

TITLE = "韩媒：姆巴佩称毛泽东独裁者后道歉"
BODY = (
    "<p>姆巴佩因调侃毛泽东引发争议，中国外交部发言人在例行记者会上批评其言论，"
    "多家中国企业随后宣布终止合作，央视暂停转播西甲。</p>"
)


class _FakeLLM:
    configured = True
    model = "fake"
    timeout = 1
    last_error = None

    def __init__(self, answer: dict):
        self.answer = answer
        self.prompts: list[str] = []

    def chat_json(self, prompt: str):
        self.prompts.append(prompt)
        return dict(self.answer)


def _semantic(**overrides) -> dict:
    return {
        "title_complete": True,
        "body_complete": True,
        "has_ad_or_dirty": False,
        "repairable": False,
        "needs_review": False,
        "reason": "内容正常",
        "china_political": False,
        "china_political_reason": "",
        "dirty_targets": [],
        "repair_plans": [],
        **overrides,
    }


def test_semantic_prompt_carries_china_political_rule():
    llm = _FakeLLM(_semantic())
    evaluate(title=TITLE, body=BODY, channels=[1], llm=llm)
    prompt = llm.prompts[0]
    assert "china_political" in prompt
    # 国足/足协负面是正常体育新闻，必须在提示词里明确排除。
    assert "中国足协的人事和管理" in prompt


def test_evaluate_abandons_china_political_article():
    llm = _FakeLLM(_semantic(china_political=True, china_political_reason="涉及领导人评价争议"))
    result = evaluate(title=TITLE, body=BODY, channels=[1], llm=llm)
    assert result["abandon"] is True
    assert result["china_political"] is True
    assert result["pass"] is False
    assert "涉及领导人评价争议" in result["reason"]
    assert result["repair_plans"] == []


def test_evaluate_ignores_non_strict_china_political_flag():
    # 字段写成字符串等非布尔值时按未命中处理，不误放弃。
    llm = _FakeLLM(_semantic(china_political="true"))
    result = evaluate(title=TITLE, body=BODY, channels=[1], llm=llm)
    assert result.get("abandon") is not True
    assert result["pass"] is True


def test_foreign_national_team_rule_only_for_home_sources():
    llm = _FakeLLM(_semantic())
    evaluate(title=TITLE, body=BODY, channels=[1], llm=llm)
    evaluate(title=TITLE, body=BODY, channels=[1], llm=llm, home_country="日本")
    assert "foreign_national_team" not in llm.prompts[0]
    assert "foreign_national_team" in llm.prompts[1]
    assert "本文来自日本媒体" in llm.prompts[1]


def test_evaluate_abandons_foreign_national_team_article_from_home_source():
    llm = _FakeLLM(_semantic(foreign_national_team=True, foreign_national_team_reason="阿根廷国家队名单"))
    result = evaluate(
        title="日媒：阿根廷公布国家队名单", body=BODY, channels=[1], llm=llm, home_country="日本"
    )
    assert result["abandon"] is True
    assert result["abandon_code"] == "foreign_national_team"
    assert "日本媒体转述的外国国家队新闻" in result["reason"]


def test_foreign_national_team_flag_ignored_without_home_country():
    # 非日韩来源即使模型多输出了这个字段，也不放弃。
    llm = _FakeLLM(_semantic(foreign_national_team=True))
    result = evaluate(title=TITLE, body=BODY, channels=[1], llm=llm)
    assert result.get("abandon") is not True


def test_pipeline_marks_china_political_article_abandoned(app, monkeypatch):
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
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return {
            "pass": False,
            "needs_review": False,
            "abandon": True,
            "abandon_code": "china_political",
            "china_political": True,
            "reason": "涉华政治内容，直接放弃：领导人评价争议",
            "issues": {"china_political": ["涉华政治内容"]},
            "semantic_check": _semantic(china_political=True),
            "title_before": TITLE,
            "title_after": TITLE,
            "title_fix_method": "unchanged",
        }

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    run_once(AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        llm_api_key="",
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
    assert len(calls) == 1
    # 日韩来源进质检第一轮就带上本国，外国国家队稿在这里直接过滤。
    assert calls[0]["home_country"] == "韩国"
    assert article["status"] == "ABANDONED"
    assert "ARTICLE_ABANDONED" in events
