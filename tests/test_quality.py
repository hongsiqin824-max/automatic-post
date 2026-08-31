from __future__ import annotations

from app.services.quality import analyze_body_language, evaluate, html_to_text


def test_clean_article_passes_without_llm():
    result = evaluate(
        title="主队在联赛中取得关键胜利",
        body="<p>这是一段完整的体育新闻正文，介绍了比赛过程、关键球员表现以及赛后的相关信息。</p>",
        channels=[1, 2],
    )
    assert result["pass"] is True
    assert result["level"] == "B"


def test_incomplete_body_goes_to_review():
    result = evaluate(title="主队取得胜利", body="<p>太短</p>", channels=[])
    assert result["needs_review"] is True
    assert result["issues"]["completeness_problems"]


def test_dirty_content_goes_to_review():
    result = evaluate(
        title="球队公布最新比赛安排",
        body="<p>球队今天公布了完整比赛安排，球迷可以查看全部赛程。扫码关注并点击购买官方商品。</p>",
        channels=[],
    )
    assert result["needs_review"] is True
    assert result["issues"]["dirty_content"]


def test_truncated_title_without_llm_goes_to_review():
    result = evaluate(
        title="记者：球队将在",
        body="<p>球队确认将在周末进行一场友谊赛，完整参赛名单和比赛地点已经公布。</p>",
        channels=[],
    )
    assert result["needs_review"] is True
    assert result["title_fix_method"] == "manual_review_no_llm"


def test_html_to_text_removes_markup():
    assert html_to_text("<p>第一段</p><p>第二段</p>") == "第一段 第二段"


def test_language_check_flags_more_than_sixty_percent_non_chinese():
    language = analyze_body_language("<p>中文abcd</p>")

    assert language["chinese_chars"] == 2
    assert language["non_chinese_chars"] == 4
    assert language["non_chinese_ratio"] == 0.6667
    assert language["exceeds_threshold"] is True


def test_language_check_does_not_flag_exactly_sixty_percent_non_chinese():
    language = analyze_body_language("<p>中文abc</p>")

    assert language["text_chars"] == 5
    assert language["non_chinese_ratio"] == 0.6
    assert language["threshold"] == 0.6
    assert language["exceeds_threshold"] is False


def test_language_check_ignores_html_whitespace_punctuation_and_numbers():
    language = analyze_body_language(
        "<div> 中 \n 文 </div><p>abc 123，！? - 2026</p>"
    )

    assert language["chinese_chars"] == 2
    assert language["non_chinese_chars"] == 3
    assert language["text_chars"] == 5
    assert language["non_chinese_ratio"] == 0.6


def test_evaluate_records_normal_chinese_language_result_without_blocking():
    result = evaluate(
        title="主队在联赛中取得关键胜利",
        body="<p>这是一段完整的中文体育新闻正文，介绍了比赛过程、球员表现以及赛后信息。</p>",
        channels=[1],
    )

    assert result["pass"] is True
    assert result["language_check"]["non_chinese_chars"] == 0
    assert result["language_check"]["non_chinese_ratio"] == 0.0
    assert result["language_check"]["exceeds_threshold"] is False


def test_language_check_treats_body_without_letters_as_zero_ratio():
    language = analyze_body_language("<p>12345，！? 2026-08-27</p>")

    assert language["chinese_chars"] == 0
    assert language["non_chinese_chars"] == 0
    assert language["text_chars"] == 0
    assert language["non_chinese_ratio"] == 0.0
    assert language["exceeds_threshold"] is False


class _SemanticLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": False,
            "has_ad_or_dirty": False,
            "needs_review": True,
            "reason": "正文在关键比赛结果前结束",
        }


def test_llm_semantic_check_catches_non_pattern_truncation():
    result = evaluate(
        title="球队公布本轮杯赛首发名单",
        body="<p>球队已经公布首发阵容，比赛开场后双方互有攻守，主队在第八十分钟获得关键机会但结果没有继续说明。</p>",
        channels=[],
        llm=_SemanticLLM(),
    )
    assert result["needs_review"] is True
    assert result["semantic_check_used"] is True
    assert "AI 判断正文可能不完整" in result["issues"]["completeness_problems"][0]


class _FailingLLM:
    configured = True

    def chat_json(self, prompt):
        raise RuntimeError("service unavailable")


def test_llm_failure_routes_article_to_review():
    result = evaluate(
        title="球队公布完整比赛安排",
        body="<p>球队今天公布了完整比赛安排，包括比赛时间、地点、参赛名单以及面向球迷的交通信息。</p>",
        channels=[],
        llm=_FailingLLM(),
    )
    assert result["needs_review"] is True
    assert result["issues"]["semantic_problems"]


class _SemanticTitleFixLLM:
    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, prompt):
        self.calls += 1
        if self.calls == 1:
            return {
                "title_complete": False,
                "body_complete": True,
                "has_ad_or_dirty": False,
                "needs_review": True,
                "reason": "标题缺少具体比赛结果",
            }
        return {"title": "主队在杯赛中以二比一击败客队并晋级"}


def test_llm_can_fix_semantically_incomplete_title():
    llm = _SemanticTitleFixLLM()
    result = evaluate(
        title="球队公布比赛相关消息",
        body="<p>主队在杯赛中以二比一击败客队并晋级下一轮，赛后双方主教练都确认了最终比赛结果。</p>",
        channels=[],
        llm=llm,
    )
    assert result["pass"] is True
    assert result["title_fix_method"] == "llm"
    assert result["title_after"] == "主队在杯赛中以二比一击败客队并晋级"
    assert llm.calls == 2


class _StructuredPromotionLLM:
    configured = True

    def chat_json(self, prompt):
        assert "b2 <p>: 点击这里关注 WhatsApp 频道，获取最新消息" in prompt
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "needs_review": True,
            "reason": "正文末尾含 WhatsApp 频道引流信息",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": "点击这里关注 WhatsApp 频道，获取最新消息",
                "confidence": 0.98,
            }],
        }


def test_llm_semantic_check_exposes_structured_targeted_repair_plan():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body=(
            "<p>球队在本轮联赛中取胜，报道包含进球过程、球员表现和赛后采访。</p>"
            "<p>点击这里关注 WhatsApp 频道，获取最新消息</p>"
        ),
        channels=[1],
        llm=_StructuredPromotionLLM(),
    )

    assert result["needs_review"] is True
    assert result["repair_plan_error"] is None
    assert result["repair_plans"][0]["block_id"] == "b2"


class _LinePromotionLLM:
    configured = True

    def chat_json(self, prompt):
        teaser = "【视频】佐藤龙之介送出引发进球的凶狠逼抢，以及他的威胁场面"
        assert f"b1.s2 独立行: {teaser}" in prompt
        assert '"action":"remove_text_line"' in prompt
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "needs_review": True,
            "reason": "正文含独立视频引流行",
            "repair_plans": [{
                "segment_id": "b1.s2",
                "action": "remove_text_line",
                "evidence": teaser,
                "confidence": 0.99,
            }],
        }


def test_llm_semantic_check_exposes_line_level_repair_target():
    teaser = "【视频】佐藤龙之介送出引发进球的凶狠逼抢，以及他的威胁场面"
    result = evaluate(
        title="佐藤龙之介首发出场，瓦伦西亚客场告负",
        body=(
            "<p>佐藤龙之介本轮首发出场约60分钟。\n"
            f"{teaser}\n"
            "他在第26分钟参与前场逼抢并帮助球队取得进球。</p>"
        ),
        channels=[1],
        llm=_LinePromotionLLM(),
    )

    assert result["needs_review"] is True
    assert result["repair_plan_error"] is None
    assert result["repair_plans"] == [{
        "segment_id": "b1.s2",
        "action": "remove_text_line",
        "evidence": teaser,
        "confidence": 0.99,
    }]


class _MalformedSemanticLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": "true",
            "body_complete": True,
            "has_ad_or_dirty": True,
            "needs_review": True,
            "reason": "字段格式异常",
            "repair_plans": ["delete everything"],
        }


def test_malformed_semantic_result_is_explicitly_blocked():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body="<p>球队在本轮联赛中取胜，报道包含比赛过程、球员表现和赛后采访。</p>",
        channels=[1],
        llm=_MalformedSemanticLLM(),
    )

    assert result["needs_review"] is True
    assert result["repair_plans"] == []
    assert result["repair_plan_error"]
    assert result["issues"]["semantic_problems"]


class _ContradictoryRepairPlanLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "needs_review": False,
            "reason": "内容正常",
            "repair_plans": [{
                "block_id": "b1",
                "action": "remove_block",
                "evidence": "点击这里关注 WhatsApp 频道，获取最新消息",
                "confidence": 0.99,
            }],
        }


def test_repair_plan_cannot_coexist_with_semantic_pass():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body="<p>点击这里关注 WhatsApp 频道，获取最新消息</p>",
        channels=[1],
        llm=_ContradictoryRepairPlanLLM(),
    )

    assert result["pass"] is False
    assert result["needs_review"] is True
    assert result["repair_plan_error"] == "AI 修复计划与质检结论矛盾"
