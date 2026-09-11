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
# A link-only paragraph becomes empty after ``remove_clickable_links``.
# Remove only containers that contain whitespace/comments or empty
# presentational wrappers; image-only containers are intentionally retained.
_EMPTY_CONTENT_BLOCK = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>\s*"
    r"(?:(?:<!--.*?-->\s*)|"
    r"<(?P<fmt>strong|b|span|em|i)\b[^>]*>\s*</(?P=fmt)\s*>\s*)*"
    r"</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# ``sponichi`` publishes a CMS template as ordinary text in the translated
# body.  These markers can occur in the same paragraph as real reporting, so
# they must be removed as exact tokens rather than by deleting the paragraph.
# The source check is applied by ``preprocess_quality_body``; other sources are
# left byte-for-byte unchanged.
_SPONICHI_TEMPLATE_MARKER = re.compile(
    r"(?:^|(?<=\s)|(?<=>))(?:"
    r"google_ad_section_(?:start|end)(?:\([^\r\n)]*\))?"
    r"|前文链接|相关(?:文章)?SSI\s*[（(](?:正文|本文)中[）)]"
    r")(?=\s|<|$)",
    re.IGNORECASE,
)
_SPONICHI_TEMPLATE_LABEL = re.compile(
    r"(?:^|(?<=\r)|(?<=\n)|(?<=>))[ \t]*(?:导语|前言|正文|本文)[ \t]*(?=\r?\n|$|<)",
    re.IGNORECASE | re.MULTILINE,
)
# Upstream feeds embed template captions such as ``【图片】…`` and bylines such
# as ``编写●…编辑部`` as ordinary text.  They are either a standalone block or a
# ``<br>``/newline separated line inside a reporting paragraph, so the cleanup
# works line by line and never touches an image node.
_ARTIFACT_LINE_SEPARATOR = re.compile(r"((?:\r\n|\r|\n|<br\b[^>]*>))", re.IGNORECASE)
_ARTIFACT_TEXT_BLOCK = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>"
    r"(?P<content>(?:[^<>]|<br\b[^>]*>)*?)"
    r"</(?P=tag)\s*>",
    re.IGNORECASE,
)
_MEDIA_ARTIFACT_PROBE = re.compile(
    r"【\s*(?:图片|写真|视频|集锦|实战|直播|积分榜|赛程)"
    r"|\[\s*(?:图片|写真|photo|video|视频|集锦|直播|积分榜|赛程)"
    r"|(?:编写|撰文|编辑|记者|编排|整理|构成|供稿|文|著者)\s*[●•・·:：]",
    re.IGNORECASE,
)
# The Chinese full stop is excluded on purpose: a caption glued to a following
# sentence reads as one line, and dropping that line would delete reporting.
_MEDIA_CAPTION_LINE = re.compile(
    r"[ \t\u3000]*(?:"
    r"【\s*(?:图片|写真|视频|集锦|实战|直播|积分榜|赛程)[^】\r\n]{0,20}】"
    r"|\[\s*(?:图片|写真|photo|video|视频|集锦|直播|积分榜|赛程)[^\]\r\n]{0,20}\]"
    r")[^<>\r\n。]{0,120}[ \t\u3000]*",
    re.IGNORECASE,
)
_EDITORIAL_BYLINE_LINE = re.compile(
    r"[ \t\u3000]*(?:编写|撰文|编辑|记者|文|著者)\s*[●•・·:：]\s*[^<>\r\n。]{1,80}[ \t\u3000]*",
    re.IGNORECASE,
)
# Some feeds glue the editorial byline to the end of a real reporting line,
# separated only by a space rather than a ``<br>``/newline (for example
# ``……榜首意大利队。 编排●Soccer Digest Web编辑部``).  ``_EDITORIAL_BYLINE_LINE``
# only removes a whole line, so the trailing signature survives.  This matcher
# strips just the signature tail while keeping the sentence that precedes it.
# It is deliberately narrow: the tail must follow a sentence-ending punctuation
# mark, start with an explicit byline lead-in token and a ``●``-style marker,
# and stay short so a normal sentence is never truncated.
_EDITORIAL_BYLINE_TAIL = re.compile(
    r"(?<=[。！？!?])[ \t\u3000]*"
    r"(?:编写|撰文|编辑|记者|编排|整理|构成|供稿|文|著者)\s*[●•・·]\s*"
    r"[^<>\r\n。！？!?]{1,40}[ \t\u3000]*$",
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


def remove_empty_content_blocks(body_html: str | None) -> str:
    """Drop empty text containers while retaining image-only containers.

    This is deliberately narrower than a generic HTML minifier. It only
    removes ``p``, ``div`` and ``li`` nodes with no visible text, media, or
    arbitrary nested markup. Repeating the substitution handles an empty
    wrapper revealed by removing an inner empty block.
    """

    body = str(body_html or "")
    previous = None
    while body != previous:
        previous = body
        body = _EMPTY_CONTENT_BLOCK.sub("", body)
    return body


def _artifact_line_rule(line: str) -> str | None:
    value = str(line or "").strip().strip("\u3000").strip()
    if not value or len(value) > 200:
        return None
    if _MEDIA_CAPTION_LINE.fullmatch(value):
        return "media_caption_line"
    if _EDITORIAL_BYLINE_LINE.fullmatch(value):
        return "editorial_byline_line"
    return None


def _strip_editorial_byline_tail(line: str) -> tuple[str, str | None]:
    """Strip a byline glued to the tail of a reporting line.

    Returns ``(line, None)`` when nothing is removed. Otherwise returns the
    line with only the trailing signature removed and the removed tail text.
    The reporting sentence (including its ending punctuation) is preserved.
    """

    original = str(line or "")
    if len(original) > 200:
        return original, None
    match = _EDITORIAL_BYLINE_TAIL.search(original)
    if match is None:
        return original, None
    stripped = original[: match.start()]
    tail = original[match.start():].strip().strip("\u3000").strip()
    # The remaining text must still be a real sentence, not whitespace only.
    if not stripped.strip().strip("\u3000").strip():
        return original, None
    return stripped, tail or None


def _media_artifact_block_replacements(body: str) -> list[tuple[int, int, str, list[dict[str, str]]]]:
    """Locate caption/byline lines and return their block-level replacements."""

    replacements: list[tuple[int, int, str, list[dict[str, str]]]] = []
    for match in _ARTIFACT_TEXT_BLOCK.finditer(body):
        tokens = _ARTIFACT_LINE_SEPARATOR.split(match.group("content"))
        lines = tokens[0::2]
        separators = tokens[1::2]
        removed: list[dict[str, str]] = []
        kept: list[int] = []
        edited_lines: dict[int, str] = {}
        for index, line in enumerate(lines):
            rule = _artifact_line_rule(line)
            if rule is None:
                # The whole line is legitimate, but a byline may be glued to
                # its tail after a sentence-ending mark.  Strip only that tail
                # while keeping the reporting sentence intact.
                stripped, tail = _strip_editorial_byline_tail(line)
                if tail is not None:
                    removed.append({"rule": "editorial_byline_tail", "text": tail[:180]})
                    edited_lines[index] = stripped
                kept.append(index)
                continue
            removed.append({"rule": rule, "text": line.strip()[:180]})
        if not removed:
            continue
        if not kept:
            replacements.append((match.start(), match.end(), "", removed))
            continue
        parts: list[str] = []
        for position, index in enumerate(kept):
            parts.append(edited_lines.get(index, lines[index]))
            if position < len(kept) - 1 and index < len(separators):
                parts.append(separators[index])
        content_start = match.start("content") - match.start()
        content_end = match.end("content") - match.start()
        original = match.group(0)
        replacements.append((
            match.start(),
            match.end(),
            f"{original[:content_start]}{''.join(parts)}{original[content_end:]}",
            removed,
        ))
    return replacements


def find_media_artifact_lines(body_html: str | None) -> list[dict[str, str]]:
    """Report caption/byline lines that deterministic cleanup would remove."""

    body = str(body_html or "")
    if not body or _MEDIA_ARTIFACT_PROBE.search(body) is None:
        return []
    found: list[dict[str, str]] = []
    for _start, _end, _replacement, removed in _media_artifact_block_replacements(body):
        found.extend(removed)
    return found


def remove_media_artifact_lines(body_html: str | None) -> str:
    """Delete template caption and byline lines while keeping every image.

    Only text-only ``p``/``div``/``li`` blocks are inspected, so a block that
    carries markup such as ``img`` is left byte-for-byte unchanged.  Inside a
    matched block a single caption line is removed together with one adjacent
    ``<br>``/newline separator; when every line of the block is an artifact the
    whole block disappears.  The operation is idempotent.
    """

    body = str(body_html or "")
    if not body or _MEDIA_ARTIFACT_PROBE.search(body) is None:
        return body
    replacements = _media_artifact_block_replacements(body)
    if not replacements:
        return body
    parts: list[str] = []
    cursor = 0
    for start, end, replacement, _removed in replacements:
        parts.append(body[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(body[cursor:])
    return "".join(parts)


def preprocess_quality_body(body_html: str | None, *, source: str | None = None) -> str:
    """Remove non-article embeds and feed markers before quality checks.

    This cleanup is deliberately separate from :func:`remove_clickable_links`,
    which is used by persistence and publishing code with an established
    contract.  The operation is idempotent and keeps ordinary markup and
    ``img`` nodes unchanged while dropping executable/embed content.
    """

    body = str(body_html or "")
    if not body:
        return body
    source_code = str(source or "").strip().casefold()
    has_source_template_marker = (
        source_code == "sponichi" and _SPONICHI_TEMPLATE_MARKER.search(body) is not None
    )
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
        or has_source_template_marker
        or _MEDIA_ARTIFACT_PROBE.search(body) is not None
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
    body = remove_media_artifact_lines(body)
    if source_code == "sponichi":
        body = _SPONICHI_TEMPLATE_MARKER.sub("", body)
        body = _SPONICHI_TEMPLATE_LABEL.sub("", body)
    body = remove_empty_content_blocks(body)
    # Remove complete embed containers.  Unclosed tags are intentionally left
    # in place; the quality layer will flag them instead of swallowing article
    # text after a malformed upstream fragment.
    body = _QUALITY_DROP_CONTENT.sub("", body)
    body = _QUALITY_DROP_SELF_CLOSING.sub("", body)
    body = remove_empty_content_blocks(body)
    body = _CLICKABLE_ATTRIBUTE.sub("", body)
    body = _MARKDOWN_LINK.sub("", body)
    return _remove_markdown_reference_links(body)
