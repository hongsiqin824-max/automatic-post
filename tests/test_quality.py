from __future__ import annotations

from app.services.quality import (
    NON_COMPETITION_LABEL,
    LLMCallError,
    LLMService,
    analyze_body_language,
    check_league_membership,
    classify_article_tab,
    evaluate,
    html_to_text,
    is_photo_credit_advisory_plan,
    normalize_competition_name,
    plan_local_repair,
    verify_removal_keeps_facts,
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


class _SequencedCompletions:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return _FakeResponse(next(self.contents))


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


def test_llm_invalid_json_is_retried_once_with_strict_instruction(monkeypatch):
    service = LLMService("key", "https://example.test/v1", "test", max_retries=2, retry_delay_seconds=0.1)
    completions = _SequencedCompletions(["not-json", '{"ok":true}'])
    fake = _FakeClient([])
    fake.chat.completions = completions
    monkeypatch.setattr(service, "_get_client", lambda: fake)

    assert service.chat_json("{}") == {"ok": True}
    assert len(completions.requests) == 2
    assert "上次返回为空或格式无效" in completions.requests[1]["messages"][0]["content"]


def test_llm_invalid_json_stops_after_one_strict_retry(monkeypatch):
    service = LLMService("key", "https://example.test/v1", "test", max_retries=2, retry_delay_seconds=0.1)
    completions = _SequencedCompletions(["not-json", "still-not-json"])
    fake = _FakeClient([])
    fake.chat.completions = completions
    monkeypatch.setattr(service, "_get_client", lambda: fake)

    try:
        service.chat_json("{}")
    except LLMCallError as exc:
        assert exc.category == "invalid_response"
        assert exc.retryable is False
        assert exc.attempts == 2
    else:
        raise AssertionError("expected LLMCallError")
    assert len(completions.requests) == 2


def test_llm_empty_content_gets_the_same_single_strict_retry(monkeypatch):
    service = LLMService("key", "https://example.test/v1", "test", max_retries=0)
    completions = _SequencedCompletions(["", '{"ok":true}'])
    fake = _FakeClient([])
    fake.chat.completions = completions
    monkeypatch.setattr(service, "_get_client", lambda: fake)

    assert service.chat_json("{}") == {"ok": True}
    assert len(completions.requests) == 2


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


def test_dirty_pattern_no_longer_fires_on_words_ending_in_tu():
    # 原先 DIRTY_PATTERNS 里的裸「图[：:]」把「波尔图：」「意图：」「拼图：」全判成
    # 图注残留，全库 6 次命中 5 次误判。收窄成「【图：」后这些正常正文必须放行。
    for body in (
        "<p>本轮西甲赛前发布会公布了首发名单，波尔图：迪奥戈-科斯塔、阿尔贝托-科斯塔和内乌恩-佩雷斯出战。</p>",
        "<p>主帅赛后透露了起用古贺的意图：希望他在前场做支点，让球队重新调整进攻节奏。</p>",
        "<p>主帅一直希望留下这名球员，并把他看作关键拼图：他是阵中最好的终结者之一。</p>",
    ):
        result = evaluate(title="西甲球队公布本轮首发名单", body=body, channels=[1])

        assert result["issues"]["dirty_content"] == []


def test_bracketed_photo_caption_marker_still_flags_dirty_content():
    # 收窄后仍要接住真正的图注标记「【图：Getty Images】」。
    result = evaluate(
        title="巴黎圣日耳曼夺得欧洲超级杯冠军",
        body="<p>法甲巴黎圣日耳曼在欧洲超级杯中击败对手夺冠，全场表现稳健。【图：Getty Images】</p>",
        channels=[1],
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
        title="记者：球队将在周末迎战，",
        body="<p>球队确认将在周末进行一场国家队，完整参赛名单和比赛地点已经公布。</p>",
        channels=[],
    )
    assert result["needs_review"] is True
    assert result["title_fix_method"] == "manual_review_no_llm"


def test_title_ending_in_an_ambiguous_single_word_is_not_truncated():
    """「门将」「浦和」「阿尔艾因」都以曾被当作截断信号的单字收尾，但标题是完整的。

    中文没有词边界，按单字后缀判截断分不清「将要」和「门将」；生产库里 48 次
    命中全是这类误判，每一篇都被强制转人工。
    """

    body = "<p>球队确认将在周末进行一场国家队，完整参赛名单和比赛地点已经公布。</p>"
    for title in (
        "环球体育：迪达被视为克鲁塞罗队史最佳门将",
        "阿莱首发首秀破门，广岛4-1浦和",
        "韩媒：C罗掐脖子，胜利0-4惨败阿尔艾因",
        "每体：佩德里父亲揭秘拒去特内里费原因",
        "罗体：那不勒斯锋线引援悬念仍在",
        "韩媒：春川市民队首次办集中体能评估，官兵参与",
        "日媒：日本办韩国职业联赛观赛派对",
    ):
        issues = evaluate(title=title, body=body, channels=[])["issues"]["title_problems"]
        assert issues == [], f"{title} -> {issues}"


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
        "fallback_eligible": True,
        "status_code": 502,
        "request_id": None,
        "attempts": 3,
        "elapsed_ms": 1234,
        "model": "test-model",
        "timeout_seconds": 12,
        "message": "bad gateway",
    }


class _ProviderDownLLM:
    """Primary provider is entirely down (e.g. 403 key/group revoked)."""

    configured = True
    model = "primary-model"
    timeout = 45

    def chat_json(self, prompt):
        raise LLMCallError(
            "AI 服务调用失败: Error code: 403 - API Key 所属分组已删除",
            category="http_error", retryable=False, fallback_eligible=True,
            status_code=403, attempts=1, elapsed_ms=5780,
            model=self.model, timeout_seconds=self.timeout,
        )


class _HealthyFallbackLLM:
    configured = True
    model = "fallback-model"

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "needs_review": False,
            "reason": "内容完整",
        }


def test_provider_down_403_switches_to_fallback_model():
    fallback = _HealthyFallbackLLM()
    result = evaluate(
        title="球队公布完整比赛安排",
        body="<p>球队今天公布了完整比赛安排，包括比赛时间、地点、参赛名单以及面向球迷的交通信息。</p>",
        channels=[],
        llm=_ProviderDownLLM(),
        llm_fallback=fallback,
    )
    # The fallback provider was healthy, so the article is not forced to review
    # by the primary's 403 and the transport error is cleared.
    assert result["semantic_error"] is None
    assert result["needs_review"] is False
    assert "AI 服务调用失败，需要人工确认" not in result["issues"]["semantic_problems"]


class _InvalidResponseLLM:
    """Primary returns an unusable payload (non-transport failure)."""

    configured = True
    model = "primary-model"
    timeout = 45

    def chat_json(self, prompt):
        raise LLMCallError(
            "AI 返回不是合法 JSON", category="invalid_response", retryable=False,
            model=self.model, timeout_seconds=self.timeout,
        )


def test_any_primary_failure_switches_to_fallback_model():
    # Even a non-transport failure (invalid_response) must try the fallback.
    result = evaluate(
        title="球队公布完整比赛安排",
        body="<p>球队今天公布了完整比赛安排，包括比赛时间、地点、参赛名单以及面向球迷的交通信息。</p>",
        channels=[],
        llm=_InvalidResponseLLM(),
        llm_fallback=_HealthyFallbackLLM(),
    )
    assert result["semantic_error"] is None
    assert result["needs_review"] is False
    assert "AI 服务调用失败，需要人工确认" not in result["issues"]["semantic_problems"]


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
        body="<p>球队确认将在周末进行一场国家队，完整参赛名单和比赛地点已经公布。</p>",
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
            "repairable": True,
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
            "repairable": True,
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
                "block_id": "b2",
                "action": "remove_block",
                "evidence": "点击这里关注 WhatsApp 频道，获取最新消息",
                "confidence": 0.99,
            }],
        }


def test_repair_plan_cannot_coexist_with_semantic_pass():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body=(
            "<p>球队在本轮联赛中取胜，报道包含比赛过程、球员表现和赛后采访。</p>"
            "<p>点击这里关注 WhatsApp 频道，获取最新消息</p>"
        ),
        channels=[1],
        llm=_ContradictoryRepairPlanLLM(),
    )

    assert result["pass"] is False
    assert result["needs_review"] is True
    assert result["decision"] == "repairable"
    assert result["repair_plan_error"] is None
    assert "矛盾" in result["repair_plan_warning"]
    assert result["reason"]


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


class _MissingRepairablePromotionLLM:
    configured = True

    def chat_json(self, prompt):
        return {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "needs_review": True,
            "reason": "正文末尾存在可定位的推广段落",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": "点击这里关注 WhatsApp 频道，获取最新消息",
                "issue_type": "advertisement",
                "reason": "独立推广内容，与新闻事实无关",
                "confidence": 0.99,
            }],
        }


def test_repair_plan_requires_explicit_repairable_true():
    result = evaluate(
        title="球队公布本轮联赛完整比赛结果",
        body=(
            "<p>球队在本轮联赛中取胜，报道包含进球过程、球员表现和赛后采访。</p>"
            "<p>点击这里关注 WhatsApp 频道，获取最新消息</p>"
        ),
        channels=[1],
        llm=_MissingRepairablePromotionLLM(),
    )

    assert result["pass"] is False
    assert result["repair_plans"]
    assert result["decision"] == "repairable"
    assert result["repair_plan_error"] is None
    assert result["repair_plan_warning"]


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

    assert result["repair_plan_error"] == "AI 修复计划与正文完整性结论矛盾"
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


class _StubVerifierLLM:
    """最小 LLM 替身：直接返回预置的核验 JSON 或抛出预置异常。"""

    configured = True
    model = "stub-verifier"
    timeout = 10

    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error
        self.prompts: list[str] = []

    def chat_json(self, prompt):
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        return self._payload


def test_verify_removal_accepts_confident_no_fact_loss() -> None:
    llm = _StubVerifierLLM({"loses_fact": False, "confidence": 0.96, "reason": "仅推广内容"})
    result = verify_removal_keeps_facts(
        removed_text="请关注我们的官方频道获取最新消息",
        kept_text="球队在主场以三比一取胜。",
        body="<p>球队在主场以三比一取胜。请关注我们的官方频道获取最新消息</p>",
        llm=llm,
    )
    assert result["safe"] is True
    assert result["loses_fact"] is False
    assert "请关注我们的官方频道获取最新消息" in llm.prompts[0]


def test_verify_removal_rejects_fact_loss() -> None:
    llm = _StubVerifierLLM({"loses_fact": True, "confidence": 0.99, "reason": "含比分"})
    result = verify_removal_keeps_facts(
        removed_text="下半场补时阶段又追加一球。",
        kept_text="球队在主场取胜。",
        body="<p>球队在主场取胜。下半场补时阶段又追加一球。</p>",
        llm=llm,
    )
    assert result["safe"] is False


def test_verify_removal_rejects_low_confidence() -> None:
    llm = _StubVerifierLLM({"loses_fact": False, "confidence": 0.7, "reason": "不太确定"})
    result = verify_removal_keeps_facts(
        removed_text="某段内容",
        kept_text="保留内容",
        body="<p>保留内容某段内容</p>",
        llm=llm,
    )
    assert result["safe"] is False


def test_verify_removal_fails_closed_on_malformed_payload() -> None:
    llm = _StubVerifierLLM({"loses_fact": "no", "confidence": 0.99})
    result = verify_removal_keeps_facts(
        removed_text="某段内容", kept_text="保留内容", body="<p>正文</p>", llm=llm
    )
    assert result["safe"] is False
    assert result["error"] == "核验字段格式错误"


def test_verify_removal_fails_closed_on_call_error() -> None:
    llm = _StubVerifierLLM(
        error=LLMCallError("核验超时", category="timeout", retryable=True)
    )
    result = verify_removal_keeps_facts(
        removed_text="某段内容", kept_text="保留内容", body="<p>正文</p>", llm=llm
    )
    assert result["safe"] is False
    assert "核验超时" in str(result["error"])


def test_verify_removal_uses_fallback_model_after_primary_failure() -> None:
    primary = _StubVerifierLLM(
        error=LLMCallError("主模型不可用", category="connection", retryable=True)
    )
    fallback = _StubVerifierLLM({"loses_fact": False, "confidence": 0.95, "reason": "仅模板残留"})
    result = verify_removal_keeps_facts(
        removed_text="関連記事",
        kept_text="球队在主场以三比一取胜。",
        body="<p>球队在主场以三比一取胜。関連記事</p>",
        llm=primary,
        llm_fallback=fallback,
    )
    assert result["safe"] is True
    assert fallback.prompts


class _StubClassifierLLM:
    configured = True
    model = "test-model"

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []

    def chat_json(self, prompt):
        self.prompts.append(prompt)
        return self.payload


_TAB_CHOICES = [
    {"id": 9, "name": "日职联", "ai_league_guard_definition": "日本J1联赛"},
    {"id": 24, "name": "日职乙", "ai_league_guard_definition": "日本J2联赛"},
]


def test_classify_article_tab_returns_picked_column() -> None:
    llm = _StubClassifierLLM({
        "tab_id": 24, "confidence": 0.93, "reason": "秋田属于J2",
        "actual_competition": "日本J2联赛",
    })
    result = classify_article_tab("秋田蓝闪电取胜", "<p>正文</p>", _TAB_CHOICES, llm)
    assert result == {
        "tab_id": 24, "confidence": 0.93, "reason": "秋田属于J2",
        "actual_competition": "日本J2联赛",
    }
    # 每个候选的名称与定义都必须进提示词，否则模型只能靠栏目名猜。
    assert "日职乙" in llm.prompts[0]
    assert "日本J2联赛" in llm.prompts[0]
    assert "id=24" in llm.prompts[0]


def test_classify_article_tab_reports_competition_without_a_column() -> None:
    """候选里没有对应栏目时，仍要报出真实赛事——这是发现该建哪些栏目的依据。"""

    llm = _StubClassifierLLM({
        "tab_id": None, "confidence": 0.96, "reason": "亚运会女足，无匹配栏目",
        "actual_competition": "亚运会",
    })
    result = classify_article_tab("韩国女足6-0孟加拉国", "<p>正文</p>", _TAB_CHOICES, llm)
    assert result["tab_id"] is None
    assert result["actual_competition"] == "亚运会"
    # 提示词必须要求这个字段，否则模型不会主动给。
    assert "actual_competition" in llm.prompts[0]


def test_classify_prompt_defers_non_match_content_to_the_column_definition() -> None:
    """非比赛内容的归属交给栏目定义，不能在提示词里一刀切说「不属于任何栏目」。

    联赛型栏目的定义收录「各参赛俱乐部的相关新闻」，瑞典超球员的续约本就在范围
    内；写死的排除规则会把这类文章压成草稿。
    """

    llm = _StubClassifierLLM({
        "tab_id": 24, "confidence": 0.95, "reason": "秋田在役球员续约",
        "actual_competition": "日职乙",
    })
    classify_article_tab("秋田后卫续约一年", "<p>正文</p>", _TAB_CHOICES, llm)
    prompt = llm.prompts[0]
    assert "一律以栏目定义为准" in prompt
    assert "转入方" in prompt
    # 旧的一刀切规则必须消失，否则它会压过上面那条。
    assert "非赛事内容不属于任何赛事栏目" not in prompt


def test_membership_prompt_shares_the_non_match_content_rule() -> None:
    """二分类与多分类必须同口径，否则一个判「不属于」、另一个又挑回同类栏目。"""

    llm = _StubClassifierLLM({"belongs": True, "confidence": 0.95, "reason": "巴甲球队续约"})
    check_league_membership("弗拉门戈续约主帅", "<p>正文</p>", "巴甲", "巴甲定义", llm)
    assert "一律以栏目定义为准" in llm.prompts[0]


def test_normalize_competition_name_aligns_with_an_existing_column() -> None:
    """已有栏目的写法优先——操作者认的是栏目名。"""

    assert normalize_competition_name("日职乙J2联赛", ["日职联", "日职乙"]) == "日职乙"


def test_normalize_competition_name_collapses_known_spellings() -> None:
    assert normalize_competition_name("南美解放者杯") == "解放者杯"
    assert normalize_competition_name("2026赛季解放者杯半决赛") == "解放者杯"
    assert normalize_competition_name("POWER WORK CUP") == "POWER WORK杯"


def test_normalize_competition_name_does_not_split_a_stage_word() -> None:
    """「总决赛」必须整词清掉：先匹配「决赛」会留下一个「总」字挂在赛事名后面。"""

    assert normalize_competition_name("NBA总决赛", ["NBA", "CBA"]) == "NBA"
    assert normalize_competition_name("欧冠1/8决赛") == "欧冠"


def test_normalize_competition_name_treats_any_non_event_wording_the_same() -> None:
    """模型偶尔写成「非赛事（转会）」，那仍然是对不上任何联赛，不能当赛事名展示。"""

    assert normalize_competition_name("非赛事（转会）") == NON_COMPETITION_LABEL


def test_normalize_competition_name_keeps_qualifiers_that_change_the_event() -> None:
    """「预选赛」不是阶段词：去掉它世界杯预选赛就变成了世界杯，那是另一项赛事。"""

    assert normalize_competition_name("2026世界杯预选赛") == "世界杯预选赛"
    assert normalize_competition_name(NON_COMPETITION_LABEL) == NON_COMPETITION_LABEL
    assert normalize_competition_name("  ") == ""


def test_normalize_competition_name_keeps_ambiguous_prefix_unresolved() -> None:
    """「亚冠」同时是两个栏目的前缀，猜哪个都可能挂错，保留原文交给人看。"""

    assert normalize_competition_name("亚冠", ["亚冠精英", "亚冠2"]) == "亚冠"


def test_classify_article_tab_accepts_null_as_no_match() -> None:
    llm = _StubClassifierLLM({"tab_id": None, "confidence": 0.9, "reason": "转会新闻"})
    result = classify_article_tab("梅西将成俱乐部股东", "<p>正文</p>", _TAB_CHOICES, llm)
    assert result["tab_id"] is None
    assert result["reason"] == "转会新闻"


def test_classify_article_tab_rejects_column_that_was_not_offered() -> None:
    """模型报了一个没被提供的栏目，只能当调用失败。

    放行会把文章挂到未经校验的栏目上，比留草稿更糟，所以这里必须抛错让上层
    fail closed。
    """

    llm = _StubClassifierLLM({"tab_id": 999, "confidence": 0.99, "reason": "凭空捏造"})
    try:
        classify_article_tab("标题", "<p>正文</p>", _TAB_CHOICES, llm)
    except LLMCallError as exc:
        assert "未提供的栏目 ID 999" in str(exc)
    else:
        raise AssertionError("未提供的栏目 ID 必须抛 LLMCallError")


def test_classify_article_tab_rejects_missing_confidence() -> None:
    llm = _StubClassifierLLM({"tab_id": 24, "reason": "缺少置信度"})
    try:
        classify_article_tab("标题", "<p>正文</p>", _TAB_CHOICES, llm)
    except LLMCallError as exc:
        assert exc.category == "invalid_response"
    else:
        raise AssertionError("缺少 confidence 必须抛 LLMCallError")


def test_classify_article_tab_requires_candidates() -> None:
    """没有可选栏目时不该白花一次调用。"""

    llm = _StubClassifierLLM({"tab_id": None, "confidence": 1.0, "reason": ""})
    try:
        classify_article_tab("标题", "<p>正文</p>", [], llm)
    except LLMCallError as exc:
        assert exc.category == "configuration"
    else:
        raise AssertionError("空候选集必须抛 LLMCallError")
    assert llm.prompts == []
