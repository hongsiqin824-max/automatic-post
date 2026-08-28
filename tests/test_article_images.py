from __future__ import annotations

import pytest

from app.services.article_images import (
    ArticleImageError,
    PUBLIC_DEFAULT_LITPIC,
    build_publish_body,
    effective_litpic,
    fallback_litpic_for_tabs,
    first_image_src,
)


def test_build_publish_body_keeps_existing_safe_image_exactly() -> None:
    body = '<P>导语</P><DIV><IMG ALT="封面" SRC="https://img.example/a.jpg"></DIV>'

    assert build_publish_body(body, "/fallback.jpg") == body


def test_build_publish_body_inserts_after_first_paragraph_case_insensitively() -> None:
    body = "<P>第一段</P><p>第二段</p>"

    assert build_publish_body(body, "/fastdfs8/cover.jpg") == (
        '<P>第一段</P><p><img src="/fastdfs8/cover.jpg"></p><p>第二段</p>'
    )


def test_build_publish_body_prepends_image_when_body_has_no_closing_paragraph() -> None:
    body = "<div>没有段落标签</div>"

    assert build_publish_body(body, "//img.example/cover.jpg") == (
        '<p><img src="//img.example/cover.jpg"></p><div>没有段落标签</div>'
    )


def test_build_publish_body_escapes_image_attribute() -> None:
    result = build_publish_body(
        "<p>正文</p>",
        '/images/cover.jpg?name=&quot;x&quot;&amp;size=large',
    )

    assert result == (
        '<p>正文</p><p><img src="/images/cover.jpg?name=&quot;x&quot;&amp;size=large"></p>'
    )


@pytest.mark.parametrize(
    "litpic",
    [
        "",
        "   ",
        "data:image/png;base64,abc",
        "javascript:alert(1)",
        "https://img.example/a.jpg\nquoted",
        "//",
        "http:/missing-host.jpg",
        "http://[invalid-host.jpg",
    ],
)
def test_build_publish_body_rejects_missing_or_unsafe_litpic(litpic: str) -> None:
    with pytest.raises(ArticleImageError, match="litpic"):
        build_publish_body("<p>正文</p>", litpic)


@pytest.mark.parametrize(
    "litpic",
    [
        "/fastdfs8/cover.jpg",
        "images/cover.jpg",
        "//img.example/cover.jpg",
        "http://img.example/cover.jpg",
        "https://img.example/cover.jpg",
    ],
)
def test_build_publish_body_accepts_supported_image_locations(litpic: str) -> None:
    result = build_publish_body("正文", litpic)

    assert first_image_src(result) == litpic


def test_build_publish_body_is_idempotent() -> None:
    first = build_publish_body("<p>正文</p>", "/fastdfs8/cover.jpg")

    assert build_publish_body(first, "/other.jpg") == first


def test_effective_litpic_prefers_body_image_and_validates_fallback() -> None:
    assert effective_litpic({
        "body_html": '<img src="https://img.example/body.jpg">',
        "litpic": "javascript:alert(1)",
    }) == "https://img.example/body.jpg"
    assert effective_litpic({"body_html": "<p>正文</p>", "litpic": "/safe.jpg"}) == "/safe.jpg"
    assert effective_litpic({
        "body_html": "<p>正文</p>",
        "litpic": "data:image/png;base64,abc",
    }) == PUBLIC_DEFAULT_LITPIC
    assert effective_litpic({"body_html": "<p>正文</p>", "litpic": 123}) == PUBLIC_DEFAULT_LITPIC


def test_effective_litpic_uses_tab_fallback_for_public_placeholder() -> None:
    assert effective_litpic(
        {"body_html": "<p>正文</p>", "litpic": PUBLIC_DEFAULT_LITPIC},
        fallback_litpic="https://cdn.example.com/jleague.jpg",
    ) == "https://cdn.example.com/jleague.jpg"


def test_tab_fallback_skips_featured_and_uses_shared_default_when_unconfigured() -> None:
    assert fallback_litpic_for_tabs([
        {"backend_tab_id": -1, "fallback_litpic": "https://cdn.example.com/featured.jpg"},
        {"backend_tab_id": 284, "fallback_litpic": ""},
    ]) == PUBLIC_DEFAULT_LITPIC


def test_tab_fallback_skips_real_backend_featured_id() -> None:
    assert fallback_litpic_for_tabs([
        {"backend_tab_id": 58, "fallback_litpic": "https://cdn.example.com/featured.jpg"},
    ]) == PUBLIC_DEFAULT_LITPIC


def test_tab_fallback_uses_first_non_featured_configured_image() -> None:
    assert fallback_litpic_for_tabs([
        {"backend_tab_id": -1, "fallback_litpic": "https://cdn.example.com/featured.jpg"},
        {"backend_tab_id": 284, "fallback_litpic": "https://cdn.example.com/first.jpg"},
        {"backend_tab_id": 247, "fallback_litpic": "https://cdn.example.com/second.jpg"},
    ]) == "https://cdn.example.com/first.jpg"


def test_tab_fallback_skips_unconfigured_non_featured_tabs() -> None:
    assert fallback_litpic_for_tabs([
        {"backend_tab_id": 284, "fallback_litpic": ""},
        {"backend_tab_id": 247, "fallback_litpic": "https://cdn.example.com/second.jpg"},
    ]) == "https://cdn.example.com/second.jpg"


def test_first_image_src_skips_unsafe_image_before_safe_image() -> None:
    body = '<IMG SRC="javascript:alert(1)"><img src="/safe.jpg">'

    assert first_image_src(body) == "/safe.jpg"
