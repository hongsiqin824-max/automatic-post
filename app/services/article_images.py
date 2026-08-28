"""Small helpers for selecting an article's first usable image."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urlsplit


_FIRST_PARAGRAPH_END = re.compile(r"</p\s*>", re.IGNORECASE)
PUBLIC_DEFAULT_LITPIC = "/fastdfs7/M00/71/51/rBUC6Gh3f-uAJ3PaAABRUJ73Hek971.jpg"


class ArticleImageError(ValueError):
    """Raised when an article has no image that is safe to publish."""


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _usable_image_src(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = html.unescape(value).strip()
    if not candidate or _has_control_character(candidate):
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    # The open-platform contract accepts stored paths. HTTP(S) is also kept
    # intact because some upstream articles provide a fully qualified image.
    if scheme and scheme not in {"http", "https"}:
        return None
    if scheme in {"http", "https"} and not parsed.netloc:
        return None
    if candidate.startswith("//") and not parsed.netloc:
        return None
    if not scheme and not parsed.path and not parsed.netloc:
        return None
    return candidate


class _FirstImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.src: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.src is not None or tag.lower() != "img":
            return
        attributes = {name.lower(): value for name, value in attrs if name}
        self.src = _usable_image_src(attributes.get("src"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def first_image_src(body_html: str | None) -> str | None:
    """Return the first usable ``img[src]`` without changing the HTML."""

    parser = _FirstImageParser()
    try:
        parser.feed(str(body_html or ""))
        parser.close()
    except (TypeError, ValueError):
        return None
    return parser.src


def is_public_default_litpic(value: Any) -> bool:
    candidate = _usable_image_src(value)
    if not candidate:
        return False
    return candidate.rstrip("/") == PUBLIC_DEFAULT_LITPIC.rstrip("/") or candidate.rstrip("/").endswith(PUBLIC_DEFAULT_LITPIC.rstrip("/"))


def fallback_litpic_for_tabs(tabs: Any) -> str:
    """Choose the first non-精选 tab cover, falling back to the shared image."""
    if isinstance(tabs, Mapping):
        values = [tabs]
    else:
        try:
            values = list(tabs or [])
        except TypeError:
            values = []
    for tab in values:
        if not isinstance(tab, Mapping):
            continue
        try:
            backend_id = int(tab.get("backend_tab_id"))
        except (TypeError, ValueError):
            continue
        # 58 is the real DQD featured/general column. Keep -1 for databases
        # created from the legacy local catalog.
        if backend_id in {-1, 58}:
            continue
        configured = _usable_image_src(tab.get("fallback_litpic") or tab.get("fallback_image"))
        if configured:
            return configured
    return PUBLIC_DEFAULT_LITPIC


def effective_litpic(article: Mapping[str, Any], *, fallback_litpic: str | None = None) -> str:
    """Choose the image submitted as the backend cover for one article."""

    body_image = first_image_src(article.get("body_html") or article.get("body"))
    if body_image:
        return body_image
    material_image = _usable_image_src(article.get("litpic"))
    if material_image and not is_public_default_litpic(material_image):
        return material_image
    return _usable_image_src(fallback_litpic) or PUBLIC_DEFAULT_LITPIC


def build_publish_body(body_html: str | None, litpic: str | None) -> str:
    """Ensure publishable HTML contains one safe image without duplication.

    Existing HTML is returned byte-for-byte when it already contains a usable
    image. Otherwise the validated cover is inserted after the first closing
    paragraph, or before the body when there is no closing paragraph.
    """

    body = str(body_html or "")
    if first_image_src(body) is not None:
        return body

    image_src = _usable_image_src(litpic)
    if image_src is None:
        raise ArticleImageError("正文没有有效图片，且 litpic 不是安全的图片地址")

    image_paragraph = f'<p><img src="{html.escape(image_src, quote=True)}"></p>'
    paragraph_end = _FIRST_PARAGRAPH_END.search(body)
    if paragraph_end is None:
        return image_paragraph + body
    return body[:paragraph_end.end()] + image_paragraph + body[paragraph_end.end():]
