from __future__ import annotations

from app.services.link_sanitizer import remove_clickable_links


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
