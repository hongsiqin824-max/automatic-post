"""Conservative deterministic repair of common feed artifacts.

The material feed occasionally appends a call-to-action as a normal ``<p>``
element instead of a link.  This module only removes a complete, plain-text
block when its wording is an unambiguous promotion.  Uncertain content is
left untouched and the caller can route it to manual review.  It also exposes
a narrow photo-credit normalizer which preserves the human-readable caption.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any


# Keep this deliberately narrow.  A news paragraph can contain words such as
# "查看" or "赛程" without being an advert; the combination of a call to
# action and an explicit destination is what makes a block eligible.
PROMOTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "standalone_call_to_action",
        re.compile(
            r"^(?:请|立即)?点击(?:查看|进入|打开)?.{0,140}"
            r"(?:完整赛程|赛程详情|比赛详情|赛事详情|更多详情|详情|官网|官方网站|链接|直播间|优惠活动)"
            r"(?:[。！!]*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_call_to_action",
        re.compile(
            r"^(?:请|立即)?(?:扫码|扫描二维码|关注|下载|购买)"
            r".{0,140}(?:获取|下载|购买|关注|完整赛程|赛程|详情|直播|优惠|活动|官网|官方网站|链接).*$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_link_prompt",
        re.compile(
            r"^(?:更多|详细|完整)(?:内容|信息|赛程|资料)?(?:请|可)?(?:点击|查看|进入).{0,140}$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_cooperation_contact",
        re.compile(
            r"^(?:合作|商务|广告|媒体|业务)(?:咨询|洽谈|合作|联系)\s*[:：]?\s*"
            r"(?:[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,24}|"
            r"(?:微信|VX|QQ)\s*[:：]?\s*[A-Z0-9_\-]{4,40})"
            r"(?:[。！!]*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_media_call_to_action",
        re.compile(
            r"^(?:请|立即)?(?:点击|扫码)(?:进入|打开|前往)?"
            r".{0,60}(?:收听|观看).{0,80}"
            r"(?:直播|完整(?:视频|节目)|视频|节目|播客|回放)"
            r"(?:[。！!]*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_branded_podcast_prompt",
        re.compile(
            r"^[+🎧\s]*(?:收听|听听)\s*ge\s*.{0,80}(?:播客|podcast)"
            r"[🎧。！!：:\s]*$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_branded_watch_prompt",
        re.compile(
            r"^[+✅\s]*(?:观看(?:更多)?|收看|在)[:：]?\s*.{0,100}"
            r"(?:ge|globo|sportv).{0,100}"
            r"(?:观看|收看|了解|全部|一切|内容|消息|动态)"
            r"[：:。！!\s]*$",
            re.IGNORECASE,
        ),
    ),
)

_TAIL_ONLY_RULES = {
    "standalone_cooperation_contact",
    "standalone_media_call_to_action",
    "standalone_branded_podcast_prompt",
    "standalone_branded_watch_prompt",
}

_BLOCK_RE = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>(?P<content>[^<>]*)</(?P=tag)\s*>",
    re.IGNORECASE,
)

_EXCLUDED_CONTEXT_RE = re.compile(
    r"<!--.*?(?:-->|$)|"
    r"<(?P<raw_tag>script|style|template|textarea)\b[^>]*>"
    r".*?(?:</(?P=raw_tag)\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)

_TAIL_AFTER_BLOCK_RE = re.compile(
    r"^(?:(?:\s+)|(?:<!--.*?-->)|(?:</(?:article|section|main|div)>))*$",
    re.IGNORECASE | re.DOTALL,
)

_PHOTO_CREDIT_RE = re.compile(
    r"\[\s*照片\s*\]\s*[=＝]\s*(?P<source>[^<>\[\]\r\n]{1,120}?)\s*$",
    re.IGNORECASE,
)

_PHOTOGRAPHER_CREDIT_RE = re.compile(
    r"(?:"
    r"（\s*摄影\s*[:：]\s*(?P<source_zh>[^<>\[\]\r\n]{1,80}?)\s*）"
    r"|\(\s*摄影\s*[:：]\s*(?P<source_ascii>[^<>\[\]\r\n]{1,80}?)\s*\)"
    r"|摄影\s*[:：]\s*(?P<source_bare>[^<>\[\]\r\n]{1,80}?)"
    r")\s*$",
    re.IGNORECASE,
)

_PHOTO_SOURCE_ALLOWED_RE = re.compile(
    r"^[\w\s.·・&＆'’()（）/／、,，_\-]+$",
    re.UNICODE,
)


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _excluded_context_spans(body: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _EXCLUDED_CONTEXT_RE.finditer(body)]


def _inside_excluded_context(start: int, spans: list[tuple[int, int]]) -> bool:
    return any(span_start <= start < span_end for span_start, span_end in spans)


def _rule_for(text: str) -> str | None:
    if not text or len(text) > 180:
        return None
    for name, pattern in PROMOTION_RULES:
        if pattern.fullmatch(text):
            return name
    return None


def _normalised_photo_source(raw_source: str) -> str | None:
    source = re.sub(r"\s+", " ", html.unescape(raw_source or "")).strip()
    if not 1 <= len(source) <= 100:
        return None
    if not any(character.isalnum() for character in source):
        return None
    if not _PHOTO_SOURCE_ALLOWED_RE.fullmatch(source):
        return None
    return source


def _normalised_photographer_source(raw_source: str) -> str | None:
    """Validate a byline more strictly than a feed photo-source marker."""

    source = _normalised_photo_source(raw_source)
    if source is None or len(source) > 60:
        return None
    if re.search(r"[。！？!?；;：:]", source):
        return None
    # A genuine byline is a name or organization, not a sentence following a
    # colon. These predicates deliberately prefer a false negative.
    if re.search(r"(?:表示|认为|报道|介绍|显示|指出|说明|告诉|拍摄了|记录了)", source):
        return None
    if len(source.split()) > 10:
        return None
    return source


def normalize_photo_credits(body_html: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Normalize unambiguous photo-source/byline markers without losing captions.

    Only the plain-text content of a complete ``p``, ``div`` or ``li`` block
    is considered.  A ``[照片]=来源`` or ``摄影：署名`` marker must end the
    block and its source must use a deliberately small character set.
    Attributes, surrounding markup and every image node remain byte-for-byte
    unchanged.
    """

    body = str(body_html or "")
    matches: list[dict[str, Any]] = []
    excluded_spans = _excluded_context_spans(body)

    def replace(block: re.Match[str]) -> str:
        if _inside_excluded_context(block.start(), excluded_spans):
            return block.group(0)
        content = block.group("content")
        credit = _PHOTO_CREDIT_RE.search(content)
        rule = "photo_credit_marker"
        if credit is not None:
            source = _normalised_photo_source(credit.group("source"))
        else:
            credit = _PHOTOGRAPHER_CREDIT_RE.search(content)
            if credit is None:
                return block.group(0)
            raw_source = next(
                (credit.group(name) for name in ("source_zh", "source_ascii", "source_bare")
                 if credit.group(name) is not None),
                "",
            )
            source = _normalised_photographer_source(raw_source)
            rule = "photographer_credit_marker"
        if source is None:
            return block.group(0)
        caption = content[:credit.start()].rstrip()
        if rule == "photographer_credit_marker":
            caption = re.sub(r"\s*/\s*$", "", caption).rstrip()
        replacement = f"{caption}（图片来源：{html.escape(source, quote=False)}）"
        matches.append({
            "rule": rule,
            "tag": block.group("tag").lower(),
            "caption": _plain_text(caption),
            "source": source,
            "before": _plain_text(content),
            "after": _plain_text(replacement),
        })
        prefix_length = block.start("content") - block.start()
        suffix_start = block.end("content") - block.start()
        original = block.group(0)
        return original[:prefix_length] + replacement + original[suffix_start:]

    normalized = _BLOCK_RE.sub(replace, body)
    return normalized, matches


def find_promotional_blocks(body_html: str | None) -> list[dict[str, Any]]:
    """Return removable standalone blocks without changing the body."""

    body = str(body_html or "")
    candidates: list[dict[str, Any]] = []
    excluded_spans = _excluded_context_spans(body)
    for match in _BLOCK_RE.finditer(body):
        if _inside_excluded_context(match.start(), excluded_spans):
            continue
        text = _plain_text(match.group("content"))
        rule = _rule_for(text)
        if rule is None:
            continue
        candidates.append(
            {
                "rule": rule,
                "tag": match.group("tag").lower(),
                "text": text,
                "start": match.start(),
                "end": match.end(),
            }
        )

    # A feed can append more than one promotional block.  Walk backwards so
    # every consecutive recognized block at the article tail is eligible,
    # while the same wording in the middle remains untouched.
    tail_candidate_starts: set[int] = set()
    tail_boundary = len(body)
    for item in reversed(candidates):
        if not _TAIL_AFTER_BLOCK_RE.fullmatch(body[item["end"]:tail_boundary]):
            break
        if item["rule"] in _TAIL_ONLY_RULES:
            tail_candidate_starts.add(int(item["start"]))
        tail_boundary = int(item["start"])

    return [
        item for item in candidates
        if item["rule"] not in _TAIL_ONLY_RULES
        or int(item["start"]) in tail_candidate_starts
    ]


def remove_promotional_blocks(body_html: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Remove all matched blocks in one pass and return details for auditing."""

    body = str(body_html or "")
    matches = find_promotional_blocks(body)
    if not matches:
        return body, []
    spans = {(item["start"], item["end"]) for item in matches}
    cleaned_parts: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        cleaned_parts.append(body[cursor:start])
        cursor = end
    cleaned_parts.append(body[cursor:])
    cleaned = "".join(cleaned_parts)
    return cleaned, matches


class _BodyStatsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.image_count = 0
        self.image_sources: list[str] = []

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data or "").strip()
        if value:
            self.text_parts.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        self.image_count += 1
        source = ""
        for name, value in attrs:
            if str(name or "").lower() == "src":
                source = str(value or "")
                break
        self.image_sources.append(source)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def body_safety_stats(body_html: str | None) -> dict[str, Any]:
    """Return the small set of invariants checked around automatic repair."""

    body = str(body_html or "")
    parser = _BodyStatsParser()
    try:
        parser.feed(body)
        parser.close()
        visible = " ".join(parser.text_parts).strip()
        image_count = parser.image_count
        image_sources = parser.image_sources
        parse_ok = True
    except Exception:
        visible = _plain_text(re.sub(r"<[^>]+>", " ", body))
        image_count = 0
        image_sources = []
        parse_ok = False
    return {
        "body_length": len(body),
        "visible_text_length": len(visible),
        "image_count": image_count,
        "image_sources": image_sources,
        "parse_ok": parse_ok,
    }
