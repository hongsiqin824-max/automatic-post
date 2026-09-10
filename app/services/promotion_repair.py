"""Deterministic and AI-planned local repair of common feed artifacts.

The material feed occasionally appends a call-to-action as a normal ``<p>``
element instead of a link.  This module only removes a complete, plain-text
block when its wording is an unambiguous promotion.  AI-planned removals use
exact block or line references instead of a wording allowlist; the caller still
runs structural safeguards and a complete second quality check before commit.
It also exposes a narrow photo-credit normalizer which preserves the
human-readable caption.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from .link_sanitizer import remove_clickable_links, remove_empty_content_blocks


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
            r"^(?:请|立即|欢迎|点击这里|订阅|加入|进入)\s*(?:关注|订阅|加入|进入).{0,80}"
            r"(?:whatsapp|telegram|电报|频道|群组|社群).{0,100}"
            r"(?:获取|接收|查看|观看|最新|全部|内容|资讯|消息|动态|直播|节目|更新)?"
            r"[。！!：:，,、\s]*$",
            re.IGNORECASE,
        ),
    ),
    (
        "standalone_feed_marker",
        re.compile(r"^(?:前文|正文|转会中心\s*[:：])$", re.IGNORECASE),
    ),
    (
        "standalone_more_news_cta",
        re.compile(r"^更多.{0,80}(?:球队)?(?:消息|新闻|资讯|动态|内容)$", re.IGNORECASE),
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
            r"^[+✅\s]*(?:请\s*)?(?:观看(?:更多)?|收看|在)[:：]?\s*.{0,100}"
            r"(?:ge|globo|sportv).{0,100}"
            r"(?:观看|收看|了解|全部|一切|内容|消息|动态)"
            r"[：:。！!\s]*$",
            re.IGNORECASE,
        ),
    ),
)

_TAIL_ONLY_RULES = {
    "standalone_cooperation_contact",
    "standalone_feed_marker",
    "standalone_media_call_to_action",
    "standalone_more_news_cta",
    "standalone_branded_podcast_prompt",
    "standalone_branded_watch_prompt",
    "standalone_channel_call_to_action",
}

# ``_BLOCK_RE`` is intentionally kept text-only for deterministic cleanup.
# Rich formatting wrappers are handled by the AI-plan locator below, where an
# exact evidence check and a second quality pass are required before commit.
_BLOCK_RE = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>(?P<content>[^<>]*)</(?P=tag)\s*>",
    re.IGNORECASE,
)

# A second, deliberately narrow matcher is used for AI line-level plans.  It
# accepts plain text, ``<br>`` separators, and a small set of *attribute-less*
# presentational wrappers. Links, images, and arbitrary nested markup remain
# excluded so an AI plan cannot accidentally target a caption or link node.
_PRESENTATIONAL_TAG = r"(?:b|i|strong|em|span)"
_PRESENTATIONAL_TOKEN = rf"<(?:{_PRESENTATIONAL_TAG}\s*|/{_PRESENTATIONAL_TAG}\s*)>"
_PRESENTATIONAL_TAG_TOKEN_RE = re.compile(
    rf"<(?P<closing>/)?(?P<tag>{_PRESENTATIONAL_TAG})\s*>",
    re.IGNORECASE,
)
_LINE_BLOCK_RE = re.compile(
    rf"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>(?P<content>(?:[^<>]|<br\b[^>]*>|{_PRESENTATIONAL_TOKEN})*?)</(?P=tag)\s*>",
    re.IGNORECASE,
)
_LINE_SEPARATOR_RE = re.compile(r"(?:\r\n|\r|\n|<br\b[^>]*>)", re.IGNORECASE)
_VIDEO_TEASER_RE = re.compile(
    r"^(?:【视频】|【集锦视频】|【实战视频】|【视频集锦】|"
    r"\[视频\]|\[集锦视频\]|\[实战视频\]|\[视频集锦\])\s*[^<>\r\n]{2,170}$",
    re.IGNORECASE,
)
_SCOREBOARD_MARKER_RE = re.compile(
    r"^(?:【积分榜】|\[积分榜\])\s*[^<>\r\n]{2,170}$",
    re.IGNORECASE,
)
_PROGRAM_PROMOTION_RE = re.compile(
    r"^[●•+]\s*.{0,100}(?:节目|播客|podcast).{0,120}"
    r"(?:开播|播出|上线|观看|收听).{0,100}$",
    re.IGNORECASE,
)

_AI_PLAN_ACTIONS = {
    "remove_block",
    "remove_text_line",
    "remove_link",
    "remove_empty_block",
    "replace_text",
}
_AI_PLAN_ACTION_ALIASES = {
    "delete_block": "remove_block",
    "delete_segment": "remove_text_line",
    "delete_link": "remove_link",
    "remove_anchor": "remove_link",
    "delete_empty_block": "remove_empty_block",
    "remove_empty_node": "remove_empty_block",
    "replace_exact_text": "replace_text",
}

# A targeted repair is deliberately more conservative than a deterministic
# feed cleanup.  The complete candidate must still pass the second quality
# check before it can be committed.
MAX_AI_REPAIR_PLANS = 3
MIN_AI_REPAIR_CONFIDENCE = 0.95
MAX_AI_REPAIR_REMOVED_CHARS = 600

# The model is allowed to describe a new promotion wording without waiting
# for a new regular expression.  These are content categories, rather than
# concrete phrases; the exact evidence and structural checks below remain the
# authority for what can actually be removed.
_AI_PROMOTION_ISSUE_TYPES = frozenset({
    "promotion",
    "advertisement",
    "traffic_generation",
    "call_to_action",
    "media_promotion",
    "program_promotion",
    "channel_promotion",
    "external_promotion",
    "schedule_promotion",
    "social_promotion",
    "sponsorship_promotion",
    "video_promotion",
    "引流",
    "推广",
    "广告",
    "广告引流",
    "视频引流",
    "节目推广",
    "频道推广",
    "standalone_program_promotion",
    "standalone_media_promotion",
    "standalone_ad_promotion",
    "standalone_external_promotion",
})
_AI_GENERAL_REMOVAL_ISSUE_TYPES = frozenset({
    "extraneous_content",
    "duplicate_content",
    "template_artifact",
    "format_noise",
    "无关内容",
    "重复内容",
    "模板残留",
    "格式噪声",
})
_AI_REPLACEMENT_ISSUE_TYPES = frozenset({
    "minor_text_defect",
    "format_noise",
    "轻微文本缺陷",
    "格式噪声",
})
_AI_STRUCTURAL_ARTIFACT_RE = re.compile(
    r"^(?:"
    r"【\s*(?:图片|写真|视频|集锦|实战|直播|积分榜|赛程|节目|播客|广告|推荐|相关阅读|更多)"
    r"[^】\r\n]{0,20}】"
    r"|\[\s*(?:图片|写真|photo|video|视频|集锦|直播|积分榜|赛程|节目|podcast)"
    r"[^\]\r\n]{0,20}\]"
    r"|(?:https?://|www\.)\S+\s*$"
    r")|(?:\[[^\]]*(?:照片|写真|photo)[^\]]*\]\s*[=＝])",
    re.IGNORECASE,
)
_AI_MEDIA_ARTIFACT_RE = re.compile(
    r"^(?:"
    r"【\s*(?:图片|写真|视频|集锦|集锦视频|实战视频|视频集锦)\s*】"
    r"|\[\s*(?:图片|写真|photo|video|视频|集锦|集锦视频|实战视频|视频集锦)\s*\]"
    r")\s*[^<>\r\n]{2,170}$",
    re.IGNORECASE,
)
_AI_BRACKETED_ARTIFACT_RE = re.compile(
    r"^(?:【[^】\r\n]{1,24}】|\[[^\]\r\n]{1,24}\])\s*(?P<tail>.*)$",
    re.IGNORECASE,
)
_AI_ARTIFACT_EVIDENCE_CTA_RE = re.compile(
    r"^(?:(?:请|立即|现在|欢迎)\s*)?"
    r"(?:点击|查看|查看更多|查看全部|观看|收看|进入|前往|打开|访问|扫码|"
    r"关注|订阅|下载|收听)|^(?:更多|完整).{0,30}(?:内容|资讯|新闻|赛程|视频|节目|回放)",
    re.IGNORECASE,
)
_NUMERIC_EXPRESSION_RE = re.compile(
    r"[$¥€£₩<>=≤≥≈≠(]*[-+]?\d+(?:[.,]\d+)?(?:[-:/]\d+)?[%‰$¥€£₩)]*"
)
_NUMERIC_DASH_TRANSLATION = str.maketrans({
    "−": "-",
    "﹣": "-",
    "－": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
})
_AI_ARTIFACT_REASON_RE = re.compile(
    r"(?:入口|引流|导流|推广|广告|跳转|外链|占位|"
    r"(?:采集|抓取|解析|模板|格式).{0,12}(?:残留|噪声|错误|异常))",
    re.IGNORECASE,
)
_AI_PROMOTION_REASON_MARKERS = re.compile(
    r"(?:推广|广告|引流|导流|宣传|引导|号召|节目|视频|频道|关注|点击|直播|外部|链接|促销|商业|"
    r"赞助|营销|接收|获取|"
    r"promotion|advert|traffic|media|program|channel|external|sponsor)",
    re.IGNORECASE,
)
_AI_FIXED_MARKER_REASON_MARKERS = re.compile(
    r"(?:入口|引导|引流|导流|榜单|推广|广告|视频|节目|频道|"
    r"与.{0,30}(?:新闻|正文|事实).{0,10}无关)",
    re.IGNORECASE,
)
_AI_STANDALONE_REASON_RE = re.compile(r"(?:独立|单独|额外|入口)", re.IGNORECASE)
_AI_UNRELATED_REASON_RE = re.compile(
    r"(?:无关|不属于|不是(?:新闻|正文|事实)|非(?:新闻|正文|事实))",
    re.IGNORECASE,
)
_AI_REASON_SUBJECT_RE = re.compile(
    r"(?:独立|单独|额外|入口|该段|这段|此段|该块|这块|该内容|这条内容|"
    r"该句|这句|该行|这行|该文字|这部分)",
    re.IGNORECASE,
)

# AI can identify a new wording before a deterministic rule is added.  These
# matchers validate a recognizable call-to-action shape without treating a
# generic sentence containing words such as "观看" or "关注" as an advert.
_AI_EXPLICIT_ACTION_PREFIX_RE = re.compile(
    r"^[+✅📲➡️🗞️\s]*(?:(?:请|立即|现在|欢迎)\s*"
    r"(?:点击|扫码|扫描二维码|关注|订阅|下载|购买|收听|听听|观看|收看|"
    r"进入|前往|打开|访问|查看|查看更多|查看全部)|"
    r"点击这里|扫码|扫描二维码|查看更多|查看全部)",
    re.IGNORECASE,
)
_AI_PROMOTION_ACTION_RE = re.compile(
    r"(?:点击|扫码|扫描二维码|关注|订阅|下载|购买|收听|听听|观看|收看|"
    r"获取|查看|进入|前往|打开|访问|最新|更多|查看更多|查看全部|全部内容|更多内容|尽在|"
    r"优惠|活动)",
    re.IGNORECASE,
)
_AI_PROMOTION_BENEFIT_RE = re.compile(
    r"(?:获取|接收|领取|最新|全部|更多|查看更多|查看全部|尽在|一切|"
    r"完整(?:内容|信息|资讯|新闻|赛程|视频|节目|回放)|"
    r"观看全部|收看全部)",
    re.IGNORECASE,
)
_AI_PROMOTION_DESTINATION_RE = re.compile(
    r"(?:官网|官方网站|网站|链接|频道|群组|社群|平台|whatsapp|telegram|电报|"
    r"播客|podcast|直播间|二维码|应用|app|客户端|小程序|商店)",
    re.IGNORECASE,
)
_AI_EXPLICIT_IN_PREFIX_RE = re.compile(
    r"^[+✅📲➡️🗞️\s]*(?:请|立即|现在|欢迎)\s*在",
    re.IGNORECASE,
)
_AI_EXPLICIT_IN_DESTINATION_RE = re.compile(
    r"(?:官方平台|官方媒体|官方网站|官网|网站|频道|平台|媒体|"
    r"whatsapp|telegram|电报|群组|社群)",
    re.IGNORECASE,
)
_AI_MORE_CONTENT_PROMOTION_RE = re.compile(
    r"^(?:更多|查看更多|查看全部).{0,80}(?:新闻|资讯|消息|动态|内容)\s*[:：]?$",
    re.IGNORECASE,
)
_AI_MEDIA_PLATFORM_RE = re.compile(
    r"(?:^|[\s、，,：:;；|/（）()])(?:ge|globo|sportv)(?![a-z])",
    re.IGNORECASE,
)
_AI_COMMERCIAL_MEDIA_PLATFORM_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:kayo|bein\s+sports|fox\s+sports\s+sportmail|sportmail)(?![A-Za-z])",
    re.IGNORECASE,
)
_AI_COMMERCIAL_BENEFIT_RE = re.compile(
    r"(?:无广告|新用户|订阅|开通|收件箱|第一时间|获取最新|"
    r"观看直播|比赛直播|精彩集锦|集锦和分析)",
    re.IGNORECASE,
)
_AI_EXPLICIT_WATCH_HEADING_RE = re.compile(
    r"^[+✅📲➡️🗞️\s]*(?:观看(?:更多)?|收看)\s*[:：]",
    re.IGNORECASE,
)


def _find_ai_safe_media_blocks(body_html: str | None) -> list[dict[str, Any]]:
    """Return explicit media teaser blocks reserved for an AI-confirmed plan."""

    body = str(body_html or "")
    excluded_spans = _excluded_context_spans(body)
    result: list[dict[str, Any]] = []
    for match in _BLOCK_RE.finditer(body):
        if _inside_excluded_context(match.start(), excluded_spans):
            continue
        text = _plain_text(match.group("content"))
        if _VIDEO_TEASER_RE.fullmatch(text) or _SCOREBOARD_MARKER_RE.fullmatch(text):
            result.append({
                "rule": "standalone_media_marker",
                "tag": match.group("tag").lower(),
                "text": text,
                "start": match.start(),
                "end": match.end(),
            })
    return result


def _is_ai_promotional_evidence(evidence: str) -> bool:
    """Return whether evidence has a standalone promotion-shaped signal.

    The deterministic rules cover known feed templates.  The fallback shape
    check is intentionally compositional: a CTA at the beginning plus a
    destination/benefit marker (or a known media platform) is required.
    """

    text = _normalise_promotion_text(evidence)
    if not text or len(text) > 240:
        return False
    if (
        _rule_for(text)
        or _VIDEO_TEASER_RE.fullmatch(text)
        or _SCOREBOARD_MARKER_RE.fullmatch(text)
        or _PROGRAM_PROMOTION_RE.fullmatch(text)
    ):
        return True
    has_explicit_action_prefix = bool(_AI_EXPLICIT_ACTION_PREFIX_RE.match(text))
    has_explicit_in_prefix = bool(_AI_EXPLICIT_IN_PREFIX_RE.match(text))
    has_watch_heading = bool(_AI_EXPLICIT_WATCH_HEADING_RE.match(text))
    has_more_content_heading = bool(_AI_MORE_CONTENT_PROMOTION_RE.fullmatch(text))
    has_media_platform = bool(_AI_MEDIA_PLATFORM_RE.search(text))
    has_commercial_media_platform = bool(_AI_COMMERCIAL_MEDIA_PLATFORM_RE.search(text))
    if has_commercial_media_platform:
        # Some publisher templates begin directly with a product/platform
        # name (for example ``在Kayo上观看每一场...``) instead of a conventional
        # ``请点击`` CTA. Require both an action and an explicit commercial
        # benefit so a factual sentence merely mentioning the platform is not
        # treated as removable promotion.
        return bool(
            _AI_PROMOTION_ACTION_RE.search(text)
            and _AI_COMMERCIAL_BENEFIT_RE.search(text)
        )
    if not any((
        has_explicit_action_prefix,
        has_explicit_in_prefix,
        has_watch_heading,
        has_more_content_heading,
    )):
        return False
    if not _AI_PROMOTION_ACTION_RE.search(text):
        return False
    if has_explicit_in_prefix:
        return bool(
            (has_media_platform or _AI_EXPLICIT_IN_DESTINATION_RE.search(text))
            and _AI_PROMOTION_BENEFIT_RE.search(text)
        )
    if has_more_content_heading:
        return True
    if has_media_platform:
        return bool(_AI_PROMOTION_BENEFIT_RE.search(text))
    return bool(
        _AI_PROMOTION_DESTINATION_RE.search(text)
        and _AI_PROMOTION_BENEFIT_RE.search(text)
    )


def _is_ai_promotional_plan(
    item: dict[str, Any],
    evidence: str,
    *,
    fixed_marker: bool = False,
) -> bool:
    """Validate the model's category signal for a previously unknown promo.

    Deterministic rules remain a backwards-compatible fallback for existing
    plans.  A new wording must carry an explicit category and a short reason;
    this lets the model expand coverage without granting it permission to
    delete an arbitrary news paragraph.
    """

    raw_issue_type = item.get("issue_type")
    raw_reason = item.get("reason")
    if raw_issue_type is None and raw_reason is None:
        return bool(_rule_for(evidence) or _VIDEO_TEASER_RE.fullmatch(evidence))
    if not isinstance(raw_issue_type, str) or not isinstance(raw_reason, str):
        return False
    issue_type = re.sub(r"[\s-]+", "_", raw_issue_type.strip().lower())
    reason = re.sub(r"\s+", " ", raw_reason).strip()
    if (
        not issue_type
        or len(issue_type) > 80
        or len(reason) < 4
        or len(reason) > 500
        or "<" in reason
        or ">" in reason
    ):
        return False
    if issue_type not in _AI_PROMOTION_ISSUE_TYPES:
        return False
    if fixed_marker:
        # Fixed feed markers (for example the standalone scoreboard entry in
        # article 11501) can use equivalent wording such as "引导访问".  The
        # reason still needs to tie the marker to a promotion unrelated to the
        # article.  The subject can be described as "该段" instead of using a
        # single required adjective such as "独立".
        return bool(
            _AI_UNRELATED_REASON_RE.search(reason)
            and (
                _AI_STANDALONE_REASON_RE.search(reason)
                or _AI_REASON_SUBJECT_RE.search(reason)
            )
        )
    if not _AI_PROMOTION_REASON_MARKERS.search(reason):
        return False
    # A new AI category cannot authorize deletion of an arbitrary news
    # paragraph. It must also carry an explicit standalone promotion shape.
    return bool(
        _is_ai_promotional_evidence(evidence)
        and _AI_UNRELATED_REASON_RE.search(reason)
        and (
            _AI_STANDALONE_REASON_RE.search(reason)
            or _AI_REASON_SUBJECT_RE.search(reason)
        )
    )


def _normalized_issue_type(item: dict[str, Any]) -> str:
    raw = item.get("issue_type") or item.get("issue_code") or ""
    normalized = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
    return {
        "无关内容": "extraneous_content",
        "重复内容": "duplicate_content",
        "模板残留": "template_artifact",
        "格式噪声": "format_noise",
        "轻微文本缺陷": "minor_text_defect",
    }.get(normalized, normalized)


def _has_valid_ai_reason(item: dict[str, Any]) -> bool:
    reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
    return bool(4 <= len(reason) <= 500 and "<" not in reason and ">" not in reason)


def _is_ai_general_removal_plan(item: dict[str, Any]) -> bool:
    """Accept broad issue families while keeping the actual edit structurally bounded."""

    issue_type = _normalized_issue_type(item)
    if issue_type not in _AI_GENERAL_REMOVAL_ISSUE_TYPES or not _has_valid_ai_reason(item):
        return False
    if issue_type == "duplicate_content":
        return True
    evidence = _plain_text(str(item.get("evidence") or item.get("before") or ""))
    if not _AI_STRUCTURAL_ARTIFACT_RE.search(evidence):
        return False
    if evidence.startswith(("【", "[")):
        reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
        if not _AI_ARTIFACT_REASON_RE.search(reason):
            return False
        if _AI_MEDIA_ARTIFACT_RE.fullmatch(evidence):
            return True
        bracketed = _AI_BRACKETED_ARTIFACT_RE.match(evidence)
        return bool(
            bracketed
            and _AI_ARTIFACT_EVIDENCE_CTA_RE.search(bracketed.group("tail").strip())
        )
    return True


def _substantive_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _plain_text(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _numeric_expressions(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", _plain_text(value)).translate(
        _NUMERIC_DASH_TRANSLATION
    )
    return _NUMERIC_EXPRESSION_RE.findall(normalized)

_EXCLUDED_CONTEXT_RE = re.compile(
    r"<!--.*?(?:-->|$)|"
    r"<(?P<raw_tag>script|style|template|textarea|noscript|iframe|embed|object|video|audio|svg)\b[^>]*>"
    r".*?(?:</(?P=raw_tag)\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)

_TAIL_AFTER_BLOCK_RE = re.compile(
    r"^(?:(?:\s+)|(?:<!--.*?-->)|(?:</(?:article|section|main|div)>))*$",
    re.IGNORECASE | re.DOTALL,
)

_PHOTO_CREDIT_RE = re.compile(
    r"\[\s*(?:照片|写真)\s*\]\s*[=＝]\s*(?P<source>[^<>\[\]\r\n]{1,120}?)\s*$",
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
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", without_tags).strip()


def _normalise_promotion_text(value: str | None) -> str:
    """Normalize only the copy used by promotion matchers.

    The original evidence is retained for exact body matching and auditing;
    NFKC here only makes full-width punctuation and compatibility characters
    comparable to the fixed rules.
    """

    return unicodedata.normalize("NFKC", _plain_text(str(value or "")))


def _excluded_context_spans(body: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _EXCLUDED_CONTEXT_RE.finditer(body)]


def _inside_excluded_context(start: int, spans: list[tuple[int, int]]) -> bool:
    return any(span_start <= start < span_end for span_start, span_end in spans)


def _presentational_markup_is_balanced(value: str) -> bool:
    """Reject mismatched attribute-less formatting wrappers before indexing."""

    stack: list[str] = []
    for match in _PRESENTATIONAL_TAG_TOKEN_RE.finditer(value):
        tag = match.group("tag").lower()
        if match.group("closing"):
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack


def _rule_for(text: str) -> str | None:
    normalized = _normalise_promotion_text(text)
    if not normalized or len(normalized) > 180:
        return None
    for name, pattern in PROMOTION_RULES:
        if pattern.fullmatch(normalized):
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
        if not _presentational_markup_is_balanced(match.group("content")):
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


_EMPTY_NODE_RE = re.compile(
    r"<(?P<tag>p|div|li)(?P<attrs>\s[^>]*)?>\s*"
    r"(?:(?:<!--.*?-->\s*)|"
    r"<(?P<fmt>strong|b|span|em|i)\b[^>]*>\s*</(?P=fmt)\s*>\s*)*"
    r"</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


class _AnchorIndexParser(HTMLParser):
    """Collect real closed anchors with source offsets and parsed attributes."""

    def __init__(self, body: str) -> None:
        super().__init__(convert_charrefs=False)
        self.body = body
        self.line_starts = [0]
        for index, value in enumerate(body):
            if value == "\n":
                self.line_starts.append(index + 1)
        self.stack: list[dict[str, Any]] = []
        self.matches: list[dict[str, Any]] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        if line <= 0 or line > len(self.line_starts):
            return len(self.body)
        return min(len(self.body), self.line_starts[line - 1] + column)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        raw_start = self.get_starttag_text() or ""
        start = self._offset()
        self.stack.append({
            "start": start,
            "start_tag_end": start + len(raw_start),
            "href": next(
                (str(value or "") for name, value in attrs if name.lower() == "href" and value),
                "",
            ),
        })

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing anchor has no linked content and is not a repair target.
        return

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self.stack:
            return
        record = self.stack.pop()
        end_start = self._offset()
        closing = re.match(r"</\s*a\s*>", self.body[end_start:], re.IGNORECASE)
        if closing is None or not record.get("href"):
            return
        start = int(record["start"])
        end = end_start + closing.end()
        content_start = int(record.get("start_tag_end") or 0)
        content_end = end_start
        if content_start <= start or content_start > content_end:
            return
        content = self.body[content_start:content_end]
        self.matches.append({
            "start": start,
            "end": end,
            "content_start": content_start,
            "content_end": content_end,
            "href": str(record["href"]),
            "text": _plain_text(content),
            "image_count": len(re.findall(r"<img\b", content, re.IGNORECASE)),
        })


def content_links(body_html: str | None) -> list[dict[str, Any]]:
    """Expose closed anchor nodes as separate, stable repair targets.

    Link targets are intentionally independent from ``block_id`` because a
    link can be nested inside a rich-text paragraph that cannot be safely
    indexed by the text-only block matcher. Only closed anchors are exposed;
    malformed anchors remain fail-closed and are handled by preprocessing.
    """

    body = str(body_html or "")
    excluded_spans = _excluded_context_spans(body)
    links: list[dict[str, Any]] = []
    parser = _AnchorIndexParser(body)
    try:
        parser.feed(body)
        parser.close()
    except (AssertionError, TypeError, ValueError):
        return []
    for index, match in enumerate(sorted(parser.matches, key=lambda item: int(item["start"])), start=1):
        if _inside_excluded_context(int(match["start"]), excluded_spans):
            continue
        links.append({
            "link_id": f"l{index}",
            "tag": "a",
            "text": str(match["text"]),
            "href": str(match["href"]),
            "image_count": int(match["image_count"]),
            "start": int(match["start"]),
            "end": int(match["end"]),
            "content_start": int(match["content_start"]),
            "content_end": int(match["content_end"]),
        })
    return links


def empty_content_blocks(body_html: str | None) -> list[dict[str, Any]]:
    """Expose empty text containers as separate, image-safe targets."""

    body = str(body_html or "")
    excluded_spans = _excluded_context_spans(body)
    result: list[dict[str, Any]] = []
    for index, match in enumerate(_EMPTY_NODE_RE.finditer(body), start=1):
        if _inside_excluded_context(match.start(), excluded_spans):
            continue
        result.append({
            "empty_block_id": f"e{index}",
            "tag": match.group("tag").lower(),
            "text": "",
            "start": match.start(),
            "end": match.end(),
        })
    return result


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
    *,
    _allow_partial: bool = False,
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
    if len(plans) > MAX_AI_REPAIR_PLANS:
        return body, [], f"AI 修复计划超过单次最多{MAX_AI_REPAIR_PLANS}个局部操作"
    if len(plans) > 1 and not _allow_partial:
        # Validate each model item independently first. A malformed item
        # should not discard unrelated safe targets. The combined pass below
        # still rejects overlaps and duplicate keep-target conflicts.
        valid_plans: list[dict[str, Any]] = []
        rejected_errors: list[str] = []
        for item in plans:
            _, _, item_error = apply_repair_plan(
                body,
                item,
                _allow_partial=True,
            )
            if item_error:
                rejected_errors.append(str(item_error))
            else:
                valid_plans.append(item)
        if not valid_plans:
            return body, [], rejected_errors[0] if rejected_errors else "AI 修复计划无可执行项目"
        cleaned, applied, combined_error = apply_repair_plan(
            body,
            valid_plans,
            _allow_partial=True,
        )
        if combined_error:
            return body, [], combined_error
        if applied and rejected_errors:
            applied[0]["skipped_plan_items"] = len(rejected_errors)
            applied[0]["skipped_plan_errors"] = rejected_errors[:3]
        return cleaned, applied, None
    blocks = content_blocks(body)
    by_id = {item["block_id"]: item for item in blocks}
    links = content_links(body)
    links_by_id = {str(item["link_id"]): item for item in links}
    segments_by_id = {
        str(segment["segment_id"]): (block, segment)
        for block in blocks
        for segment in (block.get("segments") or [])
    }
    allowed_promotions = {
        (int(item["start"]), int(item["end"]), str(item["text"]))
        for item in [*find_promotional_blocks(body), *_find_ai_safe_media_blocks(body)]
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
        raw_action = str(item.get("action") or item.get("operation") or "").strip().lower()
        duplicate_action = raw_action == "delete_duplicate"
        if duplicate_action:
            action = "remove_text_line" if item.get("segment_id") else "remove_block"
        else:
            action = _AI_PLAN_ACTION_ALIASES.get(raw_action, raw_action)
        if action not in _AI_PLAN_ACTIONS:
            return body, [], "AI 修复动作不在允许范围内"
        if action == "remove_link":
            link_id = str(item.get("link_id") or "").strip().lower()
            link = links_by_id.get(link_id)
            if link is None:
                return body, [], "AI 修复目标链接不存在"
            evidence = _plain_text(str(item.get("evidence") or item.get("before") or ""))
            expected_link_text = str(link.get("text") or "")
            if evidence != expected_link_text or (not evidence and not int(link.get("image_count") or 0)):
                return body, [], "AI 修复证据与目标链接文字不一致"
            raw_confidence = item.get("confidence")
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                return body, [], "AI 修复置信度缺失或格式错误"
            confidence = float(raw_confidence)
            if not math.isfinite(confidence) or confidence < MIN_AI_REPAIR_CONFIDENCE or confidence > 1:
                return body, [], "AI 修复置信度不足"
            fragment = body[int(link["start"]):int(link["end"])]
            # Reuse the submission boundary's exact anchor semantics: remove
            # linked text and wrapper, while retaining any linked images.
            replacement = remove_clickable_links(fragment)
            operations.append({
                "action": action,
                "start": int(link["start"]),
                "end": int(link["end"]),
                "replacement": replacement,
                "evidence": evidence,
                "item": item,
                "block_id": None,
                "segment_id": None,
                "link_id": link_id,
                "keep_target_id": None,
                "tag": "a",
                "validation": "structured_link_target",
                "whole_node": True,
            })
            continue
        if action == "remove_empty_block":
            empty_id = str(item.get("empty_block_id") or "").strip().lower()
            empty = next(
                (candidate for candidate in empty_content_blocks(body)
                 if str(candidate.get("empty_block_id")) == empty_id),
                None,
            )
            if empty is None:
                return body, [], "AI 修复目标空正文块不存在"
            evidence = _plain_text(str(item.get("evidence") or item.get("before") or ""))
            if evidence:
                return body, [], "AI 修复空正文块证据必须为空"
            raw_confidence = item.get("confidence")
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                return body, [], "AI 修复置信度缺失或格式错误"
            confidence = float(raw_confidence)
            if not math.isfinite(confidence) or confidence < MIN_AI_REPAIR_CONFIDENCE or confidence > 1:
                return body, [], "AI 修复置信度不足"
            operations.append({
                "action": action,
                "start": int(empty["start"]),
                "end": int(empty["end"]),
                "replacement": "",
                "evidence": "",
                "item": item,
                "block_id": None,
                "segment_id": None,
                "empty_block_id": empty_id,
                "keep_target_id": None,
                "tag": str(empty.get("tag") or "p"),
                "validation": "structured_empty_block",
            })
            continue
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
        if (
            not math.isfinite(confidence)
            or confidence < MIN_AI_REPAIR_CONFIDENCE
            or confidence > 1
        ):
            return body, [], "AI 修复置信度不足"
        issue_type = _normalized_issue_type(item)
        keep_target_id: str | None = None
        if duplicate_action and issue_type != "duplicate_content":
            return body, [], "重复删除动作必须使用 duplicate_content 类型"
        if action == "remove_text_line":
            if segment is None:
                return body, [], "行级修复必须提供 segment_id"
            if len(block.get("segments") or []) < 2:
                return body, [], "行级修复目标缺少明确边界"
            line_identity = (int(segment["start"]), int(segment["end"]), evidence)
            known_promotion_line = line_identity in allowed_promotion_lines
            ai_promotion = _is_ai_promotional_plan(
                item, evidence, fixed_marker=known_promotion_line
            )
            ai_general_removal = _is_ai_general_removal_plan(item)
            if issue_type == "duplicate_content":
                keep_id = str(item.get("keep_segment_id") or "").strip().lower()
                keep_pair = segments_by_id.get(keep_id)
                if (
                    keep_pair is None
                    or keep_id == target_id
                    or str(keep_pair[1].get("text") or "") != evidence
                ):
                    return body, [], "重复内容修复缺少有效的保留正文行"
                keep_target_id = keep_id
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
                "keep_target_id": keep_target_id,
                "tag": str(block.get("tag") or "p"),
                "validation": (
                    "fixed_promotion_rule"
                    if known_promotion_line else (
                        "ai_general_issue" if ai_general_removal else (
                            "ai_promotion_category" if ai_promotion else "ai_exact_target"
                        )
                    )
                ),
            })
            continue

        block_identity = (int(block["start"]), int(block["end"]), evidence)
        known_promotion_block = block_identity in allowed_promotions
        ai_promotion = _is_ai_promotional_plan(
            item, evidence, fixed_marker=known_promotion_block
        )
        ai_general_removal = _is_ai_general_removal_plan(item)
        if action == "remove_block":
            if issue_type == "duplicate_content":
                keep_id = str(item.get("keep_block_id") or "").strip().lower()
                keep_block = by_id.get(keep_id)
                if (
                    keep_block is None
                    or keep_id == block_id
                    or str(keep_block.get("text") or "") != evidence
                ):
                    return body, [], "重复内容修复缺少有效的保留正文块"
                keep_target_id = keep_id
            replacement = ""
        else:
            photo_match = allowed_photo_credits.get(block_identity)
            replacement = str(item.get("after") or item.get("replacement") or "")
            if (
                not replacement
                or len(replacement) > 500
                or "<" in replacement
                or ">" in replacement
            ):
                return body, [], "AI 文本替换内容为空或包含 HTML"
            if photo_match is not None:
                if _plain_text(replacement) != _plain_text(photo_match["after"]):
                    return body, [], "图片署名替换内容与规范化结果不一致"
            elif (
                issue_type not in _AI_REPLACEMENT_ISSUE_TYPES
                or not _has_valid_ai_reason(item)
                or replacement == evidence
                or len(block.get("segments") or []) != 1
                or _substantive_text(replacement) != _substantive_text(evidence)
                or _numeric_expressions(replacement) != _numeric_expressions(evidence)
            ):
                return body, [], "AI 文本替换不是可验证的轻微局部修复"
        operations.append({
            "action": action,
            "start": int(block["start"]),
            "end": int(block["end"]),
            "replacement": replacement,
            "evidence": evidence,
            "item": item,
            "block_id": block_id,
            "segment_id": None,
            "keep_target_id": keep_target_id,
            "tag": str(block.get("tag") or "p"),
            "validation": (
                "photo_credit_rule"
                if block_identity in allowed_photo_credits
                else (
                    "fixed_promotion_rule"
                    if known_promotion_block else (
                        "ai_general_issue"
                        if ai_general_removal or issue_type in _AI_REPLACEMENT_ISSUE_TYPES
                        else (
                            "ai_promotion_category" if ai_promotion else "ai_exact_target"
                        )
                    )
                )
            ),
        })

    spans = {(int(operation["start"]), int(operation["end"])) for operation in operations}
    if len(spans) != len(operations):
        return body, [], "AI 修复计划重复操作同一正文块"
    removed_target_ids = {
        str(operation.get("segment_id") or operation.get("block_id") or "")
        for operation in operations
        if operation.get("action") in {"remove_block", "remove_text_line", "remove_link"}
    }
    if any(
        operation.get("keep_target_id") in removed_target_ids
        for operation in operations
        if operation.get("keep_target_id")
    ):
        return body, [], "重复内容修复不能同时删除指定的保留目标"

    removed_visible_chars = sum(
        len(_plain_text(str(operation.get("evidence") or "")))
        for operation in operations
        if operation.get("action") in {"remove_block", "remove_text_line", "remove_link"}
    )
    if removed_visible_chars:
        if removed_visible_chars > MAX_AI_REPAIR_REMOVED_CHARS:
            return body, [], "AI 修复计划删除可见文字超过单次上限"
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
        issue_type = _normalized_issue_type(item)
        if start < previous_end:
            return body, [], "AI 修复计划操作范围重叠"
        cleaned_parts.append(body[cursor:start])
        original = body[start:end]
        if operation.get("whole_node"):
            original = replacement
        elif replacement:
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
            "block_id": str(operation["block_id"] or ""),
            "tag": str(operation["tag"]),
            "text": evidence,
            "validation": str(operation["validation"]),
            "confidence": float(item["confidence"]),
        }
        if operation.get("segment_id"):
            audit_item["segment_id"] = str(operation["segment_id"])
        if operation.get("link_id"):
            audit_item["link_id"] = str(operation["link_id"])
        if operation.get("empty_block_id"):
            audit_item["empty_block_id"] = str(operation["empty_block_id"])
        if issue_type:
            audit_item["issue_type"] = issue_type
        for keep_key in ("keep_block_id", "keep_segment_id"):
            if item.get(keep_key) is not None:
                audit_item[keep_key] = str(item[keep_key])
        if item.get("reason") is not None:
            audit_item["reason"] = str(item["reason"])
        if replacement:
            audit_item["after"] = replacement
        applied.append(audit_item)
        previous_end = end
    cleaned_parts.append(body[cursor:])
    cleaned = "".join(cleaned_parts)
    # Removing a whole link can leave its containing paragraph empty. Prune
    # those containers in the same candidate so a second pass does not see a
    # formatting-only residue. Image-only containers are excluded by the
    # structural matcher and remain intact.
    empty_before_prune = (
        empty_content_blocks(cleaned)
        if any(
            operation.get("action") in {"remove_link", "remove_empty_block"}
            for operation in operations
        )
        else []
    )
    if empty_before_prune:
        cleaned = remove_empty_content_blocks(cleaned)
        for empty in empty_before_prune:
            applied.append({
                "rule": "empty_content_block_cleanup",
                "action": "remove_empty_block",
                "empty_block_id": str(empty["empty_block_id"]),
                "tag": str(empty["tag"]),
                "text": "",
                "validation": "structured_empty_block",
            })
    if cleaned == body:
        return body, [], "AI 修复计划未改变正文"
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
