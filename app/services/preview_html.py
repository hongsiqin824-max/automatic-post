"""Build safe, browser-ready HTML for the local article preview only."""

from __future__ import annotations

import html
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from markupsafe import Markup

from .link_sanitizer import remove_clickable_links


DEFAULT_IMAGE_BASE_URL = "https://img1.qunliao.info/"

_ALLOWED_TAGS = {
    "b",
    "blockquote",
    "br",
    "del",
    "div",
    "em",
    "figcaption",
    "figure",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "s",
    "span",
    "strong",
    "u",
    "ul",
}
_VOID_TAGS = {"br", "hr", "img"}
_DROP_WITH_CONTENT = {
    "embed",
    "iframe",
    "math",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
}
_UNSAFE_URL_CHARS = {"\x00", "\r", "\n", "\t"}


def _absolute_http_url(value: str) -> str | None:
    candidate = html.unescape(str(value or "")).strip()
    if not candidate or any(char in candidate for char in _UNSAFE_URL_CHARS):
        return None
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _preview_image_url(value: str, image_base_url: str) -> str | None:
    candidate = html.unescape(str(value or "")).strip()
    if not candidate or any(char in candidate for char in _UNSAFE_URL_CHARS):
        return None
    if candidate.startswith("//"):
        return _absolute_http_url(candidate)
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return _absolute_http_url(candidate)
    if not parsed.path:
        return None
    return urljoin(image_base_url.rstrip("/") + "/", candidate)


def _safe_dimension(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    dimension = int(text)
    return str(dimension) if 0 < dimension <= 10_000 else None


class _PreviewHTMLSanitizer(HTMLParser):
    def __init__(self, image_base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.image_base_url = image_base_url
        self.parts: list[str] = []
        self.blocked_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.blocked_tags:
            if tag in _DROP_WITH_CONTENT:
                self.blocked_tags.append(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            self.blocked_tags.append(tag)
            return
        if tag not in _ALLOWED_TAGS:
            return

        attributes = {name.lower(): value for name, value in attrs if name}
        if tag == "img":
            src = _preview_image_url(attributes.get("src") or "", self.image_base_url)
            if src is None:
                return
            safe_attrs = [("src", src)]
            for name in ("alt", "title"):
                if attributes.get(name):
                    safe_attrs.append((name, str(attributes[name])))
            for name in ("width", "height"):
                value = _safe_dimension(attributes.get(name))
                if value is not None:
                    safe_attrs.append((name, value))
            safe_attrs.extend((("loading", "lazy"), ("decoding", "async")))
        else:
            safe_attrs = []

        rendered_attrs = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs
        )
        self.parts.append(f"<{tag}{rendered_attrs}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked_tags:
            if tag in self.blocked_tags:
                while self.blocked_tags:
                    blocked = self.blocked_tags.pop()
                    if blocked == tag:
                        break
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_tags:
            self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.blocked_tags:
            self.parts.append(f"&amp;{html.escape(name, quote=True)};")

    def handle_charref(self, name: str) -> None:
        if not self.blocked_tags:
            self.parts.append(f"&amp;#{html.escape(name, quote=True)};")

    def get_html(self) -> str:
        return "".join(self.parts)


def sanitize_preview_html(
    body_html: str | None,
    *,
    image_base_url: str = DEFAULT_IMAGE_BASE_URL,
) -> Markup:
    """Return sanitized rich text without mutating the stored or published body."""

    safe_base_url = _absolute_http_url(image_base_url)
    if safe_base_url is None:
        raise ValueError("image_base_url must be an absolute HTTP(S) URL")
    parser = _PreviewHTMLSanitizer(safe_base_url)
    parser.feed(remove_clickable_links(body_html))
    parser.close()
    return Markup(parser.get_html())
