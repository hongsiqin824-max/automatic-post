"""Conservative deterministic repair of common feed artifacts.

The material feed occasionally appends a call-to-action as a normal ``<p>``
element instead of a link.  This module only removes a complete, plain-text
block when its wording is an unambiguous promotion.  Uncertain content is
left untouched and the caller can route it to manual review.  It also exposes
a narrow photo-credit normalizer which preserves the human-readable caption.
"""

from __future__ import annotations

import hashlib
import html
import math
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
        "standalone_channel_call_to_action",
        re.compile(
            r"^(?:请|立即|点击这里)?(?:关注|订阅|加入|进入).{0,80}"
            r"(?:whatsapp|telegram|电报|频道|群组|社群).{0,100}"
            r"(?:获取|接收|查看|观看|最新|全部|内容|资讯|消息|动态|直播|节目|更新)?"
            r"[。！!：:，,、\s]*$",
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
    "standalone_channel_call_to_action",
}

_BLOCK_RE = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>(?P<content>[^<>]*)</(?P=tag)\s*>",
    re.IGNORECASE,
)

# A second, deliberately narrow matcher is used for AI line-level plans.  It
# accepts plain text and ``<br>`` separators, but still rejects rich nested
# markup so that offsets cannot accidentally target a caption or link node.
_LINE_BLOCK_RE = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>(?P<content>(?:[^<>]|<br\b[^>]*>)*?)</(?P=tag)\s*>",
    re.IGNORECASE,
)
_LINE_SEPARATOR_RE = re.compile(r"(?:\r\n|\r|\n|<br\b[^>]*>)", re.IGNORECASE)
_VIDEO_TEASER_RE = re.compile(
    r"^(?:【视频】|\[视频\])\s*[^<>\r\n]{2,170}$",
    re.IGNORECASE,
)

_AI_PLAN_ACTIONS = {"remove_block", "remove_text_line", "replace_text"}

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


def content_blocks(body_html: str | None) -> list[dict[str, Any]]:
    """Expose stable, text-only block references for an AI repair plan.

    The block id is based on document order and is only accepted together with
    exact evidence by :func:`apply_repair_plan`.  This prevents a model from
    selecting a similarly-worded paragraph after the document has changed.
    """

    body = str(body_html or "")
    excluded_spans = _excluded_context_spans(body)
    blocks: list[dict[str, Any]] = []
    for index, match in enumerate(_LINE_BLOCK_RE.finditer(body), start=1):
        if _inside_excluded_context(match.start(), excluded_spans):
            continue
        block: dict[str, Any] = {
            "block_id": f"b{index}",
            "tag": match.group("tag").lower(),
            "text": _plain_text(_LINE_SEPARATOR_RE.sub(" ", match.group("content"))),
            "start": match.start(),
            "end": match.end(),
        }
        block["segments"] = _line_segments(match, block["block_id"])
        blocks.append(block)
    return blocks


def _line_segments(match: re.Match[str], block_id: str) -> list[dict[str, Any]]:
    """Return text-only line spans and their explicit separators.

    Segment offsets point into the original HTML.  The separator metadata is
    kept internal and lets the remover consume exactly one adjacent newline or
    ``<br>`` while preserving the rest of the paragraph byte-for-byte.
    """

    content = match.group("content")
    content_start = match.start("content")
    boundaries = list(_LINE_SEPARATOR_RE.finditer(content))
    segments: list[dict[str, Any]] = []
    cursor = 0
    for line_number, boundary in enumerate(boundaries + [None], start=1):
        end = boundary.start() if boundary is not None else len(content)
        raw = content[cursor:end]
        text = _plain_text(raw)
        if text:
            segments.append({
                "segment_id": f"{block_id}.s{line_number}",
                "block_id": block_id,
                "tag": match.group("tag").lower(),
                "text": text,
                "start": content_start + cursor,
                "end": content_start + end,
                "separator_before": (
                    content_start + boundaries[line_number - 2].start(),
                    content_start + boundaries[line_number - 2].end(),
                ) if line_number > 1 else None,
                "separator_after": (
                    content_start + boundary.start(),
                    content_start + boundary.end(),
                ) if boundary is not None else None,
            })
        if boundary is None:
            break
        cursor = boundary.end()
    return segments


def find_promotional_lines(body_html: str | None) -> list[dict[str, Any]]:
    """Find high-confidence promotional lines inside an otherwise valid block.

    A line is eligible only when the containing block has an explicit newline
    or ``<br>`` boundary.  This is intentionally separate from
    :func:`find_promotional_blocks`, whose deterministic cleanup remains
    limited to complete standalone blocks.
    """

    candidates: list[dict[str, Any]] = []
    for block in content_blocks(body_html):
        segments = block.get("segments") or []
        if len(segments) < 2:
            continue
        for segment in segments:
            text = str(segment.get("text") or "")
            rule = _rule_for(text)
            if rule is None and _VIDEO_TEASER_RE.fullmatch(text):
                # Video teaser markers are only eligible for an AI-validated
                # line plan; deterministic whole-block cleanup must not use
                # this broader rule.
                rule = "standalone_video_teaser"
            if rule is None or text == block.get("text"):
                continue
            candidates.append({
                "rule": rule,
                "tag": str(block.get("tag") or "p"),
                "text": text,
                "segment_id": str(segment["segment_id"]),
                "block_id": str(block["block_id"]),
                "start": int(segment["start"]),
                "end": int(segment["end"]),
            })
    return candidates


def _plan_list(plan: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(plan, dict):
        return [plan], None
    if not isinstance(plan, list):
        return [], "AI 修复计划必须是对象或数组"
    if not all(isinstance(item, dict) for item in plan):
        return [], "AI 修复计划包含无效项目"
    return list(plan), None


def apply_repair_plan(
    body_html: str | None,
    plan: Any,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Apply only an exact, text-block AI plan.

    No HTML is accepted in replacement text.  Every operation must identify a
    known block and include matching evidence, otherwise the complete plan is
    rejected and the original body is returned unchanged.
    """

    body = str(body_html or "")
    plans, plan_error = _plan_list(plan)
    if plan_error:
        return body, [], plan_error
    if not plans:
        return body, [], None
    if len(plans) > 3:
        return body, [], "AI 修复计划超过单次最多3个局部操作"
    blocks = content_blocks(body)
    by_id = {item["block_id"]: item for item in blocks}
    segments_by_id = {
        str(segment["segment_id"]): (block, segment)
        for block in blocks
        for segment in (block.get("segments") or [])
    }
    allowed_promotions = {
        (int(item["start"]), int(item["end"]), str(item["text"]))
        for item in find_promotional_blocks(body)
    }
    allowed_promotion_lines = {
        (int(item["start"]), int(item["end"]), str(item["text"]))
        for item in find_promotional_lines(body)
    }
    _, photo_matches = normalize_photo_credits(body, include_offsets=True)
    allowed_photo_credits = {
        (int(item["start"]), int(item["end"]), str(item["before"])): item
        for item in photo_matches
    }
    operations: list[dict[str, Any]] = []
    for item in plans:
        action = str(item.get("action") or item.get("operation") or "").strip().lower()
        if action not in _AI_PLAN_ACTIONS:
            return body, [], "AI 修复动作不在允许范围内"
        target_id = str(item.get("segment_id") or item.get("block_id") or "").strip().lower()
        block_id = target_id.split(".s", 1)[0]
        block = by_id.get(block_id)
        segment: dict[str, Any] | None = None
        if block is None and ".s" not in target_id:
            return body, [], "AI 修复目标正文块不存在"
        if ".s" in target_id:
            pair = segments_by_id.get(target_id)
            if pair is None:
                return body, [], "AI 修复目标正文行不存在"
            block, segment = pair
            block_id = str(block["block_id"])
        if block is None:
            return body, [], "AI 修复目标正文块不存在"
        evidence = _plain_text(str(item.get("evidence") or item.get("before") or ""))
        expected_text = str(segment["text"] if segment is not None else block["text"])
        if not evidence or evidence != expected_text:
            return body, [], "AI 修复证据与目标正文块不一致"
        raw_confidence = item.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            return body, [], "AI 修复置信度缺失或格式错误"
        confidence = float(raw_confidence)
        if not math.isfinite(confidence) or confidence < 0.85 or confidence > 1:
            return body, [], "AI 修复置信度不足"
        if action == "remove_text_line":
            if segment is None:
                return body, [], "行级修复必须提供 segment_id"
            if len(block.get("segments") or []) < 2:
                return body, [], "行级修复目标缺少明确边界"
            line_identity = (int(segment["start"]), int(segment["end"]), evidence)
            if line_identity not in allowed_promotion_lines:
                return body, [], "AI 修复目标未命中高置信独立推广行"
            same_text_count = sum(
                1
                for candidate_block in blocks
                for candidate in (candidate_block.get("segments") or [])
                if str(candidate.get("text") or "") == evidence
            )
            if same_text_count != 1:
                return body, [], "AI 修复证据在正文中不是唯一独立行"
            remove_start = int(segment["start"])
            remove_end = int(segment["end"])
            following = segment.get("separator_after")
            preceding = segment.get("separator_before")
            if following is not None:
                remove_end = int(following[1])
            elif preceding is not None:
                remove_start = int(preceding[0])
            operations.append({
                "action": action,
                "start": remove_start,
                "end": remove_end,
                "replacement": "",
                "evidence": evidence,
                "item": item,
                "block_id": block_id,
                "segment_id": target_id,
                "tag": str(block.get("tag") or "p"),
            })
            continue

        block_identity = (int(block["start"]), int(block["end"]), evidence)
        if block_identity not in allowed_promotions and block_identity not in allowed_photo_credits:
            return body, [], "AI 修复目标未命中高置信推广或图片署名规则"
        if action == "remove_block":
            if block_identity not in allowed_promotions:
                return body, [], "图片署名只能规范化，不能删除所在正文块"
            replacement = ""
        else:
            photo_match = allowed_photo_credits.get(block_identity)
            if photo_match is None:
                return body, [], "推广内容只能删除，不能由 AI 改写"
            replacement = str(item.get("after") or item.get("replacement") or "")
            if (
                not replacement
                or len(replacement) > 500
                or "<" in replacement
                or ">" in replacement
                or _plain_text(replacement) != _plain_text(photo_match["after"])
            ):
                return body, [], "AI 文本替换内容为空或包含 HTML"
        operations.append({
            "action": action,
            "start": int(block["start"]),
            "end": int(block["end"]),
            "replacement": replacement,
            "evidence": evidence,
            "item": item,
            "block_id": block_id,
            "segment_id": None,
            "tag": str(block.get("tag") or "p"),
        })

    spans = {(int(operation["start"]), int(operation["end"])) for operation in operations}
    if len(spans) != len(operations):
        return body, [], "AI 修复计划重复操作同一正文块"
    cleaned_parts: list[str] = []
    cursor = 0
    applied: list[dict[str, Any]] = []
    previous_end = -1
    for operation in sorted(operations, key=lambda value: int(value["start"])):
        start = int(operation["start"])
        end = int(operation["end"])
        replacement = str(operation["replacement"])
        evidence = str(operation["evidence"])
        item = operation["item"]
        if start < previous_end:
            return body, [], "AI 修复计划操作范围重叠"
        cleaned_parts.append(body[cursor:start])
        original = body[start:end]
        if replacement:
            content_start = original.find(">") + 1
            content_end = original.rfind("<")
            if content_start <= 0 or content_end < content_start:
                return body, [], "AI 修复目标正文块结构异常"
            original = original[:content_start] + replacement + original[content_end:]
        cleaned_parts.append(original if replacement else "")
        cursor = end
        audit_item: dict[str, Any] = {
            "rule": "ai_targeted_repair",
            "action": str(operation["action"]),
            "block_id": str(operation["block_id"]),
            "tag": str(operation["tag"]),
            "text": evidence,
        }
        if operation.get("segment_id"):
            audit_item["segment_id"] = str(operation["segment_id"])
        if replacement:
            audit_item["after"] = replacement
        applied.append(audit_item)
        previous_end = end
    cleaned_parts.append(body[cursor:])
    cleaned = "".join(cleaned_parts)
    if cleaned == body:
        return body, [], "AI 修复计划未改变正文"
    removed_line_characters = sum(
        len(str(operation["evidence"]))
        for operation in operations
        if str(operation["action"]) == "remove_text_line"
    )
    original_visible_length = max(1, body_safety_stats(body)["visible_text_length"])
    if (
        removed_line_characters > 300
        or removed_line_characters / original_visible_length > 0.15
    ):
        return body, [], "AI 行级修复删除比例超过安全上限"
    return cleaned, applied, None


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


def normalize_photo_credits(
    body_html: str | None,
    *,
    include_offsets: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
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
        audit_match: dict[str, Any] = {
            "rule": rule,
            "tag": block.group("tag").lower(),
            "caption": _plain_text(caption),
            "source": source,
            "before": _plain_text(content),
            "after": _plain_text(replacement),
        }
        if include_offsets:
            audit_match.update({"start": block.start(), "end": block.end()})
        matches.append(audit_match)
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
        self.image_attributes: list[list[tuple[str, str]]] = []

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
        self.image_attributes.append([
            (str(name or "").lower(), str(value or "")) for name, value in attrs
        ])

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
        image_attributes = parser.image_attributes
        parse_ok = True
    except Exception:
        visible = _plain_text(re.sub(r"<[^>]+>", " ", body))
        image_count = 0
        image_sources = []
        image_attributes = []
        parse_ok = False
    return {
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_length": len(body),
        "visible_text_length": len(visible),
        "image_count": image_count,
        "image_sources": image_sources,
        "image_attributes": image_attributes,
        "parse_ok": parse_ok,
    }
