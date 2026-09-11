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
    body = "<p>正文\n主帅介绍了球队状态。 google_ad_section_end(name=s1)</p>"

    assert preprocess_quality_body(body, source="foxsprt") == body


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


def test_quality_preprocess_keeps_caption_glued_to_reporting_sentence():
    body = (
        "<p>【视频】谷口彰悟打进戏剧性制胜球！圣图尔登首发4名日本球员。"
        "下半场双方迟迟未能破门，比赛以0-0进入补时。</p>"
    )

    assert preprocess_quality_body(body) == body


def test_quality_preprocess_keeps_caption_marker_inside_a_sentence():
    body = "<p>官方账号发布了【图片】维尼修斯与恋人拥抱的照片，引发热议。</p>"

    assert preprocess_quality_body(body) == body


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


def test_quality_preprocess_keeps_sentence_that_merely_mentions_editor():
    # A normal sentence containing "编辑" without a ●-style byline marker after
    # a full stop must never be truncated.
    body = "<p>这名记者曾长期担任报社编辑，负责国际足球报道多年。</p>"

    assert preprocess_quality_body(body) == body
