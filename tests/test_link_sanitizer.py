from __future__ import annotations

from app.services.link_sanitizer import preprocess_quality_body, remove_clickable_links


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
