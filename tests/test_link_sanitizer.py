from __future__ import annotations

from app.services.link_sanitizer import (
    find_media_artifact_lines,
    preprocess_quality_body,
    remove_clickable_links,
    remove_media_artifact_lines,
)


def test_removes_linked_text_nested_markup_and_keeps_surrounding_body():
    body = '<p>正文前</p><a href="https://example.com"><strong>相关阅读</strong></a><p>正文后</p>'

    assert remove_clickable_links(body) == "<p>正文前</p><p>正文后</p>"


def test_removes_link_wrapper_but_preserves_linked_image():
    body = '<p><a href="https://example.com"><img src="/body.jpg" alt="正文图"></a></p>'

    assert remove_clickable_links(body) == '<p><img src="/body.jpg" alt="正文图"></p>'


def test_is_idempotent_and_keeps_non_link_html_byte_for_byte():
    body = '<ARTICLE><p class="lead">正文</p><img src="/one.jpg"></ARTICLE>'

    assert remove_clickable_links(remove_clickable_links(body)) == body


def test_removes_case_insensitive_anchor_tags():
    assert remove_clickable_links('<P><A HREF="https://example.com">推荐</A></P>') == "<P></p>"


def test_quality_preprocess_removes_empty_container_left_by_text_link():
    body = '<p>正文内容足够完整，包含比赛过程和赛后采访。</p><p><a href="https://example.com">阅读全文</a></p>'

    assert preprocess_quality_body(body) == '<p>正文内容足够完整，包含比赛过程和赛后采访。</p>'


def test_quality_preprocess_keeps_image_only_container_after_link_cleanup():
    body = '<p>正文内容足够完整，包含比赛过程和赛后采访。</p><p><a href="https://example.com"><img src="/cover.jpg" alt="比赛图"></a></p>'

    assert preprocess_quality_body(body) == '<p>正文内容足够完整，包含比赛过程和赛后采访。</p><p><img src="/cover.jpg" alt="比赛图"></p>'


def test_removes_inline_linked_entity_text():
    body = '<p>据<a href="https://example.com/sydney">悉尼FC官方</a>消息，球队已完成签约。</p>'

    assert remove_clickable_links(body) == "<p>据消息，球队已完成签约。</p>"


def test_unclosed_anchor_is_unwrapped_without_losing_remaining_article_text():
    body = '<p>正文</p><A HREF="https://example.com">链接后仍是正文<p>末段</p>'

    cleaned = remove_clickable_links(body)

    assert "href=" not in cleaned.lower()
    assert "链接后仍是正文" in cleaned
    assert "末段" in cleaned


def test_incomplete_anchor_start_tag_is_removed():
    cleaned = remove_clickable_links('<p>正文</p><a href="https://example.com"')

    assert "href=" not in cleaned.lower()
    assert "<a" not in cleaned.lower()


def test_quality_preprocess_drops_embeds_and_scripts_but_keeps_images():
    body = (
        '<ARTICLE><p>正文内容足够完整。</p>'
        '<iframe src="https://video.example/player"></iframe>'
        '<script>alert(1)</script>'
        '<p><img src="/story.jpg" alt="比赛图" width="1200"></p>'
        '</ARTICLE>'
    )

    cleaned = preprocess_quality_body(body)

    assert '<iframe' not in cleaned.lower()
    assert '<script' not in cleaned.lower()
    assert '<img src="/story.jpg" alt="比赛图" width="1200">' in cleaned
    assert '<ARTICLE>' in cleaned and '</ARTICLE>' in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_removes_clickable_attributes_and_feed_markers():
    body = (
        '<p onclick="location.href=\'https://bad.example\'" data-href="/x">正文内容足够完整。</p>'
        '<!-- google_ad_section_start -->广告<!-- google_ad_section_end -->'
        '[推荐](https://bad.example/news)'
    )

    cleaned = preprocess_quality_body(body)

    assert 'onclick=' not in cleaned.lower()
    assert 'data-href=' not in cleaned.lower()
    assert 'google_ad_section' not in cleaned.lower()
    assert '[推荐]' not in cleaned


def test_quality_preprocess_removes_empty_transfer_marker_after_link_cleanup():
    body = (
        '<p>正文内容足够完整。</p>'
        '<p><strong>转会中心：</strong> <a href="https://example.com/transfers">查看详情</a></p>'
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == '<p>正文内容足够完整。</p>'


def test_quality_preprocess_removes_standalone_feed_marker_blocks():
    body = "<p>正文内容足够完整。</p><p>前文</p><p>正文</p><p>相关SSI（正文中）</p>"

    assert preprocess_quality_body(body) == '<p>正文内容足够完整。</p>'


def test_quality_preprocess_removes_markdown_reference_links_and_definitions():
    body = (
        "<p>正文内容足够完整。</p>"
        "<p>[相关阅读][source] 以及 [官网](https://example.com "
        "\"打开官网\")</p>\n"
        "[source]: https://example.com/news \"新闻来源\""
    )

    cleaned = preprocess_quality_body(body)

    assert "[相关阅读]" not in cleaned
    assert "[官网]" not in cleaned
    assert "[source]:" not in cleaned


def test_quality_preprocess_keeps_non_link_markdown_like_text():
    body = "<p>正文内容足够完整，正常[在比赛中](第10分钟)继续进攻。</p>"

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_keeps_undefined_markdown_reference_text():
    body = "<p>正文内容足够完整，参见[注释][x]中的比赛时间。</p>"

    assert preprocess_quality_body(body) == body


def test_sponichi_template_markers_are_removed_without_dropping_news_text():
    body = (
        "<p>google_ad_section_start(name=s1)\n导语\n"
        "主帅在赛前介绍了球队状态。 前文链接\n相关SSI(正文中)</p>"
        "<p><img src=\"/story.jpg\"></p>"
        "<p>正文\n球队将在周末出战。 google_ad_section_end(name=s1)</p>"
    )

    cleaned = preprocess_quality_body(body, source="sponichi")

    assert "google_ad_section" not in cleaned
    assert "前文链接" not in cleaned
    assert "相关SSI" not in cleaned
    assert "导语" not in cleaned
    assert "<p>正文" not in cleaned
    assert "主帅在赛前介绍了球队状态。" in cleaned
    assert "球队将在周末出战。" in cleaned
    assert '<img src="/story.jpg">' in cleaned
    assert preprocess_quality_body(cleaned, source="sponichi") == cleaned


def test_sponichi_template_cleanup_does_not_apply_to_other_sources():
    # The sponichi-specific labels (``正文`` line marker, ``前文链接`` …) stay
    # byte-for-byte for other sources, but the Google ad-section token is
    # unambiguous template residue and is stripped for every source while the
    # surrounding reporting is preserved.
    body = "<p>正文\n主帅介绍了球队状态。 google_ad_section_end(name=s1)</p>"

    cleaned = preprocess_quality_body(body, source="foxsprt")

    assert "google_ad_section" not in cleaned
    assert "主帅介绍了球队状态。" in cleaned
    assert "<p>正文\n主帅介绍了球队状态。" in cleaned
    assert preprocess_quality_body(cleaned, source="foxsprt") == cleaned


def test_google_ad_section_chinese_variant_removed_for_any_source():
    # After machine translation the boundary appears as ``google广告分区开始/结束``
    # glued to the start and end of a reporting paragraph.  It must be removed
    # for every source without dropping the news text or the image node.
    body = (
        "<p>google广告分区开始(name=s1) \n\n 明天开赛，主队将全力争胜。"
        " google广告分区结束(name=s1)</p>"
        "<p><img src=\"/match.jpg\"></p>"
    )

    cleaned = preprocess_quality_body(body, source="sponichi")

    assert "google广告分区" not in cleaned
    assert "明天开赛，主队将全力争胜。" in cleaned
    assert '<img src="/match.jpg">' in cleaned
    assert preprocess_quality_body(cleaned, source="sponichi") == cleaned

    other = preprocess_quality_body(body, source="foxsprt")
    assert "google广告分区" not in other
    assert "明天开赛，主队将全力争胜。" in other


def test_jp_preamble_residue_removed_for_any_source():
    # ``前文`` glued to the paragraph start and ``前文リンク`` glued to its end are
    # machine-translated Japanese feed template residue; both must be stripped
    # while the news sentence and image node stay intact, for every source.
    body = (
        "<p>前文 神户14日通过俱乐部官网宣布，18岁的中场濑口大翔将租借加盟"
        "斯洛伐克的FC科希策。 前文リンク</p>"
        "<p><img src=\"/loan.jpg\"></p>"
    )

    cleaned = preprocess_quality_body(body, source="sponichi")

    assert "前文" not in cleaned
    assert "リンク" not in cleaned
    assert "神户14日通过俱乐部官网宣布" in cleaned
    assert '<img src="/loan.jpg">' in cleaned
    assert preprocess_quality_body(cleaned, source="sponichi") == cleaned

    other = preprocess_quality_body(body, source="yahoojp")
    assert "前文" not in other
    assert "神户14日通过俱乐部官网宣布" in other


def test_jp_preamble_residue_keeps_mid_sentence_word():
    # A genuine ``前文`` appearing mid-sentence (e.g. 正如前文所述) must never be
    # stripped: it is ordinary reporting, not a template marker.
    body = "<p>神户宣布，正如前文所述，濑口大翔将租借加盟科希策。</p>"
    assert preprocess_quality_body(body, source="sponichi") == body


def test_recommendation_tail_headings_removed_after_anchor():
    # ``<h2>更多新闻</h2>`` anchors a trailing run of recommendation headings;
    # the anchor and every heading after it are stripped, while the real article
    # paragraphs before it stay intact.
    body = (
        "<p>德国足协表彰了这两位天才球员，肯定他们出色的表现。</p>"
        "<p>德国足协表示，这些获奖者赢得了广泛认可。</p>"
        "<h2>更多新闻</h2>"
        "<h2>拜仁发布啤酒节球衣</h2>"
        "<h2>奥蓬达伤情令人担忧</h2>"
    )
    cleaned = preprocess_quality_body(body, source="sport1")
    assert "更多新闻" not in cleaned
    assert "啤酒节" not in cleaned
    assert "奥蓬达" not in cleaned
    assert "德国足协表彰了这两位天才球员" in cleaned
    assert "赢得了广泛认可" in cleaned


def test_recommendation_headings_stop_at_trailing_paragraph():
    # Only the consecutive headings after the anchor are removed; a genuine
    # paragraph that follows the recommendation list is preserved.
    body = (
        "<p>汉堡主帅表示球队非常团结，会继续努力。</p>"
        "<h2>更多新闻</h2>"
        "<h2>汉堡正处在绝对危机氛围中</h2>"
        "<p>下个周末，汉堡将在主场迎战科隆。</p>"
    )
    cleaned = preprocess_quality_body(body, source="sport1")
    assert "更多新闻" not in cleaned
    assert "绝对危机氛围" not in cleaned
    assert "汉堡主帅表示球队非常团结" in cleaned
    assert "下个周末，汉堡将在主场迎战科隆。" in cleaned


def test_recommendation_keeps_real_subheading_before_anchor():
    # A genuine in-article ``<h2>`` subheading before the anchor must survive.
    body = (
        "<h2>汉堡主帅态度强硬：“我们非常团结”</h2>"
        "<p>主帅在发布会上强调了球队的凝聚力。</p>"
        "<h2>更多新闻</h2>"
        "<h2>拜仁对这位老熟人感到惊讶</h2>"
    )
    cleaned = preprocess_quality_body(body, source="sport1")
    assert "汉堡主帅态度强硬" in cleaned
    assert "更多新闻" not in cleaned
    assert "老熟人" not in cleaned


def test_subscription_and_paywall_promo_blocks_removed():
    # Newsletter-subscription and paywall prompt paragraphs are dropped whole.
    for promo in (
        "<p>订阅足球简报，随时掌握动态！所有进球和新闻直达你的邮箱</p>",
        "<p>继续阅读需订阅</p>",
        "<p>订阅后继续阅读</p>",
        "<p>选择适合你的订阅，解锁独家内容，畅享无间断阅读体验。</p>",
        "<p>你已经订阅了吗？登录并阅读</p>",
    ):
        body = "<p>正文段落，包含完整比赛信息与赛后采访内容。</p>" + promo
        cleaned = preprocess_quality_body(body, source="sport1")
        assert "订阅" not in cleaned, promo
        assert "正文段落，包含完整比赛信息与赛后采访内容。" in cleaned


def test_subscription_promo_keeps_factual_watch_sentence():
    # A reporting sentence that merely mentions a paid platform to watch a match
    # is legitimate content and must never be removed.
    body = (
        "<p>巴萨球迷可通过Barça Play观看这场比赛。需要订阅Culers Premium "
        "Membership，会员和球迷组织成员可免费使用，其他用户年费为39.99欧元。</p>"
    )
    assert preprocess_quality_body(body, source="marca") == body


def test_quality_preprocess_removes_standalone_caption_and_byline_blocks():
    body = (
        "<p>正文内容足够完整，包含比赛过程和赛后采访。</p>"
        "<p>【图片】“感觉很强”大阪钢巴新援卡马拉！</p>"
        '<p><img src="/story.jpg" alt="比赛图"></p>'
        "<p>编写●足球文摘Web编辑部</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>正文内容足够完整，包含比赛过程和赛后采访。</p>"
        '<p><img src="/story.jpg" alt="比赛图"></p>'
    )
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_removes_caption_line_inside_paragraph():
    body = (
        "<p>J1联赛大阪钢巴9月10日宣布签下后卫卡马拉。"
        "<br>【图片】“感觉很强”大阪钢巴新援卡马拉！"
        "<br>现年24岁的这名后卫上赛季效力于葡萄牙联赛。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>J1联赛大阪钢巴9月10日宣布签下后卫卡马拉。"
        "<br>现年24岁的这名后卫上赛季效力于葡萄牙联赛。</p>"
    )
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_removes_newline_separated_caption_and_byline_lines():
    body = (
        "<p>正文第一段足够完整。\n【视频】上田绮世让球迷震惊的瞬间\n正文第二段也足够完整。</p>"
        "<p>撰文：足球文摘网编辑部</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>正文第一段足够完整。\n正文第二段也足够完整。</p>"


def test_quality_preprocess_removes_half_width_caption_markers():
    body = "<p>[视频]南野最美好的回忆！卡拉宝杯劲射破门！</p><p>[图片]维尼修斯与恋人拥抱</p>"

    assert preprocess_quality_body(body) == ""


def test_quality_preprocess_removes_byline_variants():
    body = (
        "<p>文●白鸟和洋（足球文摘TV编辑长）</p>"
        "<p>编辑●《足球文摘》网络版编辑部</p>"
        "<p>文●沢田啓明</p>"
        "<p>正文内容足够完整，包含比赛过程和赛后采访。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>正文内容足够完整，包含比赛过程和赛后采访。</p>"


def test_quality_preprocess_strips_caption_at_block_start():
    # 口径 A：段首的独立媒体标记句（【视频】…！）整句删除，
    # 保留其后的正文句子。
    body = (
        "<p>【视频】谷口彰悟打进戏剧性制胜球！圣图尔登首发4名日本球员。"
        "下半场双方迟迟未能破门，比赛以0-0进入补时。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>圣图尔登首发4名日本球员。"
        "下半场双方迟迟未能破门，比赛以0-0进入补时。</p>"
    )
    assert "【视频】" not in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_caption_marker_inside_a_sentence():
    body = "<p>官方账号发布了【图片】维尼修斯与恋人拥抱的照片，引发热议。</p>"

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_strips_related_video_wedged_between_sentences():
    # #17225：相关视频导流句夹在两句正文之间（前有句号、后有正文），
    # 只删这一句，前后正文无缝衔接、无双空格。
    body = (
        "<p>继中场喜田阳之后，又有一名新援离队。"
        " 【视频】横滨FM16岁球员三井寺，震撼的联赛首轮处子球！ "
        "这名右后卫在本赛季开打前已有178场J联赛出场经历。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>继中场喜田阳之后，又有一名新援离队。"
        "这名右后卫在本赛季开打前已有178场J联赛出场经历。</p>"
    )
    assert "【视频】" not in cleaned
    assert "  " not in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_image_when_stripping_wedged_caption():
    # 夹在句间的视频导流句删除时，同块/相邻的图片块不受影响。
    body = (
        "<p>又有一名新援离队。 【视频】横滨FM球员的处子球！ 这名右后卫经验丰富。</p>"
        '<p><img src="/fastdfs8/M00/one.jpg"/></p>'
    )

    cleaned = preprocess_quality_body(body)

    assert "【视频】" not in cleaned
    assert '<img src="/fastdfs8/M00/one.jpg"/>' in cleaned
    assert "这名右后卫经验丰富。" in cleaned


def test_quality_preprocess_keeps_block_that_carries_an_image():
    body = '<p><img src="/one.jpg" alt="合影">【图片】维尼修斯与恋人拥抱</p>'

    assert preprocess_quality_body(body) == body
    assert find_media_artifact_lines(body) == []


def test_media_artifact_helpers_report_rules_and_are_idempotent():
    body = "<p>【积分榜】明治安田J1联赛最新排名</p><p>编写●足球文摘Web编辑部</p>"

    found = find_media_artifact_lines(body)

    assert [item["rule"] for item in found] == [
        "media_caption_line",
        "editorial_byline_line",
    ]
    assert remove_media_artifact_lines(body) == ""
    assert remove_media_artifact_lines("") == ""
    assert find_media_artifact_lines("<p>正文内容足够完整。</p>") == []


def test_quality_preprocess_strips_byline_glued_to_sentence_tail():
    # Real feed shape: the byline is glued to the end of a reporting sentence
    # with only a space (no <br>/newline) after the full stop.
    body = (
        "<p>日本队在小组赛两场过后1胜1平积4分，暂列第二。"
        "末轮她们将对阵已经提前锁定淘汰赛席位的榜首意大利队。 "
        "编排●Soccer Digest Web编辑部</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>日本队在小组赛两场过后1胜1平积4分，暂列第二。"
        "末轮她们将对阵已经提前锁定淘汰赛席位的榜首意大利队。</p>"
    )
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "editorial_byline_tail",
    ]


def test_quality_preprocess_strips_byline_lead_in_bianzhuan():
    # Real feed shape from a Yahoo Japan translation: the byline lead-in token
    # ``编撰`` was previously missing from the matcher, so the signature tail
    # survived at the end of the reporting sentence.
    body = (
        "<p>这位在2025年7月东亚杯完成日本国家队首秀的后腰，"
        "外界也在等待他的新东家首秀。 编撰●足球文摘Web编辑部</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>这位在2025年7月东亚杯完成日本国家队首秀的后腰，"
        "外界也在等待他的新东家首秀。</p>"
    )
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "editorial_byline_tail",
    ]


def test_quality_preprocess_strips_byline_lead_in_bianyi():
    # #17236：日媒译稿常用的署名引导词 ``编译`` 此前不在词表，导致句尾署名残留。
    body = (
        "<p>外界普遍认为，他在脚法上甚至比转会切尔西的阿根廷门将"
        "埃米利亚诺-马丁内斯更出色。 编译●足球文摘Web编辑部</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>外界普遍认为，他在脚法上甚至比转会切尔西的阿根廷门将"
        "埃米利亚诺-马丁内斯更出色。</p>"
    )
    assert "编译●" not in cleaned
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "editorial_byline_tail",
    ]


def test_quality_preprocess_keeps_sentence_that_merely_mentions_editor():
    # A normal sentence containing "编辑" without a ●-style byline marker after
    # a full stop must never be truncated.
    body = "<p>这名记者曾长期担任报社编辑，负责国际足球报道多年。</p>"

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_strips_trailing_reporter_email():
    # #19098/#19089：韩媒稿件段尾的记者邮箱以前只能靠 AI 判脏后再修复，
    # AI 判定不稳定时就会带着残留发布，因此在质检前确定性删除。
    body = (
        "<p>这是冲击四连冠的第一步。外界关注李敏成队能否迅速撕开卡塔尔的密集防守，"
        "顺利开启金牌之旅。 /reccos23@osen.co.kr</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>这是冲击四连冠的第一步。外界关注李敏成队能否迅速撕开卡塔尔的密集防守，"
        "顺利开启金牌之旅。</p>"
    )
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "editorial_byline_tail",
    ]


def test_quality_preprocess_strips_trailing_editorial_department_byline():
    # #19160：署名把"编辑部"写在媒体名之后，旧的 lead-in 规则匹配不到。
    body = (
        "<p>他再次强调，目标就是“在主场争取夺冠”。 "
        "FOOTBALL ZONE编辑部・上原拓真 / Takuma Uehara</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>他再次强调，目标就是“在主场争取夺冠”。</p>"
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "editorial_byline_tail",
    ]


def test_quality_preprocess_keeps_sentence_containing_email():
    # 含邮箱的完整报道句不是署名残留，必须逐字保留。
    body = (
        "<p>俱乐部表示球迷可通过 ticket@club.com 申请客场球票。"
        "官方同时公布了本轮的售票时间安排。</p>"
    )

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_keeps_editorial_department_sentence():
    # "编辑部"后面没有署名分隔符时是普通句子，不能删。
    body = "<p>俱乐部已经提出申诉，编辑部对此未予置评。</p>"

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_strips_trailing_photo_credit_marker():
    # #19189：图注末尾的 "[图片]=Getty Images" 既不以句号结尾也不是整行图注，
    # 此前只能靠 AI 判脏后修复，而 AI 的计划会留下孤立的 "[图片]" 标记。
    body = "<p>朗斯在联赛开局4场后解雇主帅[图片]=Getty Images</p>"

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>朗斯在联赛开局4场后解雇主帅</p>"
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "photo_credit_tail",
    ]


def test_quality_preprocess_strips_trailing_copyright_credit_marker():
    body = "<p>田中和卡尔弗特-勒温用膝滑庆祝胜利（右）。(C)Getty Images</p>"

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>田中和卡尔弗特-勒温用膝滑庆祝胜利（右）。</p>"
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_report_glued_to_unpunctuated_promo():
    # 推广标题没有句末标点时，80 字窗口会一路吃到后面的战报。没有可靠的文本边界
    # 能切开两者，所以待删文本里出现比分/分钟/公告时一律放弃删除——留一句推广语
    # 是观感问题，删掉比分是事实错误。
    for body in (
        "<p>【视频】李刚仁与偶像合作画面来了比赛中，马竞第43分钟由乔尔吉-多明格斯"
        "破门取得领先，但下半场连丢3球，最终1-3告负。</p>",
        "<p>【图片集】比赛首日赛况 大津对阵创成馆时，凭借中场山本翼等人的进球，以3-0大胜。</p>",
        "<p>【图片】引发热议的J1最新积分榜 FC町田泽维亚发布公告称：中山雄太将离队。</p>",
    ):
        assert preprocess_quality_body(body) == body


def test_quality_preprocess_still_strips_promo_without_match_data():
    # 守卫只认比分/分钟/公告，"破门""助攻"这类词推广标题本来就在用，不能因此放弃删除。
    cases = {
        "<p>上田绮世再次取得进球。【视频】“太夸张了吧”上田绮世的强力俯身冲顶破门！</p>": (
            "<p>上田绮世再次取得进球。</p>"
        ),
        "<p>新潟取得领先。【视频】三户舜介送出精彩直塞助攻</p>": "<p>新潟取得领先。</p>",
    }

    for body, expected in cases.items():
        cleaned = preprocess_quality_body(body)
        assert cleaned == expected
        assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_json_braces_inside_image_attributes():
    # WordPress 源的 data-image-meta 属性值是 JSON，花括号清理此前会把它删成
    # 残缺属性，导致图片安全校验失败、整篇稿件得不到清理。
    body = (
        "<p>正文里漏出了{{c|东京绿茵}的高亮标记。</p>"
        '<p><img src="/body.jpg" data-image-meta=\'{"aperture":"6.3","credit":"Getty"}\''
        ' data-image-title="Tatsuki Nara" /></p>'
    )

    cleaned = preprocess_quality_body(body)

    assert '{"aperture":"6.3","credit":"Getty"}' in cleaned
    assert "<p>正文里漏出了东京绿茵的高亮标记。</p>" in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_goal_scorer_list_heading():
    # 【进球者】是战报正文的小标题，后面跟的是进球名单，不是媒体图注。
    # 此前 _INLINE_MEDIA_CAPTION 的标记词表里有"进球"，会连着吃掉 80 字进球名单。
    body = (
        "<p>【进球者】 1-0　42分钟　丹尼尔-平特尔（迈阿密国际） "
        "1-1　50分钟　丹尼尔-阿尔西拉（莱昂体育） 2-1　54分钟　扬尼克-布赖特（迈阿密国际）</p>"
    )

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_still_strips_goal_highlight_promo():
    # 排除的只有"者"字，【进球】集锦推广标记仍要照常删除。探针词表里没有"进球"，
    # 所以这里用一个【视频】图注块把清理链路打开，再看同一篇里的【进球】推广。
    body = (
        "<p>【视频】本轮最佳进球集锦</p>"
        "<p>广岛在主场取得领先。【进球】铃木章斗的重炮世界波</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>广岛在主场取得领先。</p>"
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_inline_c_in_score():
    # 句中出现的 (C) 不是版权标记，正文必须逐字保留。
    body = "<p>本场比赛的比分是2(C)1，主队获胜。</p>"

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_strips_bracketed_photo_credit_colon_tail():
    # 日本源图注的括号署名形态。此前 _PHOTO_CREDIT_TAIL 只认 "[图片]=X" 和 "(C)X"，
    # 这些冒号形态全部漏网，整篇稿件被判脏后转人工。
    cases = {
        "<p>京都新球衣受到关注【摄影：柳濑心祐】</p>": "<p>京都新球衣受到关注</p>",
        "<p>加盟湘南的冈村来佳【摄影：森田直树/阿弗罗体育】</p>": "<p>加盟湘南的冈村来佳</p>",
        "<p>巴里举行的反种族主义活动。（摄影：朱塞佩-贝里尼/Getty Images）</p>": (
            "<p>巴里举行的反种族主义活动。</p>"
        ),
        # 图片/照片 标签与摄影同类，此前直接发到了线上。
        "<p>在巴塞罗那青训效力的西山芯太【图片：本人提供】</p>": (
            "<p>在巴塞罗那青训效力的西山芯太</p>"
        ),
        "<p>挪威队的埃尔林-布劳特-哈兰德【照片：德原隆元】</p>": (
            "<p>挪威队的埃尔林-布劳特-哈兰德</p>"
        ),
    }

    for body, expected in cases.items():
        cleaned = preprocess_quality_body(body)
        assert cleaned == expected
        assert preprocess_quality_body(cleaned) == cleaned
        assert [item["rule"] for item in find_media_artifact_lines(body)] == [
            "photo_credit_tail",
        ]


def test_quality_preprocess_strips_plain_photo_credit_colon_tail():
    # 裸形署名紧跟句末标点（可带 "/" 分隔符）时同样是图注残留。
    cases = {
        "<p>大阪钢巴35周年纪念球衣引发球迷热议。/摄影：中地拓也</p>": (
            "<p>大阪钢巴35周年纪念球衣引发球迷热议。</p>"
        ),
        "<p>川崎前锋闯入决赛，最终获得亚军。摄影：中地拓也</p>": (
            "<p>川崎前锋闯入决赛，最终获得亚军。</p>"
        ),
        "<p>鬼木达回顾对阵福冈黄蜂的比赛。摄影：金子拓弥（《足球文摘》摄影部）</p>": (
            "<p>鬼木达回顾对阵福冈黄蜂的比赛。</p>"
        ),
        "<p>新潟转会加盟长崎的长谷川元希。照片：滝川敏之</p>": (
            "<p>新潟转会加盟长崎的长谷川元希。</p>"
        ),
        "<p>本赛季转战英冠的西汉姆联。图片=Getty Images</p>": (
            "<p>本赛季转战英冠的西汉姆联。</p>"
        ),
    }

    for body, expected in cases.items():
        cleaned = preprocess_quality_body(body)
        assert cleaned == expected
        assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_removes_standalone_photo_credit_block():
    # naversp 把图注署名单独成块附在正文末尾，段尾规则够不到，需要整行删除。
    body = (
        "<p>最终在16年后做出出售决定。</p>"
        "<p>图片=Getty Images Korea</p>"
        "<p>合作咨询 ad@sportalkorea.co.kr</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>最终在16年后做出出售决定。</p>"
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "photo_credit_line",
        "ad_contact_line",
    ]


def test_quality_preprocess_keeps_photo_colon_used_as_ordinary_punctuation():
    # "一张照片：" 后面接的是描述而不是署名，裸形规则要求紧跟句末标点，
    # 所以这句必须逐字保留。
    body = (
        "<p>他又回忆了弗朗切斯科-巴雷西：“我家里有一张照片：在中国的泳池边，"
        "我和他摆出健美姿势。”</p>"
    )

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_keeps_photo_label_inside_a_reporting_sentence():
    # 标签词出现在句子中间时是报道内容的一部分，不能当署名删。
    for body in (
        "<p>他在社媒发了三张图片：第一张是训练照，另外两张是球迷合影。</p>",
        "<p>俱乐部公布了新赛季球衣，官方摄影团队全程记录了拍摄过程。</p>",
        "<p>球队晒出照片。照片里的球员正在为新赛季做准备。</p>",
    ):
        assert preprocess_quality_body(body) == body


def test_quality_preprocess_removes_ad_contact_block():
    # naversp 固定附在正文末尾的招商引流块，AI 每次判脏却不给修复计划，
    # 结果整篇转人工。整块只有招商语和邮箱时直接删除。
    body = (
        "<p>目前，朴浩民也正效力于该队，活跃在赛场之上。</p>"
        "<p>合作咨询 ad@sportalkorea.co.kr</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>目前，朴浩民也正效力于该队，活跃在赛场之上。</p>"
    assert preprocess_quality_body(cleaned) == cleaned
    assert [item["rule"] for item in find_media_artifact_lines(body)] == [
        "ad_contact_line",
    ]


def test_quality_preprocess_keeps_reporting_sentence_mentioning_cooperation():
    # 整行不只是招商语时不能删，正常报道句必须保留。
    body = "<p>俱乐部表示，关于合作咨询的具体条款仍在与赞助商谈判之中。</p>"

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_strips_inline_promo_mid_paragraph():
    # 引流句夹在正常段落中间（平台词 ge + 行动号召 点击这里/跟进），
    # 只删这句，前后正文保留。
    body = (
        "<p>本周六将客场对阵桑德兰，比赛北京时间16点在光明球场打响。"
        "ge 将实时跟进本场比赛（点击这里）。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>本周六将客场对阵桑德兰，比赛北京时间16点在光明球场打响。</p>"
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_strips_standalone_promo_block():
    # 整段就是引流，删空后由空块清理移除；周边正文块不受影响。
    body = (
        "<p>利物浦和富勒姆将于本周六交锋。</p>"
        "<p>你可以通过ge的实时文字直播关注这场比赛。</p>"
        "<p>客场2比0取胜后，利物浦升至第六位。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert "你可以通过ge" not in cleaned
    assert "利物浦和富勒姆将于本周六交锋。" in cleaned
    assert "利物浦升至第六位。" in cleaned


def test_quality_preprocess_cuts_broadcast_tail_but_keeps_schedule():
    # 长信息句尾部挂引流：只删"转播：…点击这里"尾巴，保留日期/地点。
    body = (
        "<p>日期：2026年9月12日 地点：安菲尔德球场 "
        "转播：ESPN、Disney+（流媒体）和ge实时直播（点击这里）。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert "日期：2026年9月12日" in cleaned
    assert "安菲尔德球场" in cleaned
    assert "转播：ESPN" not in cleaned
    assert "点击这里" not in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_strips_broadcast_promo_across_br_lines():
    # 引流在 <br> 分隔的信息块最后一行，前面的时间/地点行保留，尾随 <br> 清理干净。
    body = (
        "<p>比赛时间：2026年9月12日 北京时间16点<br/>"
        "比赛地点：英格兰桑德兰，光明球场<br/>"
        "直播平台：Disney+（流媒体）和ge实时跟进（点击这里）。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == (
        "<p>比赛时间：2026年9月12日 北京时间16点<br/>"
        "比赛地点：英格兰桑德兰，光明球场</p>"
    )
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_keeps_sentence_with_only_one_promo_factor():
    # 双要素约束：只出现"关注/观看"或只出现平台名，不构成引流，不能删。
    body = (
        "<p>球迷们持续关注这场比赛的走势。ESPN 曾多次报道过这支球队的历史。</p>"
    )

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_strips_pure_broadcast_label_sentence():
    # 口径 A：以"转播："标签开头、无行动号召的纯播出句整块删除，
    # 前后正文块不受影响。
    body = (
        "<p>如果再输球，两队差距可能扩大到6分。</p>"
        "<p>转播：Premiere面向全巴西直播。实时：ge将带来全部比赛进程和独家视频（点击这里）。</p>"
        "<p>路易斯将无法使用左后卫马龙。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert "转播：Premiere" not in cleaned
    assert "点击这里" not in cleaned
    assert "两队差距可能扩大到6分。" in cleaned
    assert "路易斯将无法使用左后卫马龙。" in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_cuts_broadcast_clause_keeps_leading_fact():
    # 口径 A：句中以逗号接的"<平台>将现场直播"播出子句删除，
    # 保留前面的实义子句（轮次信息），且不留悬空逗号。
    body = (
        "<p>这场比赛属于巴甲第27轮，Premiere将现场直播。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert cleaned == "<p>这场比赛属于巴甲第27轮。</p>"
    assert "现场直播" not in cleaned
    assert "，。" not in cleaned
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_protects_broadcast_sentence_with_facts():
    # 保护约束：含日期/地点/首发等实义信息的句子不因播出词被整句删。
    body = (
        "<p>预计首发：韦弗顿、卡伊托、瓦拉斯，Premiere将现场直播预热节目。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert "预计首发：韦弗顿、卡伊托、瓦拉斯" in cleaned


def test_quality_preprocess_strips_leaked_highlight_tag_braces():
    body = (
        "<p>■J1 水户蜀葵 30 中场奥村仁</p>"
        "<p>{{c|东京绿茵}可\n33 前锋一美和成\n门将高居丈流(二种)</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert "{" not in cleaned and "}" not in cleaned
    assert "东京绿茵可" in cleaned
    assert "33 前锋一美和成" in cleaned
    # Idempotent: a cleaned body has no braces left for the probe to match.
    assert preprocess_quality_body(cleaned) == cleaned


def test_quality_preprocess_drops_stray_braces_and_keeps_names():
    body = (
        "<p>{{吉田真信}连入两球，浦和青年队最终将比分扳成2-2。</p>"
        "<p>[广}}驹野友春（第17分钟）</p>"
        "<p>DF望月亨利海辉}紧急替补登场。</p>"
    )

    cleaned = preprocess_quality_body(body)

    assert "{" not in cleaned and "}" not in cleaned
    assert "吉田真信连入两球" in cleaned
    assert "驹野友春" in cleaned
    assert "望月亨利海辉紧急替补登场" in cleaned


def test_quality_preprocess_strips_broadcast_promo_without_period():
    # #17255：ge来源的直播/实时跟进推广句有时不带句号结尾，
    # 之前的正则要求必须有句号，导致这类推广内容无法被预处理删除。
    body = (
        "<p>弗拉门戈和圣保罗将于本周六北京时间16时30分进行巴西女子锦标赛半决赛首回合。"
        "比赛将在里约热内卢的卢索-巴西莱罗球场进行，TV Globo、sportv和getv将进行直播。"
        "ge将实时跟进本场比赛的所有细节（点击这里查看）。</p>"
        "<p>次回合定于9月19日下周六同一时间进行，比赛地点待定。</p>"
        "<p>直播：TV Globo、sportv和getv 实时跟进：ge全程关注——点击这里</p>"
        "<p>主裁判：伊丽莎白（塞阿拉）</p>"
    )

    cleaned = preprocess_quality_body(body)

    # 第1段中的推广句（有句号）应被删除
    assert "ge将实时跟进本场比赛的所有细节（点击这里查看）" not in cleaned
    # 第3段整个推广块（无句号）应被删除
    assert "直播：TV Globo、sportv和getv 实时跟进：ge全程关注——点击这里" not in cleaned
    # 正常内容应保留
    assert "弗拉门戈和圣保罗将于本周六北京时间16时30分进行巴西女子锦标赛半决赛首回合" in cleaned
    assert "次回合定于9月19日下周六同一时间进行" in cleaned
    assert "主裁判：伊丽莎白" in cleaned
    # 幂等性检查
    assert preprocess_quality_body(cleaned) == cleaned
