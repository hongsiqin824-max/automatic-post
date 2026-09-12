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
    r"|(?:编写|撰文|编撰|编辑|记者|编排|整理|构成|供稿|文|著者)\s*[●•・·:：]",
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
# ``_MEDIA_CAPTION_LINE`` only removes a caption that occupies a whole line
# (``fullmatch``).  Some feeds instead glue a "related video" promo *sentence*
# into a reporting paragraph with no ``<br>``/newline separator, either at the
# block start (``<p>【视频】谷口彰悟制胜球！正文……``) or wedged between two
# reporting sentences (``……又有一名新援离队。 【视频】横滨FM…处子球！ 这名右后卫……``).
# Such a sentence can never occupy a whole line, so the line-level pass cannot
# reach it.  This matcher removes the standalone media-caption *sentence* while
# keeping the reporting around it.  Per the agreed policy a bracket media marker
# (``【视频】``/``【图片】``/``[video]``/…) that OPENS a sentence — i.e. it sits at the
# block start or right after a sentence-ending mark — is a standalone caption and
# is dropped whole.  The one保留 case is a marker used INSIDE a sentence as a
# grammatical part of it (``官方账号发布了【图片】…的照片``): there the marker is
# preceded by ordinary text (not whitespace/block-start nor a sentence mark), so
# the guard below does not match and the sentence is kept.  The boundary class
# excludes ``。！？!?`` so a following reporting sentence is never swallowed, and
# the caption sentence is length-capped.
_INLINE_MEDIA_CAPTION = re.compile(
    r"(?:(?<=[。！？!?])|(?<![^ \t\u3000>]))[ \t\u3000]*(?:"
    r"【\s*(?:图片|写真|视频|集锦|实战|直播|进球|录像|回放|锦集)[^】\r\n]{0,20}】"
    r"|\[\s*(?:图片|写真|photo|video|视频|集锦|直播|进球|录像|回放)[^\]\r\n]{0,20}\]"
    r")[^<>\r\n。！？!?]{0,80}[。！？!?]?[ \t\u3000]*",
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
    r"(?:编写|撰文|编撰|编辑|记者|编排|整理|构成|供稿|文|著者)\s*[●•・·]\s*"
    r"[^<>\r\n。！？!?]{1,40}[ \t\u3000]*$",
    re.IGNORECASE,
)
# Upstream feeds occasionally leak the Dongqiudi highlight-tag syntax into the
# article body, e.g. ``{{c|东京绿茵}可`` or ``{{吉田真信}连``.  The braces are
# never legitimate article characters, so they are stripped before quality
# checks.  A pipe form ``{{X|Y}`` (closing brace optional) keeps the display
# value ``Y``; any remaining stray braces are dropped and the surrounding text
# is preserved.  The probe is used both to gate ``preprocess_quality_body`` and
# to detect that a cleanup pass is required.
_TEMPLATE_RESIDUE_PROBE = re.compile(r"\{\{|\}\}|\{[^{}\r\n]*\||\{|\}")
_TEMPLATE_RESIDUE_PIPE = re.compile(r"\{{1,2}[^{}|\r\n]*\|([^{}\r\n]*?)\}?")
_TEMPLATE_RESIDUE_BRACE = re.compile(r"\{{1,2}|\}{1,2}")
# Upstream Globo/ge feeds glue live-stream promotion into ordinary article
# sentences, e.g. ``…光明球场打响。ge 将实时跟进本场比赛（点击这里）。`` or a
# broadcast line ``转播：ESPN、Disney+（流媒体）和ge实时直播（点击这里）。``.  The
# structural repair layer can only drop whole blocks/lines, so a promo glued
# mid-paragraph survives and forces manual review.  This deterministic pass
# removes only the offending *sentence* while keeping the surrounding report.
#
# A sentence is removed only when it carries BOTH a platform/channel marker
# AND an explicit call-to-action (dual-factor guard), so ordinary sentences
# that merely mention "关注"/"观看" or a brand name are never truncated.
# ``_INLINE_PROMO_PLATFORM`` deliberately includes concrete brand names per the
# agreed policy (口径 A), and a broadcast ``转播：…`` sentence is dropped whole.
_INLINE_PROMO_PLATFORM = (
    r"(?:ge|直播平台|转播|播出|流媒体|频道|客户端|"
    r"disney\+?|espn|hulu|paramount\+?|dazn|amazon|prime\s*video|youtube|twitch|"
    r"premiere|sportv|globoplay|globo|cazetv|"
    r"咪咕|优酷|腾讯体育|爱奇艺|抖音|快手|b站|哔哩哔哩|微博|视频号|公众号|app)"
)
_INLINE_PROMO_ACTION = (
    r"(?:点击(?:这里|查看|进入|观看|了解)?|扫码|扫描二维码|关注|订阅|下载|"
    r"访问|前往|登录|实时(?:跟进|直播|文字直播|更新)|跟进本场|观看直播|收看|"
    r"抢先看|尽在|敬请关注|更多(?:内容|资讯|新闻))"
)
# A single sentence: text up to (and including) a sentence-ending mark.  Kept
# short (<=80 visible chars) so a long ordinary sentence is never swallowed.
_INLINE_PROMO_SENTENCE = re.compile(
    r"[^。！？!?\r\n]*?"
    r"(?:" + _INLINE_PROMO_PLATFORM + r"[^。！？!?\r\n]*?" + _INLINE_PROMO_ACTION
    + r"|" + _INLINE_PROMO_ACTION + r"[^。！？!?\r\n]*?" + _INLINE_PROMO_PLATFORM + r")"
    r"[^。！？!?\r\n]*?[。！？!?]",
    re.IGNORECASE,
)
_INLINE_PROMO_MAX_SENTENCE_CHARS = 80
# Broadcast/schedule lines glue the promo onto the tail of a long info sentence
# that also carries useful date/venue text, e.g.
# ``日期：… 地点：… 转播：ESPN、Disney+（流媒体）和ge实时直播（点击这里）。``.
# Removing the whole sentence would drop the date/venue, so only the broadcast
# tail (from the ``转播/直播平台/播出`` label to the sentence end) is cut when it
# carries a call-to-action.  Per 口径 A the broadcast enumeration itself is
# considered promotion and removed together with the tail.
_INLINE_PROMO_BROADCAST_TAIL = re.compile(
    r"(?:转播|直播平台|播出平台|播出|观看方式|收看方式)\s*[:：][^。！？!?\r\n]*?"
    + _INLINE_PROMO_ACTION
    + r"[^。！？!?\r\n]*?(?=[。！？!?]|$)",
    re.IGNORECASE,
)
# Per 口径 A a *pure broadcast/schedule sentence* — one that only announces where
# a match is shown, with no call-to-action — is still promotion and removed
# whole.  This intentionally relaxes the dual-factor guard, but only for
# sentences that either open with an explicit broadcast label
# (``转播：/直播：/播出：/收看：/观看方式：``) or state ``<平台> … 直播/转播/播出`` as
# their entire content.  A short-sentence cap and a negative guard for factual
# keywords (date/venue/lineup/referee/…) prevent an ordinary report sentence
# that merely mentions a broadcaster from being truncated.
_BROADCAST_LABEL = r"(?:转播|直播|播出|收看|观看方式|收看方式|观看)"
_BROADCAST_VERB = r"(?:现场直播|直播|转播|播出|放送|带来.{0,6}(?:直播|转播)|独家(?:直播|转播))"
# Factual markers that make a sentence carry real news value; if present the
# sentence is protected from the broadcast-sentence rule (it may still lose only
# its promo tail via ``_INLINE_PROMO_BROADCAST_TAIL``).
_BROADCAST_PROTECT = re.compile(
    r"(?:日期|时间|地点|球场|开球|预计首发|首发|缺阵|停赛|伤病|黄牌|红牌|裁判|"
    r"主裁|助理裁判|第四官员|var|积分|排名|名单|回归)",
    re.IGNORECASE,
)
_INLINE_PROMO_BROADCAST_LABEL = re.compile(
    r"[^。！？!?\r\n]*?" + _BROADCAST_LABEL + r"\s*[:：][^。！？!?\r\n]*?[。！？!?]",
    re.IGNORECASE,
)
_INLINE_PROMO_BROADCAST_SENTENCE = re.compile(
    # Clause boundary: start right after a clause separator (，,、；;) or the
    # block start, so a preceding factual clause (e.g. ``这场比赛属于巴甲第27轮``)
    # is never swallowed.  The broadcast clause and its leading separator are
    # removed together; a trailing sentence mark left dangling is cleaned later.
    r"[，,、；;]?"
    r"[^。！？!?，,、；;\r\n]*?"
    + _INLINE_PROMO_PLATFORM
    + r"[^。！？!?，,、；;\r\n]{0,12}?"
    + _BROADCAST_VERB
    + r"[^。！？!?，,、；;\r\n]*?"
    r"(?=[。！？!?]|$)",
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


def _strip_template_residue(body_html: str | None) -> str:
    """Remove leaked highlight-tag braces before quality checks.

    ``{{X|Y}`` (closing brace optional) collapses to the display value ``Y``;
    any remaining ``{{``/``}}``/``{``/``}`` braces are dropped while their
    surrounding text is preserved.  The operation is idempotent because a
    cleaned body no longer contains braces for the probe to match.
    """

    body = str(body_html or "")
    if _TEMPLATE_RESIDUE_PROBE.search(body) is None:
        return body
    body = _TEMPLATE_RESIDUE_PIPE.sub(lambda m: m.group(1), body)
    return _TEMPLATE_RESIDUE_BRACE.sub("", body)


# Only inspect text-only p/div/li blocks; any block carrying markup such as
# ``img`` is left byte-for-byte unchanged so images are never touched.  ``<br>``
# is allowed inside the block so broadcast/schedule lines are still reachable.
_INLINE_PROMO_TEXT_BLOCK = re.compile(
    r"<(?P<tag>p|div|li)\b[^>]*>"
    r"(?P<content>(?:<br\s*/?>|(?!</?(?:p|div|li|img|a)\b)[^<])*)"
    r"</(?P=tag)>",
    re.IGNORECASE,
)


def _strip_inline_promotion_from_text(text: str) -> str:
    """Drop promo sentences glued inside a plain-text block.

    Two passes: first cut a broadcast tail (``转播：…点击这里``) glued to a long
    info sentence while keeping its date/venue text; then remove any remaining
    short sentence that carries both a platform marker and a call-to-action.
    Surrounding reporting sentences (and their punctuation) are preserved.
    """

    def _drop_short_sentence(match: "re.Match[str]") -> str:
        sentence = match.group(0)
        if len(sentence) > _INLINE_PROMO_MAX_SENTENCE_CHARS:
            return sentence
        return ""

    def _drop_broadcast_sentence(match: "re.Match[str]") -> str:
        sentence = match.group(0)
        if len(sentence) > _INLINE_PROMO_MAX_SENTENCE_CHARS:
            return sentence
        # A sentence that also carries factual news value (date/venue/lineup/
        # referee/…) is protected; only its promo tail may be trimmed elsewhere.
        if _BROADCAST_PROTECT.search(sentence):
            return sentence
        return ""

    cleaned = _INLINE_PROMO_BROADCAST_TAIL.sub("", text)
    # 口径 A: drop pure broadcast/schedule sentences (label-led or ``<平台>…直播``)
    # even without a call-to-action, guarding factual info sentences.
    cleaned = _INLINE_PROMO_BROADCAST_LABEL.sub(_drop_broadcast_sentence, cleaned)
    cleaned = _INLINE_PROMO_BROADCAST_SENTENCE.sub(_drop_broadcast_sentence, cleaned)
    cleaned = _INLINE_PROMO_SENTENCE.sub(_drop_short_sentence, cleaned)
    # A ``转播：…`` label left dangling with nothing after it (its tail was cut)
    # is a bare fragment; drop the empty label up to the next boundary.
    cleaned = re.sub(
        r"(?:转播|直播平台|播出平台|播出|观看方式|收看方式)\s*[:：]\s*(?=[。！？!?]|$)",
        "",
        cleaned,
    )
    # A trailing ``<br>`` left dangling before a now-orphaned sentence mark (the
    # promo line after it was removed) is cosmetic noise; drop the separator and
    # the bare punctuation so the info block ends cleanly.
    cleaned = re.sub(r"(?:<br\s*/?>\s*)+[。！？!?]?\s*$", "", cleaned)
    return cleaned


def _strip_inline_promotion(body_html: str | None) -> str:
    """Remove live-stream promotion sentences glued into article text.

    Runs before quality checks so a promo embedded mid-paragraph never has to
    reach the structural repair layer (which can only drop whole blocks/lines).
    Idempotent: a cleaned body no longer matches the promo probe.  A block that
    becomes blank after removal is emptied so ``remove_empty_content_blocks``
    can drop it.
    """

    body = str(body_html or "")
    if (
        _INLINE_PROMO_SENTENCE.search(body) is None
        and _INLINE_PROMO_BROADCAST_TAIL.search(body) is None
        and _INLINE_PROMO_BROADCAST_LABEL.search(body) is None
        and _INLINE_PROMO_BROADCAST_SENTENCE.search(body) is None
    ):
        return body

    def _replace_block(match: "re.Match[str]") -> str:
        content = match.group("content")
        cleaned = _strip_inline_promotion_from_text(content)
        if cleaned == content:
            return match.group(0)
        tag = match.group("tag")
        if not cleaned.strip():
            # Whole block was promotion; leave an empty shell for the
            # downstream empty-block sweeper to remove.
            return f"<{tag}></{tag}>"
        return match.group(0).replace(content, cleaned, 1)

    return _INLINE_PROMO_TEXT_BLOCK.sub(_replace_block, body)


def _strip_inline_media_caption_from_text(text: str) -> str:
    """Drop a "related video" caption sentence glued inside a plain-text block.

    Removes only the sentence that opens with a bracket media marker
    (``【视频】``/``[video]``/…) up to its sentence-ending mark, then collapses a
    doubled separator space left behind so the surrounding report reads cleanly.
    """

    cleaned = _INLINE_MEDIA_CAPTION.sub("", text)
    if cleaned == text:
        return text
    # Removing a mid-paragraph caption can leave two spaces where the caption
    # used to sit between two reporting sentences; collapse them to one.
    cleaned = re.sub(r"(?<=\S)[ \u3000]{2,}(?=\S)", " ", cleaned)
    return cleaned


def _strip_inline_media_caption(body_html: str | None) -> str:
    """Remove related-video caption sentences glued into article text.

    Complements the line-level :func:`remove_media_artifact_lines`, which can
    only drop a caption that occupies a whole line.  Block-scoped so images and
    markup blocks are left untouched; idempotent.
    """

    body = str(body_html or "")
    if _INLINE_MEDIA_CAPTION.search(body) is None:
        return body

    def _replace_block(match: "re.Match[str]") -> str:
        content = match.group("content")
        cleaned = _strip_inline_media_caption_from_text(content)
        if cleaned == content:
            return match.group(0)
        tag = match.group("tag")
        if not cleaned.strip():
            return f"<{tag}></{tag}>"
        return match.group(0).replace(content, cleaned, 1)

    return _INLINE_PROMO_TEXT_BLOCK.sub(_replace_block, body)


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
        or _TEMPLATE_RESIDUE_PROBE.search(body) is not None
        or _INLINE_PROMO_SENTENCE.search(body) is not None
        or _INLINE_PROMO_BROADCAST_TAIL.search(body) is not None
        or _INLINE_PROMO_BROADCAST_LABEL.search(body) is not None
        or _INLINE_PROMO_BROADCAST_SENTENCE.search(body) is not None
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
    body = _strip_inline_media_caption(body)
    body = _strip_template_residue(body)
    body = _strip_inline_promotion(body)
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
