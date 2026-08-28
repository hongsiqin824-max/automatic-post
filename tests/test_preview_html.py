from __future__ import annotations

from markupsafe import Markup

from app.services.preview_html import sanitize_preview_html


def test_preview_html_preserves_structure_and_normalizes_relative_images():
    source = (
        "<p>首段正文</p>"
        '<p><img src="/fastdfs8/M00/article.jpg" alt="正文图片"></p>'
        "<h2>小标题</h2><p><strong>后续</strong>正文</p>"
    )

    result = sanitize_preview_html(source)

    assert isinstance(result, Markup)
    assert str(result) == (
        "<p>首段正文</p>"
        '<p><img src="https://img1.qunliao.info/fastdfs8/M00/article.jpg" '
        'alt="正文图片" loading="lazy" decoding="async"></p>'
        "<h2>小标题</h2><p><strong>后续</strong>正文</p>"
    )
    assert source.endswith("<h2>小标题</h2><p><strong>后续</strong>正文</p>")


def test_preview_html_removes_executable_content_and_unsafe_attributes():
    source = (
        '<p class="lead" style="position:fixed" onclick="alert(1)">安全正文</p>'
        '<script>alert("script")</script>'
        '<iframe src="https://example.com">iframe fallback</iframe>'
        '<svg><script>alert("svg")</script><path d="x"></path></svg>'
        '<img src="javascript:alert(1)" onerror="alert(1)">'
        '<a href="javascript:alert(1)" style="color:red">链接文字</a>'
    )

    result = str(sanitize_preview_html(source))

    assert result == "<p>安全正文</p>"
    for unsafe in ("script", "iframe", "svg", "javascript", "onclick", "onerror", "style", "href"):
        assert unsafe not in result.lower()


def test_preview_html_removes_external_links_and_linked_text():
    source = (
        '<a href="https://example.com/news?a=1&amp;b=2" target="_self" '
        'rel="opener" title="详情">查看原文</a>'
    )

    result = str(sanitize_preview_html(source))

    assert result == ""


def test_preview_html_rejects_dangerous_image_protocols_and_invalid_base_url():
    assert str(sanitize_preview_html('<img src="data:image/svg+xml,bad">')) == ""

    try:
        sanitize_preview_html("<p>正文</p>", image_base_url="javascript:alert(1)")
    except ValueError as exc:
        assert str(exc) == "image_base_url must be an absolute HTTP(S) URL"
    else:  # pragma: no cover - failure branch
        raise AssertionError("invalid preview image base URL should be rejected")
