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
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

from .link_sanitizer import (
    remove_clickable_links,
    remove_empty_content_blocks,
    _strip_template_residue,
)


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
    rf"<(?P<tag>p|div|li|h[1-6])(?P<attrs>\s[^>]*)?>(?P<content>(?:[^<>]|<br\b[^>]*>|{_PRESENTATIONAL_TOKEN})*?)</(?P=tag)\s*>",
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
MAX_AI_REPAIR_PLANS = 5
MIN_AI_REPAIR_CONFIDENCE = 0.95
MAX_AI_REPAIR_REMOVED_CHARS = 600
# 一篇稿子常有多处脏内容，而模型每轮只稳定地看到其中一部分：第一轮删掉引流块，第二轮
# 才报出段尾的图片署名。只要上一轮质检明确给出可执行的局部修复计划，就允许再修一轮，
# 总修复轮数不超过这个上限。每轮都跑同一套逐字校验和累计删除字数上限，最后一轮的正文
# 仍须整体通过质检才提交。
MAX_AI_REPAIR_ROUNDS = 3

# 自动修复规则版本。一次失败的修复并没有改动正文，因此它不该像成功修复那样永久占用
# 这篇文章的唯一一次修复机会——否则校验规则升级后，旧文章永远停在人工审核。幂等因此
# 记录 {正文指纹, 规则版本}：同一份正文在同一规则版本下只尝试一次，规则版本提升后允许
# 再尝试一次。升级校验逻辑时必须同时提升这个数字。
REPAIR_RULE_VERSION = 3

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
    r"关注|订阅|下载|收听)"
    r"|^(?:更多|完整).{0,30}(?:内容|资讯|新闻|赛程|视频|节目|回放)"
    r"|(?:直播(?:结束后)?还?(?:会|将)?提供?回放|提供回放)"
    r"|(?:只要|仅需|注册(?:即|后)?即?可|登录后即可).{0,20}(?:免费)?(?:观看|收看|回看)"
    r"|(?:免费观看|免费收看|随时回看|随时观看)"
    # Media-embed pointer glued to a paragraph, e.g. （见下方视频）/见下图/
    # 详见文末视频/点击下方视频 — a promotion pointer, not a news clause. A
    # locator word (见/详见/点击/下方/文末…) is required so an ordinary news
    # clause that merely mentions 视频/图片 is never matched.
    r"|(?:见|详见|点击|观看|参见)\s*(?:下方|下图|上方|文末|文中|上图|下面|以下|本文)?\s*"
    r"(?:视频|图片|集锦|录像|回放|直播|海报)"
    r"|(?:下方|文末|文中|以下)\s*(?:视频|图片|集锦|录像|回放|直播|海报)",
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


# The shortest removable-duplicate fragment. A prefix shorter than this is too
# ambiguous to treat as a duplicate (for example a single stray character), so
# the plan must stay fail-closed and route to review instead.
_MIN_DUPLICATE_PREFIX_CHARS = 4


def _is_duplicate_keep_target(removed_text: str, keep_text: str) -> bool:
    """Return whether *keep_text* is a valid duplicate-keep for *removed_text*.

    The material feed frequently leaks a standalone fragment that repeats the
    opening of a fuller paragraph (for example ``仙台俱乐部吉祥物“贝伽太”``
    duplicated ahead of ``仙台俱乐部吉祥物“贝伽太”的举动引发争议……``). The two
    blocks are not byte-identical, so requiring exact equality wrongly rejects a
    safe deletion.

    The deletion is only safe when the kept block is at least as complete as the
    removed one: either the two substantive texts are identical, or the removed
    fragment is a prefix of the kept text.  Deleting a *longer* block while
    keeping a shorter prefix would drop the extra content the removed block
    carries, so that direction stays fail-closed.
    """

    removed = _substantive_text(removed_text)
    keep = _substantive_text(keep_text)
    if not removed or not keep:
        return False
    if removed == keep:
        return True
    return len(removed) >= _MIN_DUPLICATE_PREFIX_CHARS and keep.startswith(removed)


def _is_template_residue_cleanup(evidence: str, replacement: str) -> bool:
    """Return ``True`` when the edit only strips leaked highlight-tag braces.

    The deterministic cleanup in :func:`_strip_template_residue` removes the
    upstream ``{{X|Y}`` / stray-brace residue.  When an AI plan proposes exactly
    that cleaned text, the edit is a verifiable, mechanical noise removal even
    though it changes ``_substantive_text`` (e.g. dropping the tag label ``c``)
    or spans multiple segments, so the stricter guards can be relaxed for it.
    """

    if "{" not in evidence and "}" not in evidence:
        return False
    cleaned = _strip_template_residue(evidence)
    return cleaned != evidence and _plain_text(replacement) == _plain_text(cleaned)


# Phase-1 edge-fragment removal (plan B). The AI may strip a dirty fragment
# glued to the *start* or *end* of a reporting paragraph via ``replace_text``.
# The mid-sentence case stays fail-closed: only a leading or trailing fragment
# is accepted so the deletion can never split a sentence into two disjoint
# halves.  The shortest news text that must remain after the strip.
_MIN_EDGE_REMOVAL_REMAINDER_CHARS = 15
# 图注块本身就只有十几个字，用同一个阈值会把一条完全合格的清理计划整条丢弃：
# "阿吉雷成为瓦伦西亚新帅候选人【照片】=Getty Images" 删掉署名后只剩 14 个字，
# 差一个字就转人工。剩余长度阈值防的是"把正文删成残片"，而当被删片段已经逐字
# 命中确定性形态白名单（不含结构化启发式通道）时，剩下多少字不再是安全要素，
# 因此这种情况下下调到与整句删除同一量级的下限。
_MIN_WHITELISTED_REMOVAL_REMAINDER_CHARS = 6
# A removed edge fragment must itself look like template/ad residue — never an
# ordinary reporting clause. This probe recognises the Google ad-section token
# (English or translated), a ``name=s1`` style argument, a bracketed template
# tag, or a standalone call-to-action / promotion shape.
_EDGE_ARTIFACT_FRAGMENT_RE = re.compile(
    r"google(?:_ad_section_(?:start|end)|\s*广告分区\s*(?:开始|结束|開始|結束))"
    r"|\bname\s*=\s*s\d+\b"
    r"|^【[^】\r\n]{1,24}】|^\[[^\]\r\n]{1,24}\]"
    # Japanese feed template residue such as 前文 / 前文リンク / 関連記事 /
    # 続きを読む that is glued to the head or tail of a paragraph.
    r"|^前文(?:\s*リンク)?$|^前文リンク$"
    r"|関連記事|関連リンク|続きを読む|元記事|外部リンク"
    # Chinese-source editor/byline templates glued to paragraph tails, e.g.
    # 编制●足球文摘Web编辑部 / 编成●…编辑部 / 导语链接 / 文末链接.
    r"|[编編]制●.*[编編]辑部$|[编編]成●.*[编編]辑部$|[编編]辑部$"
    # 同类署名还会把编辑部写在中间，例如
    # "FOOTBALL ZONE编辑部・上原拓真 / Takuma Uehara"。端到端锚定、长度上限，
    # 且片段内不得出现句末标点，普通报道句不会命中。
    r"|^[^。！？!?\r\n]{0,40}(?:[编編]辑部|編集部)[^。！？!?\r\n]{0,40}$"
    r"|^导语链接$|^导语$|^正文链接$|^文末链接$|^前言链接$|^前文链接$|^相关链接$|^原文链接$"
    # Media-embed pointers glued to paragraph edges or wedged between sentences,
    # e.g. （见下方视频）/详见文末视频/点击下方视频 — promotion pointers, not news.
    r"|^\s*[（(]\s*见下?方?视频\s*[）)]\s*$|[（(]\s*见下?方?视频\s*[）)]"
    r"|详见(?:文末|下方)(?:视频|图片)|点击(?:下方|文末)(?:视频|图片)"
    # Program viewership call-to-action glued to a paragraph tail, e.g.
    # 这周也要看J！/本周继续收看/下周也别错过节目 — a standalone tune-in prompt,
    # not a news clause. Anchored end-to-end and length-capped so an ordinary
    # reporting sentence that merely contains 看/观看 is never matched.
    r"|^(?:这|本|下|每)(?:周|週)[^。！？!?\r\n]{0,4}"
    r"(?:也|还|继续|接着|记得|别忘了|不要错过|别错过)?[^。！？!?\r\n]{0,4}"
    r"(?:看|收看|观看|追)[^。！？!?\r\n]{0,8}[！!。]?$"
    r"|^(?:敬请期待|千万别错过|不要错过|别错过)[^。！？!?\r\n]{0,12}[！!。]?$"
    # Copyright / agency markers such as (C)TOSHI TAKEYA（SOCCER DIGEST）.
    r"|^\([Cc]\)[^<>。！？!?]{1,80}$"
    # Photographer credits glued to a caption tail, e.g. （Koyo KODAMA/GEKISAKA）
    # — a latin "name/agency" pair wrapped in brackets. Anchored end-to-end and
    # restricted to latin letters so ordinary bracketed notes that happen to
    # contain a slash (（4年级=京都橘高中/C大阪内定）) are never matched.
    r"|^[（(]\s*[A-Za-z][A-Za-z.\-' ]{0,40}/\s*[A-Za-z][A-Za-z.\-' ]{0,40}\s*[）)]$"
    # Explicitly labelled photo credits, e.g. （撮影：山田）/（Photo by AFP）.
    r"|^[（(]\s*(?:撮影|写真|摄影|图片来源|图源|Photo(?:\s+by)?)"
    r"\s*[：:／/]?\s*[^）)\r\n]{0,40}[）)]$"
    # 同样的署名还会不带外层括号、直接以标签开头贴在段尾，例如
    # "图片：金子拓弥（足球文摘摄影部／JMPA代表拍摄）"。上面那条要求整个片段被
    # 括号包住，命中不了这种"标签：内容"形态。端到端锚定、片段内不得出现句末
    # 标点、长度上限 60，普通报道句不会以图片署名标签开头，因此不会误命中。
    r"|^(?:图片来源|图片|照片|图源|摄影|撮影|写真|供图|图)"
    r"\s*[：:=／/]\s*[^。！？!?\r\n]{1,60}$"
    # Reporter contact residue glued to a paragraph tail, e.g.
    # "/reccos23@osen.co.kr" or "记者 hong@news.co.kr" — a byline email left by
    # the source feed. Anchored end-to-end so the fragment must be nothing but
    # an optional byline marker plus one address; an ordinary reporting sentence
    # that merely mentions an email is never matched.
    r"|^[/／|｜·・\-—]?\s*(?:记者|記者|글|文|撰文|报道|報道)?\s*[：:]?\s*"
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,3}"
    r"\s*[。．.！!]?$",
    re.IGNORECASE,
)


def _is_edge_artifact_fragment(fragment: str) -> bool:
    """Return whether a removed leading/trailing fragment is dirt, not news."""

    text = _normalise_promotion_text(fragment)
    if not text:
        return False
    return bool(
        _EDGE_ARTIFACT_FRAGMENT_RE.search(text)
        or _AI_ARTIFACT_EVIDENCE_CTA_RE.search(text)
        or _AI_STRUCTURAL_ARTIFACT_RE.search(text)
        or _is_ai_promotional_evidence(fragment)
    )


# 上面的形态白名单只能覆盖"已经见过"的残留写法，遇到新写法就会 fail-closed 转人工，
# 这是一场打不完的地鼠游戏。下面这条结构化通道换一个抽象层级：不再追问"这个片段是不是
# 我见过的那几种句式"，而是问"这个片段有没有指向一个外部实体或动作"。
#
# 上游残留的共同点是它一定要提到点什么外部的东西——平台名、图库名、社媒账号、网址，
# 或者让读者去点/去关注；而普通的中文叙述句（"下半场再进两球"、"中场休息后节奏明显
# 加快"）永远不会。这个判据是要素级的，新出现的写法只要还在提平台或喊行动就会命中。
#
# 在此之上仍保留三条排除项：片段不能长、不能含独立数字（比分、分钟数、日期），也不能
# 含引语——这些都是新闻载荷。加上调用方强制的可删除问题类别、有效理由和置信度下限，
# 以及修完必跑的完整质检，判断错了会被拦下来而不是发出去。
_MAX_STRUCTURAL_REMOVAL_CHARS = 60
# 品牌名里的数字（SPORT1、beIN）紧贴拉丁字母，不是事实数字；独立出现的数字才可能是
# 比分、分钟数或日期，这种片段一律不走结构化通道。
_FACTUAL_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?![A-Za-z])")
# 直接引语属于新闻载荷。书名号《》是作品名标记而非引语，不计入。
_QUOTED_SPEECH_RE = re.compile(r"[“”\"「」『』]")
# 拉丁字母串：中文体育正文里它几乎只来自平台名、图库名、账号或网址。
_LATIN_RUN_RE = re.compile(r"[A-Za-z]{2,}")
# 整个片段被括号包住——标注和署名的典型外形。
_BRACKETED_FRAGMENT_RE = re.compile(r"^[（(\[【][^）)\]】]*[）)\]】][。．.]?$")
# 括号标注里的来源类词，覆盖"（供图：X）"这类不含拉丁字母的署名。
_SOURCE_LABEL_RE = re.compile(
    r"供图|供图片|摄影|撮影|拍摄|图源|图片来源|视频来源|来源|版权|署名"
    r"|photo|credit|courtesy",
    re.IGNORECASE,
)
# 片段以拉丁串开头或结尾——"…庆祝胜利。Instagram/@x"、"…奖杯。Getty" 这类裸署名。
_LATIN_EDGE_RE = re.compile(r"^[A-Za-z]|[A-Za-z][。．.]?$")
# 引流/推广动作词。单独出现不足以删除（"想观看的球迷可以通过多个平台收看直播"没点名
# 任何平台，无法核实），必须和拉丁串同时出现才算命中。
_PROMOTION_VERB_RE = re.compile(
    r"点击|关注|订阅|下载|扫码|扫描|转播|独播|播出|直播|收看|观看|跟踪|介绍|进入|前往"
    r"|watch|follow|subscribe|download|click"
)


def _points_to_external_entity(text: str) -> bool:
    """Return whether *text* references an outside platform, account or action."""

    bracketed = bool(_BRACKETED_FRAGMENT_RE.match(text))
    if bracketed and _SOURCE_LABEL_RE.search(text):
        return True
    if not _LATIN_RUN_RE.search(text):
        return False
    return bool(
        bracketed
        or _LATIN_EDGE_RE.search(text)
        or _PROMOTION_VERB_RE.search(text)
    )


def _is_structurally_safe_removal(fragment: str) -> bool:
    """Return whether *fragment* can be deleted on structure alone."""

    text = _normalise_promotion_text(fragment)
    if not text or len(text) > _MAX_STRUCTURAL_REMOVAL_CHARS:
        return False
    if _QUOTED_SPEECH_RE.search(text) or _FACTUAL_NUMBER_RE.search(text):
        return False
    return _points_to_external_entity(text)


def _is_removable_fragment(fragment: str) -> bool:
    """Return whether a removed fragment is safe to drop.

    The shape whitelist is the fast path; the structural gate handles residue
    shapes nobody has seen yet.
    """

    return _is_edge_artifact_fragment(fragment) or _is_structurally_safe_removal(fragment)


def _edge_fragment_removal(
    evidence: str,
    replacement: str,
    *,
    require_whitelist: bool = True,
) -> str | None:
    """Return the removed edge fragment(s) when *replacement* strips dirty ends.

    The kept text (``replacement``) must equal ``evidence`` with a leading
    fragment, a trailing fragment, or both removed — i.e. it is a contiguous
    prefix, suffix, or infix of the evidence.  The remaining news text must stay
    substantial.  Returns the removed fragment text on success (both sides joined
    by a space when two ends are stripped) or ``None`` when the edit is not a safe
    edge removal.

    With *require_whitelist* (the default) every removed side must also look
    like known template/ad residue.  Callers that verify information loss
    independently pass ``require_whitelist=False`` to get the shape check only:
    the edit is still proven to be a verbatim deletion, but what the fragment
    *means* is decided outside this function.
    """

    original = _plain_text(evidence)
    kept = _plain_text(replacement)
    if not original or not kept or kept == original or len(kept) >= len(original):
        return None
    # ``kept`` must be a contiguous prefix, suffix, or infix of ``original`` so
    # the remaining sentence is never spliced from two non-adjacent parts and
    # every kept character (score/date digits included) comes verbatim from the
    # original.
    index = original.find(kept)
    if index < 0:
        return None
    leading = original[:index]
    trailing = original[index + len(kept):]
    if not leading.strip() and not trailing.strip():
        return None
    removed_parts: list[str] = []
    for side in (leading, trailing):
        core = side.strip()
        if not core:
            continue
        if require_whitelist and not _is_removable_fragment(core):
            return None
        removed_parts.append(core)
    if not removed_parts:
        return None
    # 只有逐字命中确定性形态白名单时才放宽剩余长度下限；结构化通道是启发式判断，
    # 不足以支撑把正文删到只剩几个字，仍走 15 字的严格下限。
    whitelisted = all(_is_edge_artifact_fragment(part) for part in removed_parts)
    minimum = (
        _MIN_WHITELISTED_REMOVAL_REMAINDER_CHARS
        if whitelisted
        else _MIN_EDGE_REMOVAL_REMAINDER_CHARS
    )
    if len(kept.strip()) < minimum:
        return None
    return " ".join(removed_parts)


# Sentence terminators used to split a paragraph into complete sentences for
# the mid-paragraph whole-sentence removal below.  Whitespace is not a
# terminator: a Chinese news sentence ends with 。！？ etc., so a
# whitespace-only split would allow half a sentence to be deleted.
_SENTENCE_SEPARATOR_RE = re.compile(r"([。！？!?])")
# A removed mid-paragraph sentence must be at least this long to be considered
# a self-contained promotional sentence rather than a stray punctuation mark.
_MIN_WHOLE_SENTENCE_CHARS = 6


def _whole_sentence_removal(
    evidence: str,
    replacement: str,
    *,
    require_whitelist: bool = True,
) -> str | None:
    """Return removed whole sentences when *replacement* strips dirt sentences.

    An AI ``replace_text`` plan may delete one or more *complete* sentences from
    the middle of a paragraph.  The remaining sentences must keep their original
    order and be joined exactly as they appear in the evidence, so no character
    is rewritten and no sentence is spliced.  Returns the removed sentence text
    joined by a space, or ``None``.

    With *require_whitelist* every removed sentence must additionally look like
    a known promotion / template artefact (the ``_is_removable_fragment``
    probe).  Callers that verify information loss independently pass
    ``require_whitelist=False``; the deletion shape is still proven here.
    """

    original = _plain_text(evidence)
    kept = _plain_text(replacement)
    if not original or not kept or kept == original or len(kept) >= len(original):
        return None

    # Split into sentences while keeping the terminators attached.
    parts = _SENTENCE_SEPARATOR_RE.split(original)
    sentences: list[str] = []
    for index in range(0, len(parts), 2):
        sentence = parts[index]
        terminator = parts[index + 1] if index + 1 < len(parts) else ""
        if sentence or terminator:
            sentences.append(sentence + terminator)
    if len(sentences) < 2:
        return None

    removed_indices: list[int] = []
    # Greedy two-pointer walk: match kept text against the sentence sequence
    # so the kept order is identical to the evidence order.
    kept_target = kept.strip()
    cursor = 0
    for index, sentence in enumerate(sentences):
        stripped = sentence.strip()
        if not stripped:
            continue
        if kept_target[cursor:cursor + len(stripped)] == stripped:
            cursor += len(stripped)
            while cursor < len(kept_target) and kept_target[cursor].isspace():
                cursor += 1
        else:
            removed_indices.append(index)
    if not removed_indices or cursor != len(kept_target):
        # Either nothing was removed, or the kept text does not correspond to
        # the remaining sentences verbatim (a rewrite, not a deletion).
        return None

    removed_sentences: list[str] = []
    for index in removed_indices:
        sentence = sentences[index].strip()
        if len(sentence) < _MIN_WHOLE_SENTENCE_CHARS:
            return None
        if require_whitelist and not _is_removable_fragment(sentence):
            return None
        removed_sentences.append(sentence)
    if not removed_sentences:
        return None
    # The kept text must remain substantial so the paragraph never collapses
    # into a stub.
    if len(kept_target) < _MIN_EDGE_REMOVAL_REMAINDER_CHARS:
        return None
    return " ".join(removed_sentences)


# A removed infix fragment (a promotion pointer wedged between two kept
# sentences, e.g. "…夺冠。（见下方视频）不过，这场…") must be at least this long so
# a single stray character can never be silently dropped.
_MIN_INFIX_REMOVAL_CHARS = 4


def _infix_fragment_removal(
    evidence: str,
    replacement: str,
    *,
    require_whitelist: bool = True,
) -> str | None:
    """Return the removed middle fragment when *replacement* drops one infix.

    A media pointer such as "（见下方视频）" is sometimes glued *between* two
    complete sentences rather than at a paragraph edge, so ``replacement`` keeps
    a contiguous prefix and a contiguous suffix of ``evidence`` with exactly one
    fragment deleted from the middle.  The kept prefix and suffix must appear
    verbatim and adjacent in ``evidence`` (no character is rewritten) and the
    remaining news text must stay substantial.  Returns the removed fragment or
    ``None``.

    With *require_whitelist* the removed fragment must also look like known
    template/ad residue; independent verification callers pass
    ``require_whitelist=False``.
    """

    original = _plain_text(evidence)
    kept = _plain_text(replacement)
    if not original or not kept or kept == original or len(kept) >= len(original):
        return None
    # Find the longest common prefix and suffix; whatever sits between them in
    # the original is the single removed infix.
    prefix_len = 0
    max_prefix = min(len(original), len(kept))
    while prefix_len < max_prefix and original[prefix_len] == kept[prefix_len]:
        prefix_len += 1
    suffix_len = 0
    max_suffix = min(len(original) - prefix_len, len(kept) - prefix_len)
    while (
        suffix_len < max_suffix
        and original[len(original) - 1 - suffix_len] == kept[len(kept) - 1 - suffix_len]
    ):
        suffix_len += 1
    # The kept text must be exactly prefix + suffix (a single contiguous cut).
    if prefix_len + suffix_len != len(kept):
        return None
    removed = original[prefix_len:len(original) - suffix_len].strip()
    if len(removed) < _MIN_INFIX_REMOVAL_CHARS:
        return None
    # Both kept sides must be non-empty: an edge removal is handled elsewhere.
    if not original[:prefix_len].strip() or not original[len(original) - suffix_len:].strip():
        return None
    if len(kept.strip()) < _MIN_EDGE_REMOVAL_REMAINDER_CHARS:
        return None
    if require_whitelist and not _is_removable_fragment(removed):
        return None
    return removed


def delete_only_replacement(
    evidence: str,
    replacement: str,
    *,
    require_whitelist: bool = False,
) -> tuple[str | None, str | None]:
    """Return ``(removed_fragment, shape)`` for a verbatim-deletion replacement.

    A single entry point for the three deletion shapes a ``replace_text`` plan
    can take: stripping a paragraph edge, deleting whole interior sentences, or
    cutting one fragment wedged between two kept sentences.  All three guarantee
    the kept text is assembled verbatim from the evidence, so the edit provably
    adds and rewrites nothing.  Returns ``(None, None)`` when the replacement is
    not a pure deletion.
    """

    for detector, shape in (
        (_edge_fragment_removal, "ai_edge_fragment_removal"),
        (_whole_sentence_removal, "ai_whole_sentence_removal"),
        (_infix_fragment_removal, "ai_infix_fragment_removal"),
    ):
        removed = detector(evidence, replacement, require_whitelist=require_whitelist)
        if removed is not None:
            return removed, shape
    return None, None


def plans_from_dirty_targets(
    body_html: str | None,
    targets: Any,
) -> list[dict[str, Any]]:
    """Build bounded delete-only plans from verbatim dirt locations.

    The quality model reliably says *where* the dirt is but often declines to
    also emit a repair plan ("无法定位" even when the block is right there in
    the prompt), which used to send the article straight to review.  Deriving
    the plan here removes that dependency: the action is chosen mechanically
    from how the quoted text sits in the document, and the result still has to
    pass :func:`apply_repair_plan`'s verbatim validation plus the caller's
    information-preservation verifier.
    """

    if isinstance(targets, dict):
        targets = [targets]
    if not isinstance(targets, list) or not targets:
        return []
    body = str(body_html or "")
    blocks = content_blocks(body)
    by_block = {str(item["block_id"]): item for item in blocks}
    by_segment = {
        str(segment["segment_id"]): (item, segment)
        for item in blocks
        for segment in (item.get("segments") or [])
    }
    plans: list[dict[str, Any]] = []
    # 同一块上可能有多条脏内容目标。逐条生成会产出证据相同或范围重叠的计划，被
    # apply_repair_plan 整份否决，所以先按块归并：整块删除优先，其次是块内不同行，
    # 最后把同一块内的多个片段合并成一条 replace_text 一次性删掉。
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for target in targets[:MAX_AI_REPAIR_PLANS]:
        if not isinstance(target, dict):
            continue
        evidence = _plain_text(str(target.get("evidence") or ""))
        if not evidence:
            continue
        segment_id = str(target.get("segment_id") or "").strip().lower()
        block_id = str(target.get("block_id") or "").strip().lower()
        pair = by_segment.get(segment_id)
        block = None
        if pair is not None and evidence in pair[1]["text"]:
            block = pair[0]
        elif block_id in by_block and evidence in by_block[block_id]["text"]:
            block = by_block[block_id]
        else:
            block = next((item for item in blocks if evidence in item["text"]), None)
        if block is None:
            continue
        key = str(block["block_id"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append({
            "target": target,
            "evidence": evidence,
            "block": block,
            "segment": pair[1] if pair is not None and block is pair[0] else None,
        })

    for key in order:
        items = grouped[key]
        block = items[0]["block"]
        block_text = str(block["text"])
        segments = block.get("segments") or []
        base = _synthesized_plan_base(items[0]["target"])
        if any(item["evidence"] == block_text for item in items):
            plans.append({
                **base,
                "block_id": key,
                "action": "remove_block",
                "evidence": block_text,
            })
            continue
        line_items = [
            item for item in items
            if item["segment"] is not None and item["evidence"] == item["segment"]["text"]
        ]
        if len(segments) >= 2 and len(line_items) == len(items):
            seen_segments: set[str] = set()
            for item in line_items:
                segment_id = str(item["segment"]["segment_id"])
                if segment_id in seen_segments:
                    continue
                seen_segments.add(segment_id)
                plans.append({
                    **_synthesized_plan_base(item["target"]),
                    "block_id": key,
                    "segment_id": segment_id,
                    "action": "remove_text_line",
                    "evidence": item["evidence"],
                })
            continue
        remainder = block_text
        removed_any = False
        for evidence in sorted({item["evidence"] for item in items}, key=len, reverse=True):
            if evidence in remainder:
                remainder = remainder.replace(evidence, "", 1)
                removed_any = True
        if not removed_any or not remainder.strip():
            continue
        plans.append({
            **base,
            "block_id": key,
            "action": "replace_text",
            "evidence": block_text,
            "after": remainder,
        })
    return plans


def _synthesized_plan_base(target: dict[str, Any]) -> dict[str, Any]:
    """Shared plan fields derived from one dirt target."""

    issue_type = _normalized_issue_type(target)
    if issue_type not in (_AI_GENERAL_REMOVAL_ISSUE_TYPES | _AI_PROMOTION_ISSUE_TYPES):
        issue_type = "extraneous_content"
    reason = re.sub(r"\s+", " ", str(target.get("reason") or "")).strip()
    if not 4 <= len(reason) <= 500:
        reason = f"质检定位为可删除的{issue_type}内容，删除后不影响新闻事实"
    return {
        "issue_type": issue_type,
        "reason": reason,
        "confidence": MIN_AI_REPAIR_CONFIDENCE,
        "synthesized_from_dirty_target": True,
    }


# 形态词典是为"段首段尾一小截残留"设计的，其中若干模式故意不锚定（例如"观看…直播"、
# "点击…视频"），这在短片段上是安全的，用到整段新闻上就会误命中：一条提到"观看直播"
# 的新闻导语会被当成引流残留。合成计划的删除目标可能是整块，因此免检只对短片段开放，
# 超过这个长度一律交给信息保全核验裁决。
MAX_FAST_PATH_REMOVAL_CHARS = 80


def removal_matches_known_artifact(text: str) -> bool:
    """Return whether a *short* fragment matches a deterministic residue shape.

    The public fast path in front of the information-preservation verifier: a
    fragment the fixed rules already recognise needs no model call.  A miss is
    not a verdict — it only means the decision moves to the verifier.  Long
    targets never qualify because the shape rules are not anchored and would
    misfire on ordinary reporting text.
    """

    value = _normalise_promotion_text(text)
    if not value or len(value) > MAX_FAST_PATH_REMOVAL_CHARS:
        return False
    return _is_removable_fragment(value)


def removal_is_duplicated(removed_text: str, body_html: str | None) -> bool:
    """Return whether *removed_text* still exists elsewhere in the body.

    The mechanical half of the information-preservation check: when the exact
    substantive text appears at least twice in the article, deleting one copy
    cannot lose a fact, so no model call is needed to authorise it.
    """

    fragment = _substantive_text(removed_text)
    if len(fragment) < _MIN_DUPLICATE_PREFIX_CHARS:
        return False
    return _substantive_text(_plain_text(str(body_html or ""))).count(fragment) >= 2


# A plain-text URL glued to a platform/label, e.g. "关西电视台DOGA：https://…" or
# "TVer：https://tver.jp/…". The model often mislabels these standalone lines as
# links (action=remove_link) even though the source has no <a> anchor, so the
# structured link lookup finds nothing. We only downgrade to a text removal when
# the segment is unambiguously such a URL line, never for a factual sentence.
_PLAINTEXT_URL_LINE_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _link_plan_fallback_segment_id(
    evidence: str,
    segments_by_id: dict[str, Any],
) -> str | None:
    """Locate the segment a failed ``remove_link`` plan really targets.

    When the source has no ``<a>`` anchor the model still sometimes emits
    ``remove_link`` for a plain-text URL line (e.g. ``关西电视台DOGA：https://…``)
    with ``evidence`` holding only the platform label. Return the ``segment_id``
    of the single segment that both contains that label *and* is a standalone
    URL line, so the caller can downgrade the plan to ``remove_text_line``.
    Returns ``None`` when the target is ambiguous or is not a URL line.
    """

    probe = _plain_text(evidence)
    if not probe:
        return None
    matches: list[str] = []
    for segment_id, pair in segments_by_id.items():
        segment = pair[1]
        segment_text = _plain_text(str(segment.get("text") or ""))
        if not segment_text or probe not in segment_text:
            continue
        # The segment must be a bare URL line: the label plus a URL and nothing
        # resembling a news clause, so a factual sentence is never deleted.
        if not _PLAINTEXT_URL_LINE_RE.search(segment_text):
            continue
        remainder = _PLAINTEXT_URL_LINE_RE.sub("", segment_text)
        remainder = remainder.replace(probe, "").strip(" ：:·、，,。.-—　")
        if remainder:
            continue
        matches.append(segment_id)
    if len(matches) != 1:
        return None
    return matches[0]


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
    r"\[\s*(?:照片|写真|图片)\s*\]\s*[=＝]\s*(?P<source>[^<>\[\]\r\n]{1,120}?)\s*$",
    re.IGNORECASE,
)

_PHOTOGRAPHER_CREDIT_RE = re.compile(
    r"(?:"
    r"（\s*摄影\s*[:：]\s*(?P<source_zh>[^<>\[\]\r\n]{1,80}?)\s*）"
    r"|\(\s*摄影\s*[:：]\s*(?P<source_ascii>[^<>\[\]\r\n]{1,80}?)\s*\)"
    r"|摄影\s*[:：]\s*(?P<source_bare>[^<>\[\]\r\n]{1,80}?)"
    r"|\(\s*[Cc]\s*\)\s*(?P<source_copyright>[^<>\[\]\r\n]{1,80}?)\s*$"
    r"|【\s*(?:照片|写真|图片)\s*[:：]\s*(?P<source_bracket>[^<>【】\[\]\r\n]{1,80}?)\s*】"
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


def _merge_consecutive_removal_operations(
    operations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并首尾相接或重叠的连续删除操作。

    当 AI 标记删除连续的多行推广内容时，每行可能都会吞掉自己的分隔符，
    导致操作区间首尾相接或轻微重叠。这不是真正的"意图重叠"，而是同一
    删除意图的多个表达。合并成单一操作可以：
    1. 避免触发"操作范围重叠"误报
    2. 统一校验删除字数上限
    3. 保持安全检查的完整性

    重要约束：
    - 只合并 issue_type 非 duplicate_content 的删除操作
    - duplicate_content 涉及"删一份保留一份"，合并会导致保留目标丢失
    - 只合并同一个块内的多行删除（remove_text_line），不合并不同块的删除
    - 不同块的删除应保持独立记录，以便审计和追踪
    """

    # 分离删除操作和其他操作
    removal_ops = []
    other_ops = []

    for op in operations:
        if op.get("action") in {"remove_block", "remove_text_line", "remove_link"}:
            removal_ops.append(op)
        else:
            other_ops.append(op)

    # 如果删除操作少于 2 个，无需合并
    if len(removal_ops) <= 1:
        return operations

    # 按 start 位置排序
    removal_ops.sort(key=lambda x: int(x["start"]))

    # 合并首尾相接或重叠的删除操作（仅限同一块内的行级删除）
    merged = []
    current = removal_ops[0]

    for next_op in removal_ops[1:]:
        current_start = int(current["start"])
        current_end = int(current["end"])
        next_start = int(next_op["start"])
        next_end = int(next_op["end"])

        # 获取 issue_type
        current_issue_type = _normalized_issue_type(current.get("item", {}))
        next_issue_type = _normalized_issue_type(next_op.get("item", {}))

        # 获取 block_id 和 action
        current_block_id = current.get("block_id")
        next_block_id = next_op.get("block_id")
        current_action = current.get("action")
        next_action = next_op.get("action")

        # 只有在以下条件全部满足时才合并：
        # 1. 操作区间相接或重叠
        # 2. 两个操作都不是 duplicate_content 类型
        # 3. 两个操作都是行级删除（remove_text_line）
        # 4. 两个操作在同一个块内（block_id 相同）
        can_merge = (
            next_start <= current_end
            and current_issue_type != "duplicate_content"
            and next_issue_type != "duplicate_content"
            and current_action == "remove_text_line"
            and next_action == "remove_text_line"
            and current_block_id == next_block_id
        )

        if can_merge:
            # 合并区间：start 取最小，end 取最大
            merged_start = min(current_start, next_start)
            merged_end = max(current_end, next_end)

            # 合并 evidence（用于字数统计）
            current_evidence = str(current.get("evidence") or "")
            next_evidence = str(next_op.get("evidence") or "")
            merged_evidence = current_evidence + next_evidence

            # 创建合并后的操作
            current = {
                **current,  # 保留第一个操作的大部分字段
                "start": merged_start,
                "end": merged_end,
                "evidence": merged_evidence,
                # 保持 replacement 为空（删除操作）
                "replacement": "",
            }
        else:
            # 不能合并，保存当前操作
            merged.append(current)
            current = next_op

    # 添加最后一个操作
    merged.append(current)

    # 返回合并后的删除操作 + 其他操作
    return merged + other_ops


def _relocate_plan_by_evidence(
    body: str,
    item: dict[str, Any],
    error: str,
) -> dict[str, Any] | None:
    """Re-target a plan whose block ids drifted after a body change.

    The quality preprocessing step can remove a paragraph before the AI plan
    is applied, which shifts every later ``block_id``/``segment_id``.  When the
    failure is purely positional (missing target / mismatched evidence), the
    plan's evidence text is matched against the *current* blocks, lines, links
    and empty nodes; an exact match yields a corrected copy of the plan.  Only
    locator errors are recoverable — safety verdicts return ``None``.
    """

    relocatable_errors = {
        "AI 修复目标正文块不存在",
        "AI 修复目标正文行不存在",
        "AI 修复目标链接不存在",
        "AI 修复目标空正文块不存在",
        "AI 修复证据与目标正文块不一致",
        "AI 修复证据与目标链接文字不一致",
        "AI 修复证据与目标正文行不一致",
    }
    if error not in relocatable_errors:
        return None
    raw_action = str(item.get("action") or item.get("operation") or "").strip().lower()
    if raw_action == "delete_duplicate":
        # Duplicate plans rely on a keep-target relation; relocating only one
        # side would break the pairing, so keep them fail-closed.
        return None
    evidence = _plain_text(str(item.get("evidence") or item.get("before") or ""))
    if not evidence:
        return None
    if raw_action in {"remove_link", "remove_anchor", "delete_link"}:
        for link in content_links(body):
            if _plain_text(str(link.get("text") or "")) == evidence:
                return {**item, "link_id": str(link["link_id"]), "action": "remove_link"}
        return None
    if raw_action in {"remove_empty_block", "delete_empty_block", "remove_empty_node"}:
        return None  # empty blocks carry no evidence text to relocate by
    # Block / line removals and text replacements share block matching.
    for block in content_blocks(body):
        if str(block.get("text") or "") != evidence:
            continue
        relocated = {**item}
        if ".s" in str(item.get("segment_id") or "") or raw_action in {
            "remove_text_line", "delete_segment",
        }:
            # A whole-block evidence match on a line plan means the line is the
            # entire block content; the line split would fail anyway, so use
            # the block action with the same semantics.
            relocated["action"] = "remove_block"
            relocated.pop("segment_id", None)
        relocated["block_id"] = str(block["block_id"])
        return relocated
    # Line-level relocation: find the matching segment inside a multi-line block.
    for block in content_blocks(body):
        for segment in (block.get("segments") or []):
            if str(segment.get("text") or "") == evidence:
                return {
                    **item,
                    "action": "remove_text_line",
                    "segment_id": str(segment["segment_id"]),
                    "block_id": None,
                }
    return None


def apply_repair_plan(
    body_html: str | None,
    plan: Any,
    *,
    verifier: Callable[[dict[str, Any]], bool] | None = None,
    _allow_partial: bool = False,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Apply only an exact, text-block AI plan.

    No HTML is accepted in replacement text.  Every operation must identify a
    known block and include matching evidence, otherwise the complete plan is
    rejected and the original body is returned unchanged.

    *verifier* is the escape hatch from pattern whitelists.  A ``replace_text``
    edit that is provably a verbatim deletion but whose removed fragment does
    not match any known residue shape is offered to the verifier, which decides
    whether the deletion preserves every news fact.  Without a verifier the
    function keeps its original fail-closed behaviour.
    """

    body = str(body_html or "")
    plans, plan_error = _plan_list(plan)
    if plan_error:
        return body, [], plan_error
    if not plans:
        return body, [], None
    # ``after`` can arrive as JSON ``null`` (or the literal strings some
    # models emit for it) when the model intends "delete this block" but was
    # forced into a replace action by the schema.  Converting it to
    # ``remove_block`` keeps the deletion inside the same strict evidence /
    # promotion-shape checks instead of failing on an empty replacement.
    for item in plans:
        if not isinstance(item, dict):
            continue
        raw_action = str(item.get("action") or item.get("operation") or "").strip().lower()
        if raw_action not in {"replace_text", "replace_exact_text", "text_replace"}:
            continue
        raw_after = item.get("after")
        if raw_after is None or str(raw_after).strip().lower() in {"null", "none"}:
            item["action"] = "remove_block"
            item.pop("after", None)
            item.pop("replacement", None)
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
                verifier=verifier,
                _allow_partial=True,
            )
            if item_error:
                relocated = _relocate_plan_by_evidence(body, item, str(item_error))
                if relocated is not None:
                    _, _, relocated_error = apply_repair_plan(
                        body,
                        relocated,
                        verifier=verifier,
                        _allow_partial=True,
                    )
                    if relocated_error is None:
                        valid_plans.append(relocated)
                        continue
                rejected_errors.append(str(item_error))
            else:
                valid_plans.append(item)
        if not valid_plans:
            return body, [], rejected_errors[0] if rejected_errors else "AI 修复计划无可执行项目"
        cleaned, applied, combined_error = apply_repair_plan(
            body,
            valid_plans,
            verifier=verifier,
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
                # The source has no <a> anchor for this target: the model
                # mislabeled a plain-text URL line (e.g. "关西电视台DOGA：https://…")
                # as a link. Downgrade to a segment-level text removal when the
                # evidence unambiguously points at one such URL line, then fall
                # through to the remove_text_line validation below.
                fallback_segment_id = _link_plan_fallback_segment_id(
                    str(item.get("evidence") or item.get("before") or ""),
                    segments_by_id,
                )
                if fallback_segment_id is None:
                    return body, [], "AI 修复目标链接不存在"
                item["action"] = "remove_text_line"
                item["segment_id"] = fallback_segment_id
                item.pop("link_id", None)
                item["evidence"] = _plain_text(
                    str(segments_by_id[fallback_segment_id][1].get("text") or "")
                )
                action = "remove_text_line"
            else:
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

                # 区分两种情况：
                # 1. keep_id 不存在或等于目标 ID：自动寻找有效的保留目标
                # 2. keep_id 存在但文本不匹配：报错（AI 判断可能有误）
                if keep_pair is None or keep_id == target_id:
                    # 情况 1：AI 定位错误，自动寻找有效的保留目标
                    alternative_keep_id = None
                    for candidate_id, (candidate_block, candidate_segment) in segments_by_id.items():
                        if (
                            candidate_id != target_id
                            and _is_duplicate_keep_target(
                                evidence, str(candidate_segment.get("text") or "")
                            )
                        ):
                            alternative_keep_id = candidate_id
                            break
                    if alternative_keep_id is None:
                        return body, [], "重复内容修复缺少有效的保留正文行（目标文本在文档中唯一，疑似AI误判）"
                    keep_id = alternative_keep_id
                    keep_pair = segments_by_id[keep_id]
                elif not _is_duplicate_keep_target(evidence, str(keep_pair[1].get("text") or "")):
                    # 情况 2：keep_id 存在但文本不匹配，报错
                    return body, [], "重复内容修复缺少有效的保留正文行（指定的保留目标文本不匹配）"

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
        edge_removed_fragment: str | None = None
        validation_mode: str | None = None
        if action == "remove_block":
            if issue_type == "duplicate_content":
                keep_id = str(item.get("keep_block_id") or "").strip().lower()
                keep_block = by_id.get(keep_id)

                # 区分两种情况：
                # 1. keep_id 不存在或等于目标 ID：自动寻找有效的保留目标
                # 2. keep_id 存在但文本不匹配：报错（AI 判断可能有误）
                if keep_block is None or keep_id == block_id:
                    # 情况 1：AI 定位错误，自动寻找有效的保留目标
                    alternative_keep_id = None
                    for candidate_id, candidate_block in by_id.items():
                        if (
                            candidate_id != block_id
                            and _is_duplicate_keep_target(
                                evidence, str(candidate_block.get("text") or "")
                            )
                        ):
                            alternative_keep_id = candidate_id
                            break
                    if alternative_keep_id is None:
                        return body, [], "重复内容修复缺少有效的保留正文块（目标文本在文档中唯一，疑似AI误判）"
                    keep_id = alternative_keep_id
                    keep_block = by_id[keep_id]
                elif not _is_duplicate_keep_target(evidence, str(keep_block.get("text") or "")):
                    # 情况 2：keep_id 存在但文本不匹配，报错
                    return body, [], "重复内容修复缺少有效的保留正文块（指定的保留目标文本不匹配）"

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
                # 两种结果都可接受：改写成规范化的"（图片来源：X）"，或按 AI 的
                # 判断整段删除署名只保留图注。删除分支同样要求逐字保留图注，
                # 且需要一个可删除的问题类别和有效理由。
                normalized_after = _plain_text(photo_match["after"])
                caption_only = _plain_text(photo_match.get("caption") or "")
                plain_replacement = _plain_text(replacement)
                if plain_replacement == normalized_after:
                    pass
                elif (
                    caption_only
                    and plain_replacement == caption_only
                    and issue_type in (
                        _AI_GENERAL_REMOVAL_ISSUE_TYPES | _AI_PROMOTION_ISSUE_TYPES
                    )
                    and _has_valid_ai_reason(item)
                ):
                    edge_removed_fragment = _plain_text(photo_match["before"])[len(caption_only):].strip()
                    validation_mode = "ai_photo_credit_removal"
                else:
                    return body, [], "图片署名替换内容与规范化结果不一致"
            elif (edge_removed_fragment := _edge_fragment_removal(evidence, replacement)) is not None:
                # Phase-1 plan B: strip a dirty fragment glued to the start or
                # end of a reporting paragraph. ``_edge_fragment_removal`` already
                # guarantees ``after`` is a contiguous prefix/suffix of the
                # evidence (so every kept character — including any score/date
                # digit — comes verbatim from the original and cannot be
                # altered) and that the removed fragment matches the template/ad
                # residue probe. We only additionally require a removable issue
                # type and a valid reason so a factual clause is never deleted.
                if (
                    issue_type not in (_AI_GENERAL_REMOVAL_ISSUE_TYPES | _AI_PROMOTION_ISSUE_TYPES)
                    or not _has_valid_ai_reason(item)
                ):
                    return body, [], "AI 段首段尾片段删除不是可验证的局部修复"
            elif (edge_removed_fragment := _whole_sentence_removal(evidence, replacement)) is not None:
                # Whole-sentence removal: the model deletes one or more complete
                # promotional/template sentences from inside a paragraph. The
                # same residue probe plus an explicit removal issue type guards
                # against deleting a factual sentence.
                if (
                    issue_type not in (_AI_GENERAL_REMOVAL_ISSUE_TYPES | _AI_PROMOTION_ISSUE_TYPES)
                    or not _has_valid_ai_reason(item)
                ):
                    return body, [], "AI 段中整句删除不是可验证的局部修复"
                validation_mode = "ai_whole_sentence_removal"
            elif (edge_removed_fragment := _infix_fragment_removal(evidence, replacement)) is not None:
                # Infix removal: a single promotion/template fragment wedged
                # between two kept sentences (e.g. "（见下方视频）"). The same
                # residue probe plus a removal issue type guard against cutting a
                # factual clause out of the middle of a paragraph.
                if (
                    issue_type not in (_AI_GENERAL_REMOVAL_ISSUE_TYPES | _AI_PROMOTION_ISSUE_TYPES)
                    or not _has_valid_ai_reason(item)
                ):
                    return body, [], "AI 段中片段删除不是可验证的局部修复"
                validation_mode = "ai_infix_fragment_removal"
            elif (
                generic_removal := delete_only_replacement(evidence, replacement)
            )[0] is not None:
                # 快路径没命中：这条编辑已经被证明是逐字删除——保留文字全部原样来自
                # evidence，不新增也不改写任何一个字——但被删片段不属于任何已知的残留
                # 形态。此前这里直接 fail-closed，于是每出现一种新写法就得补一条正则，
                # 永远追不上上游。改为把"这段文字是什么类别"换成"删掉它会不会丢新闻
                # 事实"，交给调用方的核验器回答；核验不通过或核验不可用时仍然转人工。
                edge_removed_fragment, removal_shape = generic_removal
                if (
                    issue_type
                    not in (_AI_GENERAL_REMOVAL_ISSUE_TYPES | _AI_PROMOTION_ISSUE_TYPES)
                    or not _has_valid_ai_reason(item)
                    or verifier is None
                ):
                    return body, [], "AI 文本替换不是可验证的轻微局部修复"
                verdict = verifier({
                    "removed_text": edge_removed_fragment,
                    "evidence": evidence,
                    "replacement": replacement,
                    "issue_type": issue_type,
                    "reason": str(item.get("reason") or "")[:500],
                    "shape": removal_shape,
                })
                if verdict is not True:
                    return body, [], "AI 删除内容未通过信息保全核验"
                validation_mode = "ai_verified_removal"
            elif (
                issue_type not in _AI_REPLACEMENT_ISSUE_TYPES
                or not _has_valid_ai_reason(item)
                or replacement == evidence
                or _numeric_expressions(replacement) != _numeric_expressions(evidence)
            ):
                return body, [], "AI 文本替换不是可验证的轻微局部修复"
            elif not _is_template_residue_cleanup(evidence, replacement) and (
                len(block.get("segments") or []) != 1
                or _substantive_text(replacement) != _substantive_text(evidence)
            ):
                return body, [], "AI 文本替换不是可验证的轻微局部修复"
        operations.append({
            "action": action,
            # A segment-scoped replace_text (evidence matched one line inside a
            # multi-line block) must only touch that line's plain-text span, not
            # the whole block. The span is plain text, so replace it verbatim via
            # whole_node instead of the block's ">...<" HTML rewrite.
            "start": int(segment["start"]) if segment is not None else int(block["start"]),
            "end": int(segment["end"]) if segment is not None else int(block["end"]),
            "replacement": replacement,
            "evidence": evidence,
            "item": item,
            "block_id": block_id,
            "segment_id": target_id if segment is not None else None,
            "keep_target_id": keep_target_id,
            "edge_removed_fragment": edge_removed_fragment,
            "whole_node": segment is not None,
            "tag": str(block.get("tag") or "p"),
            "validation": (
                validation_mode
                if validation_mode is not None
                else (
                    "photo_credit_rule"
                    if block_identity in allowed_photo_credits
                    else (
                        "ai_edge_fragment_removal"
                        if edge_removed_fragment is not None
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
                    )
                )
            ),
        })

    # 合并首尾相接或重叠的连续删除操作，避免误判为操作范围重叠
    operations = _merge_consecutive_removal_operations(operations)

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
    # An edge-fragment ``replace_text`` also removes visible text; count only the
    # stripped fragment so it shares the same single-pass deletion budget.
    removed_visible_chars += sum(
        len(_plain_text(str(operation.get("edge_removed_fragment") or "")))
        for operation in operations
        if operation.get("action") == "replace_text"
        and operation.get("edge_removed_fragment")
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
                (credit.group(name) for name in (
                    "source_zh", "source_ascii", "source_bare",
                    "source_copyright", "source_bracket",
                )
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
