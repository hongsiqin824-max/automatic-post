from __future__ import annotations

import pytest

from app.config import AppConfig
from app.services import title_dedup
from app.services.quality import LLMCallError, LLMService


SAME_FACT_PAIRS = (
    ("巴西足球：安切洛蒂公布澳印26人名单", "安切洛蒂：毛罗-儒尼奥尔入选巴西名单"),
    ("环球体育：安切洛蒂首期巴西名单，8新面孔", "环球体育：安切洛蒂首期巴西名单出炉，备战澳印热身赛"),
    ("巴西足球：安切洛蒂周三公布名单，克鲁塞罗5人入围初选", "环球体育：安切洛蒂周三公布巴西世界杯后首份名单"),
)


def _config(**overrides) -> AppConfig:
    base = {
        "database_path": ":memory:",
        "llm_api_key": "test-llm",
        "scheduler_enabled": False,
        "publisher_enabled": False,
    }
    base.update(overrides)
    return AppConfig(**base)


def _article(article_id: int, title: str, channels: list[int], status: str = "PUBLISHED") -> dict:
    return {
        "id": article_id,
        "title_final": title,
        "channels": channels,
        "status": status,
        "published_at": "2026-09-09T00:00:00Z",
        "dqd_archive_id": 6300000 + article_id,
    }


@pytest.mark.parametrize("left,right", SAME_FACT_PAIRS)
def test_user_examples_pass_the_recall_gate(left, right) -> None:
    score = title_dedup.score_title_similarity(left, right)
    assert title_dedup.is_recall_hit(score, dice_min=0.25, lcs_min=4)


def test_conflicting_numbers_veto_candidate() -> None:
    assert title_dedup.has_conflicting_numbers(
        "西甲第3轮：皇马2-0击败塞维利亚", "西甲第4轮：皇马3-1击败塞维利亚"
    )
    assert not title_dedup.has_conflicting_numbers(
        "西甲第3轮：皇马2-0击败塞维利亚", "西甲皇马取胜后主帅出席发布会"
    )


def test_select_candidates_requires_shared_channel_and_older_id() -> None:
    articles = [
        _article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100]),
        _article(2, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [200]),
        _article(9, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100]),
    ]
    selected = title_dedup.select_candidates(
        "巴西足球：安切洛蒂公布澳印26人名单",
        articles,
        candidate_id=5,
        channels=[100],
        dice_min=0.25,
        lcs_min=4,
        limit=3,
    )
    assert [item["article"]["id"] for item in selected] == [1]
    assert selected[0]["shared_channels"] == [100]


def test_check_skips_without_llm_or_for_draft_mode() -> None:
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(llm_api_key=""), article, [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])]
    )
    assert result["outcome"] == "skipped"

    result = title_dedup.check_title_duplicate(
        _config(), article, [], publish_mode=0
    )
    assert result["outcome"] == "skipped"
    assert "非直接发布" in result["skip_reason"]


def test_check_reports_duplicate_from_llm_decision(monkeypatch) -> None:
    calls = []

    def fake_chat_json(self, prompt):
        calls.append(prompt)
        return {"duplicate": True, "matched_id": "1", "reason": "同一次巴西名单公布"}

    monkeypatch.setattr(LLMService, "chat_json", fake_chat_json)
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(),
        article,
        [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])],
        publish_mode=1,
    )
    assert result["outcome"] == "duplicate"
    assert result["matched"]["id"] == 1
    assert result["shared_channels"] == [100]
    assert "安切洛蒂" in calls[0]


def test_check_exact_normalized_title_needs_no_llm(monkeypatch) -> None:
    def unexpected(self, prompt):
        raise AssertionError("归一化完全一致不应调用 LLM")

    monkeypatch.setattr(LLMService, "chat_json", unexpected)
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单！", [100])
    result = title_dedup.check_title_duplicate(
        _config(),
        article,
        [_article(1, "巴西足球:安切洛蒂公布澳印26人名单", [100])],
        publish_mode=1,
    )
    assert result["outcome"] == "duplicate"
    assert result["reason"] == "归一化标题完全一致"


def test_check_routes_to_review_when_llm_fails(monkeypatch) -> None:
    def failing(self, prompt):
        raise LLMCallError("timeout", category="timeout", retryable=True)

    monkeypatch.setattr(LLMService, "chat_json", failing)
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(),
        article,
        [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])],
        publish_mode=1,
    )
    assert result["outcome"] == "needs_review"
    assert result["error"]


def test_check_degrades_to_fallback_model_on_transport_error(monkeypatch) -> None:
    calls = []

    def fake_chat_json(self, prompt):
        calls.append(self.model)
        if self.model == "primary":
            raise LLMCallError("timeout", category="timeout", retryable=True)
        return {"duplicate": True, "matched_id": "1", "reason": "降级模型判定同一事件"}

    monkeypatch.setattr(LLMService, "chat_json", fake_chat_json)
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(llm_model="primary", llm_api_key2="test-llm2", llm_model2="deepseek"),
        article,
        [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])],
        publish_mode=1,
    )
    assert calls == ["primary", "deepseek"]
    assert result["outcome"] == "duplicate"
    assert result["matched"]["id"] == 1
    assert result["fallback_used"] is True
    assert result["primary_error"]["category"] == "timeout"
    assert result["error"] is None


def test_check_routes_to_review_when_fallback_also_fails(monkeypatch) -> None:
    calls = []

    def failing(self, prompt):
        calls.append(self.model)
        raise LLMCallError("timeout", category="timeout", retryable=True)

    monkeypatch.setattr(LLMService, "chat_json", failing)
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(llm_model="primary", llm_api_key2="test-llm2", llm_model2="deepseek"),
        article,
        [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])],
        publish_mode=1,
    )
    assert calls == ["primary", "deepseek"]
    assert result["outcome"] == "needs_review"
    assert result["primary_error"]["category"] == "timeout"
    assert "降级模型也失败" in result["error"]


def test_check_does_not_degrade_on_invalid_response(monkeypatch) -> None:
    calls = []

    def failing(self, prompt):
        calls.append(self.model)
        raise LLMCallError("AI 返回不是合法 JSON", category="invalid_response", retryable=False)

    monkeypatch.setattr(LLMService, "chat_json", failing)
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(llm_model="primary", llm_api_key2="test-llm2", llm_model2="deepseek"),
        article,
        [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])],
        publish_mode=1,
    )
    assert calls == ["primary"]
    assert result["outcome"] == "needs_review"
    assert result["fallback_used"] is False


def test_check_routes_to_review_when_matched_id_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        LLMService,
        "chat_json",
        lambda self, prompt: {"duplicate": True, "matched_id": "999", "reason": "x"},
    )
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(),
        article,
        [_article(1, "安切洛蒂：毛罗-儒尼奥尔入选巴西名单", [100])],
        publish_mode=1,
    )
    assert result["outcome"] == "needs_review"


def test_check_not_duplicate_when_llm_says_different_event(monkeypatch) -> None:
    monkeypatch.setattr(
        LLMService,
        "chat_json",
        lambda self, prompt: {"duplicate": False, "matched_id": None, "reason": "初选与最终名单不同"},
    )
    article = _article(5, "巴西足球：安切洛蒂公布澳印26人名单", [100])
    result = title_dedup.check_title_duplicate(
        _config(),
        article,
        [_article(1, "巴西足球：安切洛蒂周三公布名单，克鲁塞罗5人入围初选", [100])],
        publish_mode=1,
    )
    assert result["outcome"] == "not_duplicate"
