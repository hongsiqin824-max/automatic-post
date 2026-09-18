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


def test_recall_keeps_candidates_whose_numbers_differ() -> None:
    """写法差异与细节补充不得被挡在召回之外，是否同一事件由 LLM 判。

    这些标题的数字序列并不相等（U-20/U20、八强/8强、多出「替补4分钟」），
    早先的数字硬过滤会把它们直接丢弃，导致同一事件被反复发布。
    """

    articles = [
        _article(1, "韩媒：韩国U-20女足1-0阿根廷，时隔12年进八强", [100]),
        _article(2, "韩媒：全北2-1逆转柏太阳神，伊塔洛制胜", [100]),
    ]
    selected = title_dedup.select_candidates(
        "韩媒：韩国U20女足1-0阿根廷，时隔12年进8强",
        articles,
        candidate_id=5,
        channels=[100],
        dice_min=0.25,
        lcs_min=4,
        limit=3,
    )
    assert 1 in [item["article"]["id"] for item in selected]

    selected = title_dedup.select_candidates(
        "韩媒：全北2-1逆转柏太阳神，伊塔洛替补4分钟制胜",
        articles,
        candidate_id=5,
        channels=[100],
        dice_min=0.25,
        lcs_min=4,
        limit=3,
    )
    assert 2 in [item["article"]["id"] for item in selected]


def test_same_tab_recalls_when_channels_do_not_overlap() -> None:
    """标签无交集但同栏目且词面高度相似时必须入围。

    同一事件的稿件常被路由规则打上完全不同的标签（真实案例：同一场亚运比赛
    分别落在国际栏目与 K 联赛栏目），只靠标签交集会整组漏掉。
    """

    articles = [
        {**_article(1, "韩媒：韩国裁判协会就判罚争议道歉", [999]), "tab_id": 3},
    ]
    selected = title_dedup.select_candidates(
        "韩媒：韩国裁判协会就判罚争议道歉并整改",
        articles,
        candidate_id=5,
        channels=[100],          # 与候选的 [999] 无交集
        dice_min=0.25,
        lcs_min=4,
        limit=3,
        tab_id=3,
        tab_dice_min=0.6,
    )
    assert [item["article"]["id"] for item in selected] == [1]
    assert selected[0]["matched_by"] == "tab"
    assert selected[0]["shared_channels"] == []


def test_same_tab_requires_higher_similarity_than_shared_channel() -> None:
    """仅靠同栏目入围时，低于 tab_dice_min 的低相似候选必须被挡掉。"""

    # 词面有重叠（能过 lcs_min），但 bigram dice 远低于 0.6
    articles = [
        {**_article(1, "韩媒：韩国裁判协会公布本季度执法安排与培训计划", [999]), "tab_id": 3},
    ]
    kwargs = dict(
        candidate_id=5, dice_min=0.25, lcs_min=4, limit=3, tab_id=3,
    )
    low = title_dedup.select_candidates(
        "韩媒：韩国裁判协会就判罚争议道歉", articles, channels=[100],
        tab_dice_min=0.6, **kwargs,
    )
    assert low == []

    # 同一对候选，若标签有交集则仍按原本较宽的门槛入围
    shared = title_dedup.select_candidates(
        "韩媒：韩国裁判协会就判罚争议道歉", articles, channels=[999],
        tab_dice_min=0.6, **kwargs,
    )
    assert [item["article"]["id"] for item in shared] == [1]
    assert shared[0]["matched_by"] == "channels"


def test_same_tab_gate_disabled_without_threshold() -> None:
    """未配置 tab_dice_min 时行为与改动前一致：同栏目不构成入围理由。"""

    articles = [
        {**_article(1, "韩媒：韩国裁判协会就判罚争议道歉", [999]), "tab_id": 3},
    ]
    selected = title_dedup.select_candidates(
        "韩媒：韩国裁判协会就判罚争议道歉并整改",
        articles,
        candidate_id=5,
        channels=[100],
        dice_min=0.25,
        lcs_min=4,
        limit=3,
        tab_id=3,
        tab_dice_min=None,
    )
    assert selected == []


def test_match_scope_label_distinguishes_channel_and_tab() -> None:
    assert title_dedup.match_scope_label({"shared_channels": [104, 1568]}) == "共同标签 [104, 1568]"
    assert title_dedup.match_scope_label({"shared_channels": []}) == "同一栏目"


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
