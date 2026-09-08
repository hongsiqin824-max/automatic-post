from __future__ import annotations

import pytest

from app.services.promotion_repair import (
    apply_repair_plan,
    body_safety_stats,
    content_blocks,
    find_promotional_blocks,
    find_promotional_lines,
    normalize_photo_credits,
    remove_promotional_blocks,
)


VIDEO_TEASER = "【视频】佐藤龙之介送出引发进球的凶狠逼抢，以及他的威胁场面"
VIDEO_REASON = "该独立视频引流行与新闻事实无关"
PROGRAM_PROMOTION = "●矢部浩之先生的新节目《J.LEAGUE WEEKEND 周日的矢部萨卡》开播！ ｜ J联赛"
SCOREBOARD_MARKER = "【积分榜】明治安田J1联赛2026/27"
BRANDED_WATCH_PROMOTION = (
    "请在 ge、Globo 和 SporTV 上观看关于瓦斯科达伽马的全部内容："
)


def test_ai_repair_plan_removes_10878_video_line_only() -> None:
    before = "西甲第3轮，瓦伦西亚客场作战，佐藤龙之介首发出场约60分钟。"
    after = "佐藤从赛季开局起连续3场首发，并在第26分钟参与了前场逼抢。"
    image = '<p><img src="/fastdfs8/story.jpg" alt="比赛图片" /></p>'
    context_text = (
        "瓦伦西亚当地媒体随后分析了球队的整体表现，并逐一评价了首发球员。"
        "报道肯定了球员回撤接应以及在前场施压的做法，同时指出球队控球不足。"
    ) * 3
    context = f"<p>{context_text}</p>"
    body = f"<p>{before}\n{VIDEO_TEASER}\n{after}</p>{image}{context}"
    segment = content_blocks(body)[0]["segments"][1]

    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": segment["segment_id"],
        "action": "remove_text_line",
        "evidence": VIDEO_TEASER,
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == f"<p>{before}\n{after}</p>{image}{context}"
    assert matches == [{
        "rule": "ai_targeted_repair",
        "action": "remove_text_line",
        "block_id": "b1",
        "tag": "p",
        "text": VIDEO_TEASER,
        "validation": "fixed_promotion_rule",
        "confidence": 0.99,
        "segment_id": "b1.s2",
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
    }]


def test_content_blocks_and_ai_plan_support_attribute_less_nested_presentation_tags() -> None:
    news = (
        "报道介绍了球队本轮比赛的完整过程、球员表现、主教练赛后采访以及后续训练安排，"
        "相关细节均已得到俱乐部官方确认。"
    ) * 3
    promotion = "在Kayo上观看每一场BBL比赛的直播，比赛期间无广告打断。Kayo新用户？"
    body = f"<p>{news}</p><p><b><i>{promotion}</i></b></p><p>{news}</p>"

    blocks = content_blocks(body)
    assert blocks[1]["text"] == promotion
    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": promotion,
        "issue_type": "advertisement",
        "reason": "该段是商业推广内容，与新闻事实无关",
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == f"<p>{news}</p><p>{news}</p>"
    assert matches[0]["text"] == promotion


def test_nested_presentation_tags_do_not_allow_attributes_or_arbitrary_markup() -> None:
    promotion = "在Kayo上观看每一场BBL比赛的直播，比赛期间无广告打断。Kayo新用户？"
    body = f"<p>新闻正文内容足够完整。</p><p><b class='promo'><i>{promotion}</i></b></p>"

    blocks = content_blocks(body)
    assert len(blocks) == 1
    assert blocks[0]["text"] == "新闻正文内容足够完整。"
    assert blocks[0]["segments"][0]["text"] == blocks[0]["text"]


def test_nested_presentation_tags_can_remove_only_one_explicit_line() -> None:
    before = "新闻主体介绍比赛过程和赛后采访，球队还公布了下一轮训练与备战安排。" * 3
    promotion = "在Kayo上观看每一场BBL比赛的直播，比赛期间无广告打断。Kayo新用户？"
    after = "报道还补充了球队下一轮的训练安排以及教练对比赛的复盘意见。" * 3
    body = f"<p>{before}\n<b><i>{promotion}</i></b>\n{after}</p>"

    block = content_blocks(body)[0]
    assert [segment["text"] for segment in block["segments"]] == [before, promotion, after]
    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": "b1.s2",
        "action": "remove_text_line",
        "evidence": promotion,
        "issue_type": "advertisement",
        "reason": "该行是商业推广内容，与新闻事实无关",
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == f"<p>{before}\n{after}</p>"
    assert matches[0]["segment_id"] == "b1.s2"


def test_ai_repair_plan_removes_unknown_program_promotion_line() -> None:
    before = "京都不死鸟已经确认球员离队，俱乐部正在办理后续手续。"
    after = "报道同时回顾了球员本赛季的出场数据和此前的职业经历。"
    context = (
        "<p>文章其余部分详细介绍了转会背景、球队计划以及俱乐部发布的官方信息，"
        "并引用了相关人员对于下一阶段安排的说明。</p>"
    ) * 4
    image = '<p><img src="/fastdfs8/program.jpg" alt="球员资料图" /></p>'
    body = f"<p>{before}\n{PROGRAM_PROMOTION}\n{after}</p>{image}{context}"
    segment = content_blocks(body)[0]["segments"][1]

    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": segment["segment_id"],
        "action": "remove_text_line",
        "evidence": PROGRAM_PROMOTION,
        "issue_type": "standalone_program_promotion",
        "reason": "独立节目推广内容，与新闻事实无关",
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == f"<p>{before}\n{after}</p>{image}{context}"
    assert PROGRAM_PROMOTION not in cleaned
    assert image in cleaned
    assert matches[0] == {
        "rule": "ai_targeted_repair",
        "action": "remove_text_line",
        "block_id": "b1",
        "tag": "p",
        "text": PROGRAM_PROMOTION,
        "validation": "ai_promotion_category",
        "confidence": 0.99,
        "segment_id": "b1.s2",
        "issue_type": "standalone_program_promotion",
        "reason": "独立节目推广内容，与新闻事实无关",
    }


def test_exact_line_plan_does_not_require_issue_type_or_reason() -> None:
    body = (
        f"<p>球队已经确认本轮首发名单。\n{PROGRAM_PROMOTION}\n"
        "赛后报道还将继续更新球员数据。</p>"
        "<p>文章其余部分包含完整比赛过程、球员表现、赛后采访和俱乐部官方说明。</p>"
        "<p>报道还补充了球队本赛季的整体计划以及后续比赛安排。</p>"
    )
    base = {
        "segment_id": "b1.s2",
        "action": "remove_text_line",
        "evidence": PROGRAM_PROMOTION,
        "confidence": 0.99,
    }

    for extra in (
        {},
        {"issue_type": "standalone_program_promotion"},
        {"issue_type": "program_schedule", "reason": "节目相关内容"},
        {"issue_type": "program_promotion", "reason": "这是一段内容"},
    ):
        cleaned, matches, error = apply_repair_plan(body, {**base, **extra})
        assert error is None
        assert PROGRAM_PROMOTION not in cleaned
        assert len(matches) == 1


def test_ai_repair_plan_accepts_explicit_br_line_boundary() -> None:
    before = "球队上半场占据主动并率先取得进球。"
    after = "主教练赛后肯定了全队的防守表现。"
    context_text = (
        "报道还详细回顾了双方在中场的争夺、换人调整和终场前的攻防过程，"
        "并收录了两队主教练在赛后的完整评价。"
    ) * 4
    context = f"<p>{context_text}</p>"
    body = f"<p>{before}<br class='feed'>{VIDEO_TEASER}<BR>{after}</p>{context}"

    segment = content_blocks(body)[0]["segments"][1]
    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": segment["segment_id"],
        "action": "remove_text_line",
        "evidence": VIDEO_TEASER,
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
        "confidence": 0.98,
    })

    assert error is None
    assert matches[0]["segment_id"] == "b1.s2"
    assert cleaned == f"<p>{before}<br class='feed'>{after}</p>{context}"


def test_ai_line_repair_rejects_inline_sentence_without_boundary() -> None:
    body = (
        "<p>球队在第26分钟取得进球，"
        f"{VIDEO_TEASER}，随后对手加强了进攻。</p>"
    )

    assert find_promotional_lines(body) == []
    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": "b1.s1",
        "action": "remove_text_line",
        "evidence": VIDEO_TEASER,
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
        "confidence": 0.99,
    })
    assert cleaned == body
    assert matches == []
    assert error


def test_ai_line_repair_uses_segment_id_when_evidence_is_repeated() -> None:
    body = (
        f"<p>正常新闻前文。\n{VIDEO_TEASER}\n正常新闻后文。</p>"
        f"<p>另一段正常新闻。\n{VIDEO_TEASER}\n报道结束。</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": "b1.s2",
        "action": "remove_text_line",
        "evidence": VIDEO_TEASER,
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == (
        "<p>正常新闻前文。\n正常新闻后文。</p>"
        f"<p>另一段正常新闻。\n{VIDEO_TEASER}\n报道结束。</p>"
    )
    assert matches[0]["segment_id"] == "b1.s2"


def test_ai_line_repair_allows_high_confidence_line_with_sufficient_context() -> None:
    body = (
        f"<p>正常新闻前文介绍了比赛过程和球员表现，球队还公布了后续训练安排。\n"
        f"{VIDEO_TEASER}\n报道随后补充了教练采访、积分情况以及下一轮比赛计划。</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": "b1.s2",
        "action": "remove_text_line",
        "evidence": VIDEO_TEASER,
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
        "confidence": 0.99,
    })

    assert error is None
    assert VIDEO_TEASER not in cleaned
    assert len(matches) == 1


def test_ai_line_repair_operation_alias_allows_high_confidence_line_with_sufficient_context() -> None:
    body = (
        f"<p>新闻前文完整介绍双方比赛过程、球员表现、教练评价以及赛后安排。\n"
        f"{VIDEO_TEASER}\n新闻后文补充了球队下一轮备战计划和俱乐部官方说明。</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "segment_id": "b1.s2",
        "operation": "remove_text_line",
        "evidence": VIDEO_TEASER,
        "issue_type": "video_promotion",
        "reason": VIDEO_REASON,
        "confidence": 0.99,
    })

    assert error is None
    assert VIDEO_TEASER not in cleaned
    assert len(matches) == 1


def test_video_teaser_whole_block_waits_for_ai_line_plan() -> None:
    body = f"<p>{VIDEO_TEASER}</p>"

    assert find_promotional_blocks(body) == []
    assert remove_promotional_blocks(body) == (body, [])


def test_ai_plan_can_remove_explicit_media_marker_block():
    body = (
        '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>'
        '<p>【集锦视频】本场比赛精彩回放</p>'
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": "【集锦视频】本场比赛精彩回放",
        "issue_type": "video_promotion",
        "reason": "该独立集锦视频引流块与新闻事实无关",
        "confidence": 0.98,
    })

    assert error is None
    assert cleaned == '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>'
    assert matches[0]["text"] == "【集锦视频】本场比赛精彩回放"


def test_ai_plan_can_remove_unknown_standalone_promotion_block():
    context = (
        "文章其余部分详细介绍了比赛过程、球员表现、赛后采访和俱乐部官方信息，"
        "并补充了球队本赛季的整体计划以及后续比赛安排。"
    )
    body = (
        '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>'
        f"<p>{PROGRAM_PROMOTION}</p>"
        f"<p>{context}</p><p>{context}</p><p>{context}</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": PROGRAM_PROMOTION,
        "issue_type": "program_promotion",
        "reason": "独立节目推广内容，与新闻事实无关",
        "confidence": 0.98,
    })

    assert error is None
    assert cleaned == (
        '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>'
        f"<p>{context}</p><p>{context}</p><p>{context}</p>"
    )
    assert matches[0]["validation"] == "ai_promotion_category"


def test_real_branded_watch_plan_accepts_ai_reason_referring_to_target_as_this_block():
    context = (
        "报道完整介绍了比赛进程、球队表现、教练赛后评价和接下来的赛事安排，"
        "并引用了俱乐部相关人员对于球队现状的说明。"
    ) * 3
    image = '<img src="/fastdfs8/vasco.jpg" alt="比赛图片" width="1200">'
    body = (
        f"<p>{context}</p>{image}<p>{context}</p>"
        f"<p>{BRANDED_WATCH_PROMOTION}</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b3",
        "action": "remove_block",
        "evidence": BRANDED_WATCH_PROMOTION,
        "issue_type": "media_promotion",
        "reason": (
            "该段以‘观看全部内容’为号召，推广在指定媒体平台观看内容，"
            "与新闻事实无关。"
        ),
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == f"<p>{context}</p>{image}<p>{context}</p>"
    assert image in cleaned
    assert matches[0]["validation"] == "fixed_promotion_rule"


def test_ai_plan_can_remove_new_standalone_watch_promotion_shape():
    promotion = "观看：关于球队的一切尽在 FanZone 频道"
    context = (
        "文章其余内容详细介绍了双方比赛过程、球员表现和教练赛后发言，"
        "并补充了球队下一阶段的训练和比赛计划。"
    )
    body = f"<p>{context}</p><p>{context}</p><p>{promotion}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b3",
        "action": "remove_block",
        "evidence": promotion,
        "issue_type": "channel_promotion",
        "reason": "该段引导读者前往指定频道查看内容，与新闻事实无关",
        "confidence": 0.98,
    })

    assert error is None
    assert cleaned == f"<p>{context}</p><p>{context}</p>"
    assert matches[0]["validation"] == "ai_promotion_category"


def test_ai_plan_accepts_explicit_in_platform_watch_promotion():
    promotion = "请在官方平台观看本轮比赛的完整回放"
    context = (
        "报道详细介绍了双方的比赛过程、球员表现和教练赛后发言，"
        "并补充了球队下一阶段的训练安排。"
    )
    body = f"<p>{context}</p><p>{promotion}</p><p>{context}</p><p>{context}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": promotion,
        "issue_type": "media_promotion",
        "reason": "该段引导读者前往官方平台观看回放，与新闻事实无关",
        "confidence": 0.97,
    })

    assert error is None
    assert cleaned == f"<p>{context}</p><p>{context}</p><p>{context}</p>"
    assert matches[0]["validation"] == "ai_promotion_category"


def test_ai_plan_removes_scoreboard_marker_when_reason_uses_guidance_wording():
    context = (
        "这篇报道完整介绍了球员转会背景、合同安排、球队计划和后续训练，"
        "并引用了俱乐部及相关人员对下一阶段工作的说明。"
    )
    body = (
        f"<p>{context}</p><p>{SCOREBOARD_MARKER}</p>"
        f"<p>{context}</p><p>{context}</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": SCOREBOARD_MARKER,
        "issue_type": "traffic_generation",
        "reason": "该独立积分榜入口用于引导访问赛事榜单，与球员转会新闻事实无关",
        "confidence": 0.99,
    })

    assert error is None
    assert SCOREBOARD_MARKER not in cleaned
    assert matches[0]["validation"] == "fixed_promotion_rule"


def test_exact_scoreboard_block_plan_does_not_require_ai_category():
    body = (
        f"<p>报道完整介绍了球员转会背景、合同安排和球队计划。</p>"
        f"<p>{SCOREBOARD_MARKER}</p>"
        f"<p>报道还补充了俱乐部官方说明和后续训练安排。</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": SCOREBOARD_MARKER,
        "confidence": 0.99,
    })

    assert error is None
    assert SCOREBOARD_MARKER not in cleaned
    assert matches[0]["validation"] == "fixed_promotion_rule"


def test_high_confidence_plan_is_not_rejected_only_for_deletion_ratio():
    context = "这是一段完整的比赛报道，包含比赛过程和赛后采访信息。"
    body = (
        f'<p>{context}</p>'
        f"<p>{PROGRAM_PROMOTION}</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": PROGRAM_PROMOTION,
        "issue_type": "program_promotion",
        "reason": "独立节目推广内容，与新闻事实无关",
        "confidence": 0.98,
    })

    assert error is None
    assert cleaned == '<p>这是一段完整的比赛报道，包含比赛过程和赛后采访信息。</p>'
    assert matches[0]["text"] == PROGRAM_PROMOTION


def test_ai_repair_plan_rejects_more_than_three_operations() -> None:
    context = "报道完整介绍了比赛过程、球员表现、教练采访和后续训练安排。"
    promotions = [
        "点击查看本轮完整赛程",
        "点击查看球队最新消息",
        "点击查看比赛详情",
        "点击查看赛事官网",
    ]
    body = f"<p>{context}</p>" + "".join(f"<p>{item}</p>" for item in promotions)
    blocks = content_blocks(body)
    plan = [
        {
            "block_id": block["block_id"],
            "action": "remove_block",
            "evidence": block["text"],
            "confidence": 0.99,
        }
        for block in blocks[1:]
    ]

    cleaned, matches, error = apply_repair_plan(body, plan)

    assert cleaned == body
    assert matches == []
    assert "最多3个局部操作" in str(error)


def test_ai_repair_plan_rejects_confidence_below_point_ninety_five() -> None:
    context = "报道完整介绍了比赛过程、球员表现、教练采访和后续训练安排。"
    promotion = "点击查看本轮完整赛程"
    body = f"<p>{context}</p><p>{promotion}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": promotion,
        "confidence": 0.94,
    })

    assert cleaned == body
    assert matches == []
    assert "置信度不足" in str(error)


def test_ai_repair_plan_rejects_removed_text_over_absolute_budget() -> None:
    context = "报道完整介绍了比赛过程、球员表现、教练采访和后续训练安排。" * 40
    promotions = [
        "请点击查看" + ("本轮赛事详细信息" * 25) + "完整赛程官网",
        "请点击查看" + ("本轮赛事详细信息" * 25) + "完整赛程官网",
        "请点击查看" + ("本轮赛事详细信息" * 25) + "完整赛程官网",
    ]
    body = f"<p>{context}</p>" + "".join(f"<p>{item}</p>" for item in promotions)
    blocks = content_blocks(body)
    plan = [
        {
            "block_id": block["block_id"],
            "action": "remove_block",
            "evidence": block["text"],
            "issue_type": "advertisement",
            "reason": "该段是独立广告推广内容，与新闻事实无关",
            "confidence": 0.99,
        }
        for block in blocks[1:]
    ]

    cleaned, matches, error = apply_repair_plan(body, plan)

    assert cleaned == body
    assert matches == []
    assert "超过单次上限" in str(error)


def test_ai_repair_plan_rejects_removed_text_over_twenty_five_percent() -> None:
    context = "报道完整介绍了比赛过程、球员表现和赛后采访信息。" * 9
    promotion = "点击查看本轮赛事完整赛程和比赛详情官网" * 4
    body = f"<p>{context}</p><p>{promotion}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": promotion,
        "confidence": 0.99,
    })

    assert cleaned == body
    assert matches == []
    assert "比例超过25%" in str(error)


def test_exact_high_confidence_block_plan_does_not_require_allowed_issue_type():
    evidence = "球队本轮取得胜利，教练引导球员保持阵型并继续施压，最终赢得比赛。"
    body = f"<p>{evidence}</p><p>报道还补充了赛后采访和球队后续训练安排。</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b1",
        "action": "remove_block",
        "evidence": evidence,
        "issue_type": "traffic_generation",
        "reason": "该段含有推广引流信息，与新闻事实无关",
        "confidence": 0.99,
    })

    assert error is None
    assert evidence not in cleaned
    assert matches[0]["validation"] == "ai_exact_target"


def test_exact_high_confidence_block_plan_does_not_require_promotion_shape():
    evidence = "在 ge 体育场，球迷观看了比赛，随后为球队的获胜欢呼。"
    context = "报道还完整回顾了比赛过程、关键进球以及主教练在赛后的采访。"
    body = f"<p>{evidence}</p><p>{context}</p><p>{context}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b1",
        "action": "remove_block",
        "evidence": evidence,
        "issue_type": "media_promotion",
        "reason": "该段是媒体推广内容，与新闻事实无关",
        "confidence": 0.99,
    })

    assert error is None
    assert evidence not in cleaned
    assert matches[0]["validation"] == "ai_exact_target"


def test_exact_high_confidence_plans_do_not_depend_on_wording_shape():
    context = "报道还完整回顾了比赛过程、关键进球以及主教练在赛后的采访。"
    for evidence in (
        "观看 Globo 播出的比赛后，主教练分析了球队本轮的防守表现。",
        "广告赞助了本场公益比赛，相关收入将用于当地青训建设。",
        "查看VAR回放后，裁判取消了这粒进球。",
        "查看比赛视频后，教练认为球队防守仍需改进。",
        "关注本场比赛的媒体记者已经抵达球场。",
        "进入视频裁判复核环节后，主裁判改判点球。",
        "请查看视频回放，裁判随后确认进球有效。",
        "立即观看回放的裁判最终取消进球。",
        "关注官方平台伤病通报的球迷发现，主力前锋仍未恢复训练。",
        "打开俱乐部官方平台的报名名单后，记者发现两名新援已经入选。",
        "关注俱乐部频道报道的记者透露，球队本周将进行封闭训练。",
        "打开官方网站公示名单后，主教练确认这名球员没有报名。",
        "进入官方平台工作的前球员表示，俱乐部管理已经有所改善。",
        "访问俱乐部网站时，记者发现球队已经更新了球员资料。",
        "查看官方平台发布的伤病公告后，球迷得知门将将缺阵三周。",
    ):
        body = f"<p>{evidence}</p><p>{context}</p><p>{context}</p>"
        cleaned, matches, error = apply_repair_plan(body, {
            "block_id": "b1",
            "action": "remove_block",
            "evidence": evidence,
            "issue_type": "media_promotion",
            "reason": "该段是媒体推广内容，与新闻事实无关",
            "confidence": 0.99,
        })

        assert error is None
        assert evidence not in cleaned
        assert matches[0]["validation"] == "ai_exact_target"


def test_ai_repair_plan_requires_allowlisted_tail_promotion_and_exact_evidence() -> None:
    body = (
        '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>'
        '<img src="/story.jpg" alt="比赛图片">'
        '<p>点击这里关注 WhatsApp 频道，获取最新消息</p>'
        '<p>观看 ge、Globo 和 sportv 上的全部内容</p>'
    )
    blocks = content_blocks(body)
    plan = [
        {
            "block_id": blocks[1]["block_id"],
            "action": "remove_block",
            "evidence": blocks[1]["text"],
            "confidence": 0.97,
        },
        {
            "block_id": blocks[2]["block_id"],
            "action": "remove_block",
            "evidence": blocks[2]["text"],
            "confidence": 0.96,
        },
    ]

    cleaned, matches, error = apply_repair_plan(body, plan)

    assert error is None
    assert cleaned == '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p><img src="/story.jpg" alt="比赛图片">'
    assert [item["block_id"] for item in matches] == ["b2", "b3"]


def test_ai_repair_plan_accepts_exact_target_without_type_but_rejects_untrusted_plan() -> None:
    body = '<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>'
    block = content_blocks(body)[0]

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": block["block_id"],
        "action": "remove_block",
        "evidence": block["text"],
        "confidence": 0.99,
    })
    assert error is None
    assert cleaned == ""
    assert matches[0]["validation"] == "ai_exact_target"

    for plan in (
        {
            "block_id": block["block_id"],
            "action": "remove_block",
            "evidence": "其他文本",
            "confidence": 0.99,
        },
        {
            "block_id": block["block_id"],
            "action": "remove_block",
            "evidence": block["text"],
            "confidence": 0.5,
        },
    ):
        cleaned, matches, error = apply_repair_plan(body, plan)
        assert cleaned == body
        assert matches == []
        assert error


def test_ai_repair_plan_uses_block_id_when_text_occurs_more_than_once() -> None:
    promotion = "点击这里关注 WhatsApp 频道，获取最新消息"
    body = (
        f"<p>{promotion}</p>"
        "<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>"
        f"<p>{promotion}</p>"
    )
    blocks = content_blocks(body)

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": blocks[0]["block_id"],
        "action": "remove_block",
        "evidence": promotion,
        "confidence": 0.99,
    })

    assert error is None
    assert cleaned == (
        "<p>这是一段完整的比赛报道，包含比赛过程、球员表现和赛后采访信息。</p>"
        f"<p>{promotion}</p>"
    )
    assert matches[0]["block_id"] == blocks[0]["block_id"]


def test_duplicate_content_plan_keeps_first_block_and_removes_selected_copy() -> None:
    repeated = "费内巴切已确认，格林伍德和贡多齐正接受欧足联的纪律调查。"
    body = (
        f"<p>{repeated}</p>"
        "<p>俱乐部还说明了案件背景以及下一阶段的处理安排。</p>"
        f"<p>{repeated}</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b3",
        "keep_block_id": "b1",
        "action": "remove_block",
        "evidence": repeated,
        "issue_type": "duplicate_content",
        "reason": "b3 与 b1 完全重复，保留首次出现的段落",
        "confidence": 0.97,
    })

    assert error is None
    assert cleaned.count(repeated) == 1
    assert cleaned.startswith(f"<p>{repeated}</p>")
    assert matches[0]["keep_block_id"] == "b1"
    assert matches[0]["validation"] == "ai_general_issue"


def test_duplicate_content_plan_requires_matching_keep_block() -> None:
    repeated = "这段比赛报道被错误重复了一次。"
    body = f"<p>{repeated}</p><p>{repeated}</p><p>后续报道内容完整。</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "keep_block_id": "b3",
        "action": "remove_block",
        "evidence": repeated,
        "issue_type": "duplicate_content",
        "reason": "删除后一次重复内容",
        "confidence": 0.98,
    })

    assert cleaned == body
    assert matches == []
    assert "保留正文块" in str(error)


def test_duplicate_content_plan_cannot_delete_each_others_keep_blocks() -> None:
    repeated = "这段比赛报道被错误重复了一次。"
    body = f"<p>{repeated}</p><p>{repeated}</p><p>后续报道内容完整。</p>"

    cleaned, matches, error = apply_repair_plan(body, [
        {
            "block_id": "b1",
            "keep_block_id": "b2",
            "action": "remove_block",
            "evidence": repeated,
            "issue_type": "duplicate_content",
            "reason": "删除其中一个重复段落",
            "confidence": 0.98,
        },
        {
            "block_id": "b2",
            "keep_block_id": "b1",
            "action": "remove_block",
            "evidence": repeated,
            "issue_type": "duplicate_content",
            "reason": "删除另一个重复段落",
            "confidence": 0.98,
        },
    ])

    assert cleaned == body
    assert matches == []
    assert "保留目标" in str(error)


def test_general_extraneous_content_plan_removes_exact_plain_text_block() -> None:
    extra = "【图片】三井寺眞崇拜的两位世界级球员"
    body = (
        "<p>报道介绍了球员本轮比赛的完整表现和赛后采访，并补充了比赛背景及后续安排。</p>" * 3
        + f"<p>{extra}</p>"
        + "<p>球队将在下周继续备战下一轮联赛，并依据教练组安排进行训练。</p>" * 3
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b4",
        "action": "remove_block",
        "evidence": extra,
        "issue_type": "extraneous_content",
        "reason": "该图片入口是采集残留，与正文新闻事实无关",
        "confidence": 0.95,
    })

    assert error is None
    assert extra not in cleaned
    assert matches[0]["validation"] == "ai_general_issue"


@pytest.mark.parametrize("prefix", ["【官方】", "【伤停】", "【赛果】", "[Official]"])
def test_exact_plan_can_delete_labeled_block_without_type_allowlist(
    prefix: str,
) -> None:
    evidence = f"{prefix}俱乐部宣布张伟将在9月10日续约至2028年。"
    body = f"<p>{evidence}</p><p>球队随后公布了下一阶段的训练安排。</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b1",
        "action": "remove_block",
        "evidence": evidence,
        "issue_type": "extraneous_content",
        "reason": "模型声称这是采集残留入口，与正文无关",
        "confidence": 0.99,
    })

    assert error is None
    assert evidence not in cleaned
    assert matches[0]["validation"] == "ai_exact_target"


def test_structural_label_with_cta_and_artifact_reason_can_be_removed() -> None:
    evidence = "【赛程】查看2026/27赛季完整赛程"
    body = (
        f"<p>新赛季揭幕战将在十月进行，球队已经公布了备战安排和比赛背景。</p>" * 3 +
        f"<p>{evidence}</p>"
    )

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b4",
        "action": "remove_block",
        "evidence": evidence,
        "issue_type": "extraneous_content",
        "reason": "该赛程入口是采集残留，与正文新闻事实无关",
        "confidence": 0.95,
    })

    assert error is None
    assert evidence not in cleaned
    assert matches[0]["validation"] == "ai_general_issue"


@pytest.mark.parametrize(
    "evidence",
    [
        "【赛程】新赛季揭幕战将在10月5日举行。",
        "【直播】主教练赛后表示球队发挥出色。",
    ],
)
def test_exact_plan_can_delete_structural_label_without_cta(
    evidence: str,
) -> None:
    body = f"<p>{evidence}</p><p>报道还介绍了球队的备战情况。</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b1",
        "action": "remove_block",
        "evidence": evidence,
        "issue_type": "extraneous_content",
        "reason": "模型声称这是采集残留入口，与正文无关",
        "confidence": 0.99,
    })

    assert error is None
    assert evidence not in cleaned
    assert matches[0]["validation"] == "ai_exact_target"


def test_exact_plan_removes_unrelated_customer_service_block() -> None:
    news = (
        "在3-0战胜沙尔克开局后，奥格斯堡又在客场4-1大胜法兰克福，"
        "并在队史首次登上积分榜榜首。"
    )
    customer_service = (
        "在我们的常见问题中，你可以找到许多关于使用 kicker+ 以及排查问题的实用提示和解答。"
        "如果你仍有疑问，请通过 +(0) 911 477 911 11 联系我们的客户服务，"
        "或发送电子邮件至 service@kicker.de。"
    )
    body = f"<p>{news}</p>\n<p>{customer_service}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "remove_block",
        "evidence": customer_service,
        "issue_type": "extraneous_content",
        "reason": "该段与比赛报道无关，删除不会影响新闻事实。",
        "confidence": 0.98,
    })

    assert error is None
    assert cleaned == f"<p>{news}</p>\n"
    assert matches[0]["validation"] == "ai_exact_target"


def test_minor_text_defect_replaces_only_the_exact_plain_text_block() -> None:
    before = "球队将在下周出战。。"
    after = "球队将在下周出战。"
    body = f"<p>主教练确认了下一阶段的训练安排。</p><p>{before}</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b2",
        "action": "replace_text",
        "evidence": before,
        "after": after,
        "issue_type": "minor_text_defect",
        "reason": "目标块末尾存在重复标点，只修正该标点",
        "confidence": 0.95,
    })

    assert error is None
    assert cleaned == f"<p>主教练确认了下一阶段的训练安排。</p><p>{after}</p>"
    assert matches[0]["after"] == after


def test_minor_text_defect_cannot_change_names_dates_or_contract_terms() -> None:
    evidence = "张伟将在9月10日与俱乐部续约至2028年。"
    body = f"<p>{evidence}</p><p>球队随后公布了训练安排。</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b1",
        "action": "replace_text",
        "evidence": evidence,
        "after": "李明将在10月20日与俱乐部续约至2030年。",
        "issue_type": "minor_text_defect",
        "reason": "模型声称只做轻微修正",
        "confidence": 0.99,
    })

    assert cleaned == body
    assert matches == []
    assert "轻微局部修复" in str(error)


@pytest.mark.parametrize(
    ("evidence", "replacement"),
    [
        ("俱乐部支付了1.5亿欧元转会费。", "俱乐部支付了15亿欧元转会费。"),
        ("球队最终以2-1赢得比赛。", "球队最终以21赢得比赛。"),
        ("该项数据修正值为-1。", "该项数据修正值为1。"),
        ("该项数据修正值为−1。", "该项数据修正值为1。"),
        ("球队控球率达到50%。", "球队控球率达到50。"),
        ("俱乐部支付了$100。", "俱乐部支付了¥100。"),
        ("报名人数不得超过≤20人。", "报名人数不得超过20人。"),
        ("本期账面调整为(100)万元。", "本期账面调整为100万元。"),
    ],
)
def test_minor_text_defect_cannot_change_numeric_expressions(
    evidence: str,
    replacement: str,
) -> None:
    body = f"<p>{evidence}</p><p>报道还介绍了比赛背景。</p>"

    cleaned, matches, error = apply_repair_plan(body, {
        "block_id": "b1",
        "action": "replace_text",
        "evidence": evidence,
        "after": replacement,
        "issue_type": "minor_text_defect",
        "reason": "模型声称只做轻微修正",
        "confidence": 0.99,
    })

    assert cleaned == body
    assert matches == []
    assert "轻微局部修复" in str(error)


def test_removes_only_high_confidence_standalone_plain_text_block() -> None:
    body = (
        '<p class="lead">球队在下半场完成逆转。</p>'
        '<p data-source="feed">立即点击官网查看完整赛程</p>'
        '<p>主教练赛后接受了采访。</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == (
        '<p class="lead">球队在下半场完成逆转。</p>'
        '<p>主教练赛后接受了采访。</p>'
    )
    assert [(item["tag"], item["rule"], item["text"]) for item in matches] == [
        ("p", "standalone_call_to_action", "立即点击官网查看完整赛程")
    ]


def test_matches_real_aleagues_full_schedule_prompt() -> None:
    body = (
        '<p>新赛季揭幕战将在十月进行。</p>'
        '<p>点击查看2026/27赛季五十铃UTE澳超完整赛程</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == '<p>新赛季揭幕战将在十月进行。</p>'
    assert len(matches) == 1
    assert matches[0]["text"] == "点击查看2026/27赛季五十铃UTE澳超完整赛程"
    assert matches[0]["rule"] == "standalone_call_to_action"


def test_removes_all_high_confidence_blocks_in_one_pass() -> None:
    body = (
        '<p>正文第一段，介绍比赛过程。</p>'
        '<P>扫码关注获取详情</P>'
        '<div class="promotion">更多内容请点击查看</div>'
        '<p>正文最后一段，介绍赛后采访。</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == (
        '<p>正文第一段，介绍比赛过程。</p>'
        '<p>正文最后一段，介绍赛后采访。</p>'
    )
    assert len(matches) == 2
    assert [item["start"] for item in matches] == sorted(item["start"] for item in matches)


def test_images_and_surrounding_markup_are_byte_for_byte_unchanged() -> None:
    image = '<figure><img src="/fastdfs8/one.jpg" alt="赛场图" width="1200"></figure>'
    body = (
        '<h2>比赛战报</h2>'
        f"{image}"
        '<p>球队凭借终场前的进球取胜。</p>'
        '<p>立即下载官网链接获取详情</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == (
        '<h2>比赛战报</h2>'
        f"{image}"
        '<p>球队凭借终场前的进球取胜。</p>'
    )
    assert cleaned.count(image) == 1
    assert len(matches) == 1


def test_does_not_remove_non_standalone_or_rich_text_content() -> None:
    body = (
        '<p>主教练表示，点击查看回放能够帮助球员复盘比赛。</p>'
        '<p>点击查看回放能够帮助球员复盘比赛。</p>'
        '<p><strong>立即点击官网查看完整赛程</strong></p>'
        '<p><img src="/qr.jpg" alt="扫码关注获取详情"></p>'
    )

    assert remove_promotional_blocks(body) == (body, [])


def test_does_not_modify_promotion_like_markup_inside_comments_or_raw_elements() -> None:
    body = (
        '<!-- template: <p>点击查看完整赛程</p> -->'
        '<script>const card = "<p>点击查看完整赛程</p>";</script>'
        '<template><p>商务咨询：editor@example.com</p></template>'
        '<p>这是文章中真实存在且需要保留的正常正文。</p>'
    )

    assert remove_promotional_blocks(body) == (body, [])


def test_does_not_normalize_photo_marker_inside_comment_or_script() -> None:
    body = (
        '<!-- <p>球员庆祝 [照片]=Getty Images</p> -->'
        '<script>const caption = "<p>球员庆祝 [照片]=Getty Images</p>";</script>'
    )

    assert normalize_photo_credits(body) == (body, [])


def test_unmatched_body_is_byte_for_byte_unchanged() -> None:
    body = (
        " \n<!-- 点击官网查看详情 -->"
        '<ARTICLE><p class=\'lead\'>更多比赛信息将在赛后公布&nbsp;更新。</p>'
        '<img src="/one.jpg"></ARTICLE>\n '
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == body
    assert matches == []
    assert find_promotional_blocks(body) == []


def test_cleanup_is_idempotent_and_reports_matches_only_once() -> None:
    body = '<p>正文内容足够完整。</p><p>点击官网查看详情</p>'

    first_body, first_matches = remove_promotional_blocks(body)
    second_body, second_matches = remove_promotional_blocks(first_body)

    assert first_body == '<p>正文内容足够完整。</p>'
    assert len(first_matches) == 1
    assert second_body == first_body
    assert second_matches == []


def test_normalizes_getty_photo_credit_and_preserves_caption() -> None:
    body = '<p class="caption">球员在终场哨响后向球迷致意 [照片]=Getty Images</p>'

    normalized, matches = normalize_photo_credits(body)

    assert normalized == (
        '<p class="caption">球员在终场哨响后向球迷致意'
        '（图片来源：Getty Images）</p>'
    )
    assert matches == [{
        "rule": "photo_credit_marker",
        "tag": "p",
        "caption": "球员在终场哨响后向球迷致意",
        "source": "Getty Images",
        "before": "球员在终场哨响后向球迷致意 [照片]=Getty Images",
        "after": "球员在终场哨响后向球迷致意（图片来源：Getty Images）",
    }]


def test_normalizes_fullwidth_equals_without_losing_semantic_parentheses() -> None:
    caption = "悉尼队球员在主场庆祝进球（比赛第88分钟）"
    body = f"<div>{caption} [照片]＝Getty Images</div>"

    normalized, matches = normalize_photo_credits(body)

    assert normalized == f"<div>{caption}（图片来源：Getty Images）</div>"
    assert matches[0]["caption"] == caption
    assert matches[0]["source"] == "Getty Images"


def test_normalizes_photographer_credit_and_removes_feed_separator() -> None:
    body = '<p>球员在赛后向看台致意。/ 摄影：中地拓也</p>'

    normalized, matches = normalize_photo_credits(body)

    assert normalized == '<p>球员在赛后向看台致意。（图片来源：中地拓也）</p>'
    assert matches[0]["rule"] == "photographer_credit_marker"
    assert matches[0]["before"] == "球员在赛后向看台致意。/ 摄影：中地拓也"
    assert matches[0]["after"] == "球员在赛后向看台致意。（图片来源：中地拓也）"


def test_plain_photo_label_without_credit_assignment_is_unchanged() -> None:
    body = (
        '<p>球队赛前训练画面 [照片]</p>'
        '<p>Getty Images [照片] 提供了比赛资料图</p>'
    )

    assert normalize_photo_credits(body) == (body, [])


def test_photo_credit_normalization_preserves_image_count_sources_and_order() -> None:
    first_image = '<img src="/fastdfs8/first.jpg" alt="首图" width="1200">'
    second_image = '<IMG loading="lazy" SRC="https://img.example/second.jpg">'
    body = (
        f'<figure>{first_image}</figure>'
        '<p>两队球员在赛后相互致意 [照片]=Getty Images</p>'
        f'<figure class="second">{second_image}</figure>'
    )
    before = body_safety_stats(body)

    normalized, matches = normalize_photo_credits(body)
    after = body_safety_stats(normalized)

    assert len(matches) == 1
    assert first_image in normalized
    assert second_image in normalized
    assert before["image_count"] == after["image_count"] == 2
    assert before["image_sources"] == after["image_sources"] == [
        "/fastdfs8/first.jpg",
        "https://img.example/second.jpg",
    ]
    assert normalized.index(first_image) < normalized.index(second_image)


def test_tail_only_promotions_match_only_at_article_end() -> None:
    cooperation = '<p>商务咨询：editor@example.com</p>'
    trailing = (
        '<article><p>正文介绍了球队备战和新赛季安排。</p>'
        f'{cooperation}<!-- feed end --></article>'
    )
    middle = (
        '<article><p>正文介绍了球队备战。</p>'
        f'{cooperation}<p>记者随后补充了比赛信息。</p></article>'
    )

    trailing_cleaned, trailing_matches = remove_promotional_blocks(trailing)

    assert trailing_cleaned == (
        '<article><p>正文介绍了球队备战和新赛季安排。</p>'
        '<!-- feed end --></article>'
    )
    assert [item["rule"] for item in trailing_matches] == [
        "standalone_cooperation_contact"
    ]
    assert remove_promotional_blocks(middle) == (middle, [])


def test_feed_markers_and_more_news_prompts_are_only_removed_at_tail() -> None:
    middle = (
        "<article><p>正文介绍了球队备战。</p><p>更多球队新闻</p>"
        "<p>记者随后补充了比赛信息。</p></article>"
    )
    tail = (
        "<article><p>正文介绍了球队备战和新赛季安排。</p>"
        "<p>前文</p><p>更多球队新闻</p></article>"
    )

    assert remove_promotional_blocks(middle) == (middle, [])
    cleaned, matches = remove_promotional_blocks(tail)
    assert cleaned == "<article><p>正文介绍了球队备战和新赛季安排。</p></article>"
    assert [item["rule"] for item in matches] == [
        "standalone_feed_marker",
        "standalone_more_news_cta",
    ]


def test_tail_media_call_to_action_allows_only_comments_and_container_closers_after_it() -> None:
    prompt = '<p>点击收听完整播客</p>'
    at_tail = f'<section><p>节目回顾了本轮比赛。</p>{prompt}\n<!-- end --></section>'
    not_at_tail = f'<section>{prompt}<p>节目还采访了主教练。</p></section>'

    cleaned, matches = remove_promotional_blocks(at_tail)

    assert cleaned == '<section><p>节目回顾了本轮比赛。</p>\n<!-- end --></section>'
    assert [item["rule"] for item in matches] == ["standalone_media_call_to_action"]
    assert remove_promotional_blocks(not_at_tail) == (not_at_tail, [])


def test_removes_known_ge_podcast_and_watch_prompts_only_at_tail() -> None:
    body = (
        '<article><p>正文介绍了球队最新备战情况。</p>'
        '<p>🎧 收听 ge 帕尔梅拉斯播客 🎧</p>'
        '<p>+ 在 Globo、sportv 和 ge 收看帕尔梅拉斯的全部内容</p>'
        '</article>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == '<article><p>正文介绍了球队最新备战情况。</p></article>'
    assert [item["rule"] for item in matches] == [
        "standalone_branded_podcast_prompt",
        "standalone_branded_watch_prompt",
    ]


def test_does_not_remove_branded_watch_sentence_from_middle_of_article() -> None:
    body = (
        '<p>观看：在ge、Globo和sportv了解关于弗拉门戈的一切</p>'
        '<p>随后，主教练公布了本轮比赛的首发阵容。</p>'
    )

    assert remove_promotional_blocks(body) == (body, [])
