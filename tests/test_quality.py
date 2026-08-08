from __future__ import annotations

from app.services.quality import evaluate, html_to_text


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
