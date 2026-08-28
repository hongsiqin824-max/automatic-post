from __future__ import annotations

import pytest

from app.services.dqd_publish_html import (
    DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES,
    DqdPublishHtmlError,
    sanitize_dqd_publish_html,
)


def test_body_without_exact_unsupported_attribute_is_byte_for_byte_unchanged() -> None:
    body = (
        " \n<!-- data-image-meta='text only' -->"
        '<P class=\'lead\' style="color:red" data-club="Seoul" '
        'data-image-metadata="keep">正文&nbsp;内容</P>\n '
    )

    assert sanitize_dqd_publish_html(body) == body


def test_removes_only_known_wordpress_metadata_from_aleagues_image() -> None:
    metadata_value = '{"camera":"Canon EOS R5","caption":"A &quot;quote&quot; > B"}'
    metadata = "".join(
        f" {name.upper()}='{metadata_value}'"
        for name in sorted(DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES)
    )
    body = (
        '<p class="intro">澳超正文</p>'
        '<p><img src="https://aleagues.example/one.jpg" alt="球员" title="赛场" '
        'width="1200" height="800" class="wp-image-1" style="width:100%" '
        'data-club="keep"'
        f"{metadata}></p>"
    )

    cleaned = sanitize_dqd_publish_html(body)

    lowered = cleaned.lower()
    assert all(name not in lowered for name in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES)
    assert 'src="https://aleagues.example/one.jpg"' in cleaned
    assert 'alt="球员"' in cleaned
    assert 'title="赛场"' in cleaned
    assert 'width="1200"' in cleaned
    assert 'height="800"' in cleaned
    assert 'class="wp-image-1"' in cleaned
    assert 'style="width:100%"' in cleaned
    assert 'data-club="keep"' in cleaned
    assert "澳超正文" in cleaned


def test_cleanup_preserves_multiple_image_order_sources_and_article_text() -> None:
    body = (
        '<blockquote>第一段 &amp; 中文</blockquote>'
        '<img loading="lazy" src="https://img.example/1.jpg" '
        "data-image-meta='{" + '"camera":"R5"' + "}'>"
        '<p>中间文字 <a href="https://example.com?a=1&amp;b=2">链接</a></p>'
        '<img decoding="async" sizes="100vw" src="/fastdfs8/2.jpg" '
        'data-orig-size="1200,800">'
        "<p>最后一段</p>"
    )

    cleaned = sanitize_dqd_publish_html(body)

    assert cleaned.index("https://img.example/1.jpg") < cleaned.index("/fastdfs8/2.jpg")
    assert cleaned.count("<img") == 2
    assert "第一段 &amp; 中文" in cleaned
    assert "中间文字" in cleaned
    assert "最后一段" in cleaned
    assert 'loading="lazy"' in cleaned
    assert 'decoding="async"' in cleaned
    assert 'sizes="100vw"' in cleaned


def test_cleanup_is_idempotent() -> None:
    body = '<p>正文</p><img src="/one.jpg" data-image-title="title">'

    cleaned = sanitize_dqd_publish_html(body)

    assert sanitize_dqd_publish_html(cleaned) == cleaned


def test_cleanup_handles_unescaped_apostrophe_inside_wordpress_json() -> None:
    body = (
        '<p>正文</p><img src="/one.jpg" class="wp-image-1" '
        "data-image-meta='{\"caption\":\"the team's first goal\","
        '\"camera\":\"Canon EOS R1\"}\' data-id="42" loading="lazy">'
    )

    cleaned = sanitize_dqd_publish_html(body)

    assert cleaned == (
        '<p>正文</p><img src="/one.jpg" class="wp-image-1" '
        'data-id="42" loading="lazy">'
    )
    assert "team's" not in cleaned
    assert "camera" not in cleaned


def test_cleanup_removes_fragments_from_already_corrupted_wordpress_tag() -> None:
    body = (
        '<img alt="" class="wp-image-2" data-image-meta="{" '
        'aperture":"2.8","credit":"getty=" images="" team\'s="" first="" '
        'data-image-title="title" data-id="2" decoding="async" '
        'src="/two.jpg" width="1024"/>'
    )

    cleaned = sanitize_dqd_publish_html(body)

    assert cleaned == (
        '<img alt="" class="wp-image-2" data-id="2" decoding="async" '
        'src="/two.jpg" width="1024" />'
    )
    assert "aperture" not in cleaned
    assert "team's" not in cleaned


def test_dangerous_attribute_on_non_image_tag_is_untouched() -> None:
    body = '<a data-permalink="article-slug" href="/article">链接</a>'

    assert sanitize_dqd_publish_html(body) == body


def test_unclosed_image_tag_with_dangerous_attribute_fails_closed() -> None:
    body = '<p>正文</p><img src="/one.jpg" data-image-meta=\'{"camera":"R5"}\''

    with pytest.raises(DqdPublishHtmlError, match="无法安全解析"):
        sanitize_dqd_publish_html(body)


def test_dangerous_image_example_inside_comment_or_script_is_untouched() -> None:
    body = (
        "<!-- <img data-image-meta='example'> -->"
        "<script>const example = \"<img data-image-meta='example'>\";</script>"
    )

    assert sanitize_dqd_publish_html(body) == body


@pytest.mark.parametrize("alt", ["A < B", "A > B"])
def test_unclosed_dangerous_tag_with_angle_bracket_in_attribute_fails(alt: str) -> None:
    body = f'<img alt="{alt}" data-image-meta=\'{{"camera":"R5"}}\''

    with pytest.raises(DqdPublishHtmlError, match="无法安全解析"):
        sanitize_dqd_publish_html(body)


def test_cleanup_rejects_whitelisted_words_split_from_broken_json() -> None:
    body = (
        '<img data-image-meta=\'{"caption":"player\'s title loading width style '
        'data-club aria-label src class id"}\' data-id="42" '
        'src="/real.jpg" width="1024">'
    )

    with pytest.raises(DqdPublishHtmlError, match="图片数量、顺序或地址不一致"):
        sanitize_dqd_publish_html(body)


def test_cleanup_rejects_duplicate_real_image_attributes() -> None:
    body = '<img data-image-meta="{}" src="/one.jpg" SRC="/two.jpg">'

    with pytest.raises(DqdPublishHtmlError, match="重复属性：src"):
        sanitize_dqd_publish_html(body)
