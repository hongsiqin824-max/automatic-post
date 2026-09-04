"""Remove linked text while preserving article images and non-link content.

The material API can return promotional and inline text wrapped in ``<a>``
elements.  Published articles must not retain either the clickable target or
its linked text.  Images wrapped by anchors remain article media, so only
their clickable wrapper is removed.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


_ANCHOR_START = re.compile(r"<\s*a(?:\s|/?>)", re.IGNORECASE)
_RESIDUAL_ANCHOR = re.compile(r"<\s*/?\s*a\b[^>]*(?:>|$)", re.IGNORECASE)
_QUALITY_ARTIFACT_COMMENT = re.compile(
    r"<!--\s*(?:#(?:include|set|exec|echo)\b.*?|google_ad_section_(?:start|end)\b[^-]*|(?:brightcove|video-js|jwplayer)\b[^-]*|(?:start|end)\s+of\s+(?:brightcove|video-js|jwplayer)\s+player[^-]*)-->"
    r"|<\s*google_ad_section_(?:start|end)\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_QUALITY_DROP_CONTENT = re.compile(
    r"<(?P<tag>area|embed|iframe|math|noscript|object|script|style|svg|template|video|audio)\b[^>]*>"
    r".*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_QUALITY_DROP_SELF_CLOSING = re.compile(
    r"<\s*area\b[^>]*>|"
    r"<\s*(?:embed|iframe|math|noscript|object|script|style|svg|template|video|audio)\b[^>]*/\s*>",
    re.IGNORECASE,
)
_GOOGLE_AD_SECTION = re.compile(
    r"<!--\s*google_ad_section_start\b[^>]*-->.*?<!--\s*google_ad_section_end\b[^>]*-->",
    re.IGNORECASE | re.DOTALL,
)
_CLICKABLE_ATTRIBUTE = re.compile(
    r"\s+(?:on[a-z][a-z0-9_-]*|data-(?:href|url|link)|xlink:href)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_QUALITY_EMPTY_MARKER_BLOCK = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>\s*"
    r"(?:<(?:strong|b|span)\b[^>]*>\s*)?"
    r"转会中心\s*[:：]?\s*"
    r"(?:</(?:strong|b|span)>\s*)?</(?P=tag)\s*>",
    re.IGNORECASE,
)
_QUALITY_PLAIN_MARKER_LINE = re.compile(
    r"(?m)^[ \t]*(?:google_ad_section_(?:start|end)(?:\([^\r\n)]*\))?|前文(?:链接)?|正文|相关SSI(?:（正文中）)?)[ \t]*$",
    re.IGNORECASE,
)
_QUALITY_MARKER_TEXT_BLOCK = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>\s*"
    r"(?:google_ad_section_(?:start|end)(?:\([^)]*\))?|前文(?:链接)?|正文|相关SSI(?:（正文中）?))\s*"
    r"</(?P=tag)\s*>",
    re.IGNORECASE,
)
_MARKDOWN_DESTINATION = (
    r"(?:https?://|//|/|#|\.\.?/|mailto:|tel:|javascript:|data:)"
    r"[^)\s>]+"
)
_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[[^\]\r\n]+\]\(\s*(?:<"
    + _MARKDOWN_DESTINATION
    + r">|"
    + _MARKDOWN_DESTINATION
    + r")"
    r"(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\)\r\n]*\)))?\s*\)",
    re.IGNORECASE,
)
_MARKDOWN_REFERENCE_LINK = re.compile(
    r"(?<!!)\[(?P<text>[^\]\r\n]+)\]\[(?P<label>[^\]\r\n]*)\]",
    re.IGNORECASE,
)
_MARKDOWN_REFERENCE_DEFINITION = re.compile(
    r"(?m)^[ \t]{0,3}\[(?P<label>[^\]\r\n]+)\]:\s*"
    r"(?:<"
    + _MARKDOWN_DESTINATION
    + r">|"
    + _MARKDOWN_DESTINATION
    + r")(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'))?\s*$",
    re.IGNORECASE,
)


def _normalise_markdown_label(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _remove_markdown_reference_links(body: str) -> str:
    """Remove only reference links backed by an explicit URL definition."""

    definitions = {
        _normalise_markdown_label(match.group("label"))
        for match in _MARKDOWN_REFERENCE_DEFINITION.finditer(body)
    }
    if not definitions:
        return body

    def replace_reference(match: re.Match[str]) -> str:
        label = match.group("label") or match.group("text")
        return "" if _normalise_markdown_label(label) in definitions else match.group(0)

    body = _MARKDOWN_REFERENCE_LINK.sub(replace_reference, body)
    return _MARKDOWN_REFERENCE_DEFINITION.sub("", body)


class _ClickableLinkRemover(HTMLParser):
    """Drop anchor text and nested markup while retaining linked images."""

    def __init__(self, *, drop_linked_content: bool = False) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._link_depth = 0
        self._drop_linked_content = drop_linked_content

    def _raw_start_tag(self) -> str:
        raw = self.get_starttag_text()
        return raw if raw is not None else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if self._link_depth:
            if lowered == "a":
                self._link_depth += 1
            elif lowered == "img":
                # A linked image is still article media; only its clickable
                # wrapper should disappear.
                self.parts.append(self._raw_start_tag())
            elif not self._drop_linked_content:
                self.parts.append(self._raw_start_tag())
            return
        if lowered == "a":
            self._link_depth = 1
            return
        self.parts.append(self._raw_start_tag())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if self._link_depth:
            if lowered == "img":
                self.parts.append(self._raw_start_tag())
            elif not self._drop_linked_content:
                self.parts.append(self._raw_start_tag())
            return
        if lowered == "a":
            return
        self.parts.append(self._raw_start_tag())

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._link_depth:
            if lowered == "a":
                self._link_depth -= 1
            elif not self._drop_linked_content:
                self.parts.append(f"</{tag}>")
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"<?{data}>")


def remove_clickable_links(body_html: str | None) -> str:
    """Remove closed ``<a>`` elements together with their linked text.

    The function is intentionally idempotent.  Non-link markup and text are
    retained, and images inside links are preserved without the clickable
    wrapper. Invalid or incomplete HTML is handled by :class:`HTMLParser`
    without raising. When an anchor is not closed, its boundary is ambiguous,
    so the fail-safe removes the link tag but retains the remaining text.
    """

    body = str(body_html or "")
    if _ANCHOR_START.search(body) is None:
        return body
    parser = _ClickableLinkRemover(drop_linked_content=True)
    parser.feed(body)
    parser.close()
    # An unclosed anchor would otherwise make the parser drop the rest of an
    # article. In that malformed case, unwrap anchors and retain their content
    # so no clickable target remains and valid article text is not lost.
    if parser._link_depth:
        fallback = _ClickableLinkRemover(drop_linked_content=False)
        fallback.feed(body)
        fallback.close()
        return _RESIDUAL_ANCHOR.sub("", "".join(fallback.parts))
    cleaned = "".join(parser.parts)
    # HTMLParser treats an incomplete start tag as plain data. Remove that
    # residual fragment as a final fail-safe so malformed upstream HTML cannot
    # leave a clickable href in the submitted body.
    return _RESIDUAL_ANCHOR.sub("", cleaned)


def preprocess_quality_body(body_html: str | None) -> str:
    """Remove non-article embeds and feed markers before quality checks.

    This cleanup is deliberately separate from :func:`remove_clickable_links`,
    which is used by persistence and publishing code with an established
    contract.  The operation is idempotent and keeps ordinary markup and
    ``img`` nodes unchanged while dropping executable/embed content.
    """

    body = str(body_html or "")
    if not body:
        return body
    if not (
        _ANCHOR_START.search(body)
        or _MARKDOWN_LINK.search(body)
        or _MARKDOWN_REFERENCE_LINK.search(body)
        or _MARKDOWN_REFERENCE_DEFINITION.search(body)
        or _QUALITY_ARTIFACT_COMMENT.search(body)
        or _QUALITY_PLAIN_MARKER_LINE.search(body)
        or _QUALITY_MARKER_TEXT_BLOCK.search(body)
        or _QUALITY_EMPTY_MARKER_BLOCK.search(body)
        or _CLICKABLE_ATTRIBUTE.search(body)
        or re.search(r"(?:brightcove|video-js|jwplayer|vjs-player|player-container)", body, re.IGNORECASE)
        or re.search(r"<\s*(?:area|embed|iframe|math|noscript|object|script|style|svg|template|video|audio)\b", body, re.IGNORECASE)
    ):
        return body
    body = remove_clickable_links(body)
    body = _GOOGLE_AD_SECTION.sub("", body)
    body = _QUALITY_ARTIFACT_COMMENT.sub("", body)
    body = _QUALITY_PLAIN_MARKER_LINE.sub("", body)
    body = _QUALITY_MARKER_TEXT_BLOCK.sub("", body)
    body = _QUALITY_EMPTY_MARKER_BLOCK.sub("", body)
    # Remove complete embed containers.  Unclosed tags are intentionally left
    # in place; the quality layer will flag them instead of swallowing article
    # text after a malformed upstream fragment.
    body = _QUALITY_DROP_CONTENT.sub("", body)
    body = _QUALITY_DROP_SELF_CLOSING.sub("", body)
    body = _CLICKABLE_ATTRIBUTE.sub("", body)
    body = _MARKDOWN_LINK.sub("", body)
    return _remove_markdown_reference_links(body)
