from __future__ import annotations

from app.services.quality import (
    LLMCallError,
    LLMService,
    analyze_body_language,
    evaluate,
    html_to_text,
    is_photo_credit_advisory_plan,
    plan_local_repair,
)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FlakyCompletions:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _FakeResponse('{"ok":true}')


class _FakeClient:
    def __init__(self, failures):
        self.chat = type("Chat", (), {"completions": _FlakyCompletions(failures)})()


def test_clean_article_passes_without_llm():
    result = evaluate(
        title="主队在联赛中取得关键胜利",
        body="<p>这是一段完整的体育新闻正文，介绍了比赛过程、关键球员表现以及赛后的相关信息。</p>",
        channels=[1, 2],
    )
    assert result["pass"] is True
    assert result["level"] == "B"


def test_llm_http_502_retries_and_exposes_diagnostics(monkeypatch):
    service = LLMService("key", "https://example.test/v1", "test", max_retries=2, retry_delay_seconds=0.1)
    fake = _FakeClient([RuntimeError("upstream"), RuntimeError("upstream")])
    fake.chat.completions.failures[0].status_code = 502
    fake.chat.completions.failures[1].status_code = 502
    monkeypatch.setattr(service, "_get_client", lambda: fake)

    assert service.chat_json("{}") == {"ok": True}
    assert fake.chat.completions.calls == 3


def test_llm_invalid_json_is_not_retried(monkeypatch):
    service = LLMService("key", "https://example.test/v1", "test", max_retries=2, retry_delay_seconds=0.1)
    fake = _FakeClient([])
    fake.chat.completions.create = lambda **kwargs: _FakeResponse("not-json")
    monkeypatch.setattr(service, "_get_client", lambda: fake)

    try:
        service.chat_json("{}")
    except LLMCallError as exc:
        assert exc.category == "invalid_response"
        assert exc.retryable is False
        assert exc.attempts == 1
    else:
        raise AssertionError("expected LLMCallError")


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


def test_unstripped_clickable_attributes_go_to_review():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body='<p data-href="https://example.com">球队在本轮联赛中取胜，报道包含比赛过程、球员表现和赛后采访。</p>',
        channels=[1],
    )

    assert result["needs_review"] is True
    assert "可跳转属性" in result["issues"]["dirty_content"][0]


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


class _ClassifiedFailingLLM:
    configured = True
    model = "test-model"
    timeout = 12

    def chat_json(self, prompt):
        raise LLMCallError(
            "bad gateway", category="http_error", retryable=True,
            status_code=502, attempts=3, elapsed_ms=1234,
            model=self.model, timeout_seconds=self.timeout,
        )


def test_llm_failure_routes_article_to_review():
    result = evaluate(
        title="球队公布完整比赛安排",
        body="<p>球队今天公布了完整比赛安排，包括比赛时间、地点、参赛名单以及面向球迷的交通信息。</p>",
        channels=[],
        llm=_FailingLLM(),
    )
    assert result["needs_review"] is True
    assert result["issues"]["semantic_problems"]


def test_classified_llm_failure_is_persisted_without_passing_quality():
    result = evaluate(
        title="球队公布完整比赛安排",
        body="<p>球队今天公布了完整比赛安排，包括比赛时间、地点、参赛名单以及面向球迷的交通信息。</p>",
        channels=[],
        llm=_ClassifiedFailingLLM(),
    )
    assert result["pass"] is False
    assert result["semantic_error"] == {
        "category": "http_error",
        "retryable": True,
        "status_code": 502,
        "request_id": None,
        "attempts": 3,
        "elapsed_ms": 1234,
        "model": "test-model",
        "timeout_seconds": 12,
        "message": "bad gateway",
    }


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


class _TitleFixFailureLLM:
    configured = True
    model = "title-model"
    timeout = 15

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
                "reason": "标题疑似不完整",
            }
        raise LLMCallError(
            "title bad gateway",
            category="http_error",
            retryable=True,
            status_code=502,
            attempts=3,
            elapsed_ms=900,
            model=self.model,
            timeout_seconds=self.timeout,
        )


def test_title_fix_failure_preserves_ai_diagnostics():
    result = evaluate(
        title="记者：球队将在",
        body="<p>球队确认将在周末进行一场友谊赛，完整参赛名单和比赛地点已经公布。</p>",
        channels=[],
        llm=_TitleFixFailureLLM(),
    )

    assert result["pass"] is False
    assert result["title_error"]["status_code"] == 502
    assert result["title_error"]["attempts"] == 3
    assert any(
        "标题自动修正调用失败" in item
        for item in result["issues"]["semantic_problems"]
    )


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


class _DuplicateContentLLM:
    configured = True

    def chat_json(self, prompt):
        repeated = "费内巴切已确认两名球员正接受欧足联纪律调查。"
        assert "duplicate_content" in prompt
        assert f"b1 <p>: {repeated}" in prompt
        assert f"b3 <p>: {repeated}" in prompt
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "repairable": True,
            "needs_review": True,
            "reason": "正文存在完全重复段落",
            "repair_plans": [{
                "block_id": "b3",
                "keep_block_id": "b1",
                "action": "remove_block",
                "evidence": repeated,
                "issue_type": "duplicate_content",
                "reason": "b3 与 b1 完全重复，保留首次出现段落",
                "confidence": 0.98,
            }],
        }


def test_semantic_duplicate_plan_is_not_treated_as_advertising_contradiction():
    repeated = "费内巴切已确认两名球员正接受欧足联纪律调查。"
    result = evaluate(
        title="费内巴切确认两名球员接受纪律调查",
        body=f"<p>{repeated}</p><p>俱乐部补充了案件背景。</p><p>{repeated}</p>",
        channels=[1],
        llm=_DuplicateContentLLM(),
    )

    assert result["needs_review"] is True
    assert result["repair_plan_error"] is None
    assert result["repair_plans"][0]["issue_type"] == "duplicate_content"


class _LocalRepairPlannerLLM:
    configured = True

    def __init__(self):
        self.prompt = ""

    def chat_json(self, prompt):
        self.prompt = prompt
        return {
            "repairable": True,
            "reason": "可精确删除无关图片入口",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": "【图片】球员喜欢的两位世界级球星",
                "issue_type": "extraneous_content",
                "reason": "该图片入口与新闻事实无关",
                "confidence": 0.91,
            }],
        }


def test_local_repair_planner_uses_first_quality_reason_and_stable_blocks():
    llm = _LocalRepairPlannerLLM()
    result = plan_local_repair(
        title="球员谈本轮联赛表现",
        body=(
            "<p>球员在赛后采访中回顾了本轮比赛。</p>"
            "<p>【图片】球员喜欢的两位世界级球星</p>"
        ),
        first_quality={
            "reason": "正文含与新闻无关的图片入口",
            "issues": {"semantic_problems": ["图片入口属于采集残留"]},
        },
        llm=llm,
    )

    assert result["repairable"] is True
    assert result["repair_plan_error"] is None
    assert result["repair_plans"][0]["block_id"] == "b2"
    assert "正文含与新闻无关的图片入口" in llm.prompt
    assert "b2 <p>: 【图片】球员喜欢的两位世界级球星" in llm.prompt


class _TailContextLLM:
    configured = True

    def __init__(self):
        self.prompt = ""

    def chat_json(self, prompt):
        self.prompt = prompt
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "needs_review": False,
            "reason": "内容正常",
        }


def test_semantic_check_includes_tail_blocks_for_long_articles():
    paragraphs = [
        f"<p>第{index}段报道介绍了球队的比赛过程、球员表现和赛后安排。</p>"
        for index in range(1, 66)
    ]
    paragraphs[-1] = "<p>请在 ge、Globo 和 SporTV 上观看关于球队的全部内容：</p>"
    llm = _TailContextLLM()

    result = evaluate(
        title="球队公布完整比赛安排",
        body="".join(paragraphs),
        channels=[1],
        llm=llm,
    )

    assert result["pass"] is True
    assert "中间正文块已省略，仅保留开头40块和结尾20块" in llm.prompt
    assert "b65 <p>: 请在 ge、Globo 和 SporTV 上观看关于球队的全部内容：" in llm.prompt


class _LinePromotionLLM:
    configured = True

    def chat_json(self, prompt):
        teaser = "【视频】佐藤龙之介送出引发进球的凶狠逼抢，以及他的威胁场面"
        assert f"b1.s2 独立行: {teaser}" in prompt
        assert '"action":"remove_text_line"' in prompt
        assert "issue_type、reason" in prompt
        assert "standalone_program_promotion" in prompt
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
                "issue_type": "video_promotion",
                "reason": "独立视频引流行，与新闻事实无关",
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
        "issue_type": "video_promotion",
        "reason": "独立视频引流行，与新闻事实无关",
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


class _RepairablePromotionLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
            "reason": "仅包含可定位的推广段落",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": "点击这里关注 WhatsApp 频道，获取最新消息",
                "confidence": 0.98,
            }],
        }


def test_repairable_promotion_plan_allows_needs_review_false_from_ai():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body=(
            "<p>球队在本轮联赛中取胜，报道包含进球过程、球员表现和赛后采访。</p>"
            "<p>点击这里关注 WhatsApp 频道，获取最新消息</p>"
        ),
        channels=[1],
        llm=_RepairablePromotionLLM(),
    )

    assert result["repair_plan_error"] is None
    assert result["needs_review"] is True
    assert result["issues"]["dirty_content"]


class _IncompleteBodyWithPlanLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": False,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
            "reason": "正文可能截断",
            "repair_plans": [{
                "block_id": "b1",
                "action": "remove_block",
                "evidence": "点击这里关注 WhatsApp 频道，获取最新消息",
                "confidence": 0.98,
            }],
        }


def test_plan_is_blocked_when_ai_marks_body_incomplete():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body="<p>点击这里关注 WhatsApp 频道，获取最新消息</p>",
        channels=[1],
        llm=_IncompleteBodyWithPlanLLM(),
    )

    assert result["repair_plan_error"] == "AI 修复计划与标题或正文完整性结论矛盾"
    assert result["needs_review"] is True


class _PhotoCreditAdvisoryLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "repairable": False,
            "needs_review": False,
            "reason": "内容完整，附带可选摄影署名建议",
            "repair_plans": [{
                "block_id": "b1",
                "action": "replace_text",
                "evidence": "前锋德田誉独中两元（Hiroyuki SATO）",
                "after": "前锋德田誉独中两元（摄影：Hiroyuki SATO）",
                "confidence": 0.9,
            }],
        }


def test_photo_credit_advisory_does_not_block_explicit_quality_pass():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body=(
            "<p>前锋德田誉独中两元（Hiroyuki SATO），"
            "球队在本轮联赛中取胜，报道包含进球过程和赛后采访。</p>"
        ),
        channels=[1],
        llm=_PhotoCreditAdvisoryLLM(),
    )

    assert result["pass"] is True
    assert result["needs_review"] is False
    assert result["repair_plan_error"] is None
    assert result["repair_plans"] == []
    assert result["advisory_repair_plans"][0]["action"] == "replace_text"


def test_photo_advisory_requires_explicit_credit_format_and_exact_evidence():
    plan = {
        "block_id": "b1",
        "action": "replace_text",
        "evidence": "球员赛后展示照片",
        "after": "球员赛后展示照片并接受采访",
        "confidence": 0.99,
    }

    assert not is_photo_credit_advisory_plan(
        plan,
        body="<p>球员赛后展示照片。</p>",
    )
