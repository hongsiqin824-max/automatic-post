"""Ingestion, quality workflow, and optional draft creation orchestration."""

from __future__ import annotations

import logging
import errno
import fcntl
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect
from ..statuses import STATUS_LABELS
from . import title_dedup
from .material_client import MaterialClient, MaterialClientError, normalize_item
from .publisher import publish_ready_articles
from .quality import (
    LLMService,
    evaluate,
    is_photo_credit_advisory_plans,
    plan_local_repair,
    verify_removal_keeps_facts,
)
from .link_sanitizer import find_media_artifact_lines, preprocess_quality_body
from .promotion_repair import (
    MAX_AI_REPAIR_REMOVED_CHARS,
    MAX_AI_REPAIR_PLANS,
    MAX_AI_REPAIR_ROUNDS,
    REPAIR_RULE_VERSION,
    apply_repair_plan,
    body_safety_stats,
    delete_only_replacement,
    find_promotional_blocks,
    normalize_photo_credits,
    plans_from_dirty_targets,
    removal_matches_known_artifact,
    remove_promotional_blocks,
    removal_is_duplicated,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quality_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") in {"RECEIVED", "QUALITY_CHECKING", "ERROR"}


def _update_content(article_id: int, *, title: str | None = None,
                    body: str | None = None, connection,
                    quality_claim_token: str | None = None) -> None:
    conn = connection
    current = repo.get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    with conn:
        clauses = ["id=?"]
        params: list[Any] = [article_id]
        if quality_claim_token is not None:
            clauses.append("quality_claim_token=?")
            params.append(str(quality_claim_token))
        cursor = conn.execute(
            "UPDATE articles SET title_final=?, body_html=?, updated_at=? WHERE " + " AND ".join(clauses),
            (
                current.get("title_final", "") if title is None else str(title),
                current.get("body_html", "") if body is None else str(body),
                _utc_now(),
                *params,
            ),
        )
        if quality_claim_token is not None and cursor.rowcount != 1:
            raise RuntimeError("文章质检租约已失效，放弃写入旧正文")


def _make_llm(config: AppConfig) -> LLMService | None:
    if not config.llm_configured:
        return None
    return LLMService(
        config.llm_api_key,
        config.llm_base_url,
        config.llm_model,
        config.llm_timeout,
        config.llm_max_retries,
        config.llm_retry_delay_seconds,
    )


def _make_fallback_llm(config: AppConfig) -> LLMService | None:
    if not config.llm_fallback_configured:
        return None
    return LLMService(
        config.llm_api_key2,
        config.llm_base_url2,
        config.llm_model2,
        config.llm_timeout2,
        config.llm_max_retries2,
        config.llm_retry_delay_seconds2,
    )


def _apply_title_dedup(article_id: int, current: dict[str, Any], config: AppConfig, connection) -> str | None:
    """Block or reroute a queue-bound direct-publish article on title duplicates.

    Returns the replacement final status, or ``None`` when the article keeps
    its queue destination.  A failed judgement follows the configured policy
    and routes the article to manual review instead of publishing blindly.
    """

    if not config.title_dedup_enabled:
        return None
    mode = title_dedup.direct_publish_mode(article_id, current, connection)
    if mode != 1:
        return None

    # 不在这里等待同批次的并发质检：质检本身要几十秒，短暂 sleep 等不到对方进入
    # 候选池状态；而且先完成的那篇会留在池子里，后完成的那篇查重时照样能看见它，
    # 同一对稿件仍会被拦住一篇。
    candidates = repo.list_title_dedup_candidates(
        title_dedup.dedup_window_since(config.title_dedup_hours),
        article_id,
        connection,
    )
    result = title_dedup.check_title_duplicate(
        config,
        current,
        candidates,
        publish_mode=mode,
        body_loader=lambda target_id: repo.get_article_body(target_id, connection),
    )
    if not result["checked"] or result["outcome"] not in {"duplicate", "needs_review"}:
        title_dedup.record_body_confirm_release(article_id, result, connection)
        return None
    if result["outcome"] == "duplicate":
        matched = result["matched"] or {}
        repo.transition_status(
            article_id,
            "TITLE_DUPLICATE",
            connection,
            event_type="TITLE_DUPLICATE_DETECTED",
            message=(
                f"与文章 #{matched.get('id')}《{matched.get('title')}》标题高度相似"
                f"（{title_dedup.match_scope_label(result)}），已取消自动发布"
            ),
            payload=result,
        )
        return "TITLE_DUPLICATE"
    repo.transition_status(
        article_id,
        "NEEDS_REVIEW",
        connection,
        event_type="TITLE_DUPLICATE_REVIEW",
        message=f"标题查重判定失败：{result['error']}，转人工审核",
        payload=result,
    )
    return "NEEDS_REVIEW"


def _audit_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal source offsets before persisting repair evidence."""

    audited: list[dict[str, Any]] = []
    for item in matches:
        entry = {
            "rule": str(item.get("rule") or ""),
            "tag": str(item.get("tag") or ""),
        }
        for key in (
            "block_id",
            "segment_id",
            "link_id",
            "action",
            "validation",
            "issue_type",
            "reason",
            "text",
            "caption",
            "source",
            "before",
            "after",
        ):
            value = str(item.get(key) or "")[:300]
            if value:
                entry[key] = value
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            entry["confidence"] = confidence
        audited.append(entry)
    return audited


def _audit_repair_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist only bounded, non-HTML repair-plan evidence."""

    audited: list[dict[str, Any]] = []
    for item in plans[:MAX_AI_REPAIR_PLANS]:
        segment_id = str(item.get("segment_id") or "")[:40]
        block_id = str(item.get("block_id") or "")[:40]
        link_id = str(item.get("link_id") or "")[:40]
        empty_block_id = str(item.get("empty_block_id") or "")[:40]
        if not block_id and ".s" in segment_id.lower():
            block_id = segment_id.lower().split(".s", 1)[0]
        entry: dict[str, Any] = {
            "block_id": block_id,
            "segment_id": segment_id,
            "link_id": link_id,
            "empty_block_id": empty_block_id,
            "action": str(item.get("action") or item.get("operation") or "")[:40],
            "evidence": str(item.get("evidence") or item.get("before") or "")[:300],
        }
        for key in ("issue_type", "reason", "keep_block_id", "keep_segment_id"):
            value = str(item.get(key) or "")[:300]
            if value:
                entry[key] = value
        if "issue_type" not in entry and item.get("issue_code"):
            entry["issue_type"] = str(item.get("issue_code"))[:80]
        if item.get("after") is not None or item.get("replacement") is not None:
            entry["after"] = str(item.get("after") or item.get("replacement") or "")[:300]
        try:
            entry["confidence"] = float(item.get("confidence"))
        except (TypeError, ValueError):
            entry["confidence"] = None
        audited.append(entry)
    return audited


def _quality_passes(quality: Any) -> bool:
    """Require an explicit, empty-issues pass before entering publication."""

    if not isinstance(quality, dict):
        return False
    if quality.get("decision") not in {None, "clean"}:
        return False
    if quality.get("pass") is not True or quality.get("needs_review") is not False:
        return False
    if quality.get("repair_plans") or quality.get("repair_plan_error"):
        return False
    issues = quality.get("issues")
    if not isinstance(issues, dict):
        return False
    required_keys = {
        "title_problems",
        "dirty_content",
        "completeness_problems",
        "channel_problems",
        "semantic_problems",
    }
    if set(issues) != required_keys:
        return False
    return all(isinstance(values, list) and not values for values in issues.values())


def _quality_repair_scope_is_dirty_only(quality: dict[str, Any]) -> bool:
    """Allow body-only cleanup only when no other quality issue is present."""

    issues = quality.get("issues")
    if not isinstance(issues, dict):
        return False
    dirty = issues.get("dirty_content")
    if not isinstance(dirty, list) or not dirty:
        return False
    return all(
        isinstance(issues.get(key), list) and not issues.get(key)
        for key in (
            "title_problems",
            "completeness_problems",
            "channel_problems",
            "semantic_problems",
        )
    )


def _quality_has_repairable_ai_plan(
    quality: dict[str, Any], plans: list[dict[str, Any]]
) -> bool:
    """Return whether a failed body check has a bounded AI repair plan."""

    if not plans:
        return False
    semantic = quality.get("semantic_check")
    if not isinstance(semantic, dict):
        return False
    if (
        quality.get("decision") != "repairable"
        and semantic.get("repairable") is not True
    ):
        return False
    if semantic.get("title_complete") is False or semantic.get("body_complete") is False:
        return False
    issues = quality.get("issues")
    if not isinstance(issues, dict):
        return False
    if any(
        not isinstance(issues.get(key), list) or issues.get(key)
        for key in (
            "title_problems",
            "completeness_problems",
            "channel_problems",
        )
    ):
        return False
    return bool(
        quality.get("decision") == "repairable"
        or issues.get("dirty_content")
        or issues.get("semantic_problems")
        or (isinstance(semantic, dict) and semantic.get("needs_review") is True)
    )


def _quality_allows_body_repair_planning(quality: dict[str, Any]) -> bool:
    """Keep title, completeness and channel failures out of body-only repair."""

    issues = quality.get("issues")
    if not isinstance(issues, dict):
        return False
    return bool(
        not quality.get("semantic_error")
        and not any(issues.get(key) for key in (
            "title_problems",
            "completeness_problems",
            "channel_problems",
        ))
        and (issues.get("dirty_content") or issues.get("semantic_problems"))
    )


def _plan_error_allows_dedicated_planner(error: Any) -> bool:
    """Allow the planner to replace only malformed or empty plan payloads.

    The planner runs *before* any body mutation, so a malformed payload
    (including the "dirty content detected but no plan offered" shape) is
    exactly the situation a dedicated planning call is designed to fix.
    Safety rejections produced by ``apply_repair_plan`` are handled by the
    separate, narrower replan whitelist instead.
    """

    if not error:
        return True
    return str(error) in {
        "AI 修复计划项目为空",
        "AI 修复计划包含无效项目",
        "AI 修复计划必须是对象或数组",
        "AI 质检返回字段格式错误：repairable",
        "AI 修复计划与质检结论矛盾",
        "AI 修复计划与标题完整性结论矛盾",
        "AI 修复计划与正文完整性结论矛盾",
    }


def _repair_validation_error_allows_replan(error: Any) -> bool:
    """Re-plan locators and formats; never let the model retry a safety rejection.

    Locator misses, empty/malformed replacement payloads, and category/reason
    formatting failures are all *positional* mistakes a re-plan can correct.
    Hard safety verdicts (overlaps, budget overruns, unmet promotion shapes,
    integrity contradictions) stay excluded: retrying them would only invite
    the model to widen its edit.
    """

    return str(error or "") in {
        # 定位类错误：重新选择目标即可修复
        "AI 修复目标正文块不存在",
        "AI 修复目标正文行不存在",
        "AI 修复目标链接不存在",
        "AI 修复证据与目标链接文字不一致",
        "AI 修复目标空正文块不存在",
        "AI 修复空正文块证据必须为空",
        "AI 修复证据与目标正文块不一致",
        "行级修复必须提供 segment_id",
        "行级修复目标缺少明确边界",
        "重复内容修复缺少有效的保留正文行",
        "重复内容修复缺少有效的保留正文块",
        "重复内容修复缺少有效的保留正文行（指定的保留目标文本不匹配）",
        "重复内容修复缺少有效的保留正文块（指定的保留目标文本不匹配）",
        # 格式/载荷类错误：模型返回了意图但载荷不合格
        "AI 文本替换内容为空或包含 HTML",
        "AI 文本替换不是可验证的轻微局部修复",
        "AI 段首段尾片段删除不是可验证的局部修复",
        "AI 段中整句删除不是可验证的局部修复",
        "AI 修复推广类别或原因无效",
        "AI 修复目标未命中高置信推广或图片署名规则",
        "AI 修复目标未命中高置信独立推广行",
        "AI 修复目标未命中可处理的局部问题类型",
        "AI 修复目标未命中高置信固定规则，也未提供可验证的推广类别和原因",
    }


class _RemovalVerifier:
    """Information-preservation gate for deletions no pattern rule recognises.

    The validator in :mod:`promotion_repair` proves an edit is a verbatim
    deletion; this class answers the only remaining question — whether the
    deletion drops a news fact.  It replaces the per-wording pattern whitelists
    that previously had to grow for every new promo phrasing.

    Two channels, cheapest first: a mechanical check (the removed text still
    exists elsewhere in the body, so no fact can be lost) and a single model
    call.  Verdicts are cached per fragment so the plan-validation retries do
    not multiply the call count, and every verdict is recorded for audit.
    """

    def __init__(self, config: AppConfig, *, body: str) -> None:
        self._config = config
        self.body = body
        self._cache: dict[str, bool] = {}
        self.records: list[dict[str, Any]] = []

    def __call__(self, context: dict[str, Any]) -> bool:
        removed = str(context.get("removed_text") or "").strip()
        if not removed:
            return False
        cached = self._cache.get(removed)
        if cached is not None:
            return cached
        verdict, record = self._decide(removed, context)
        self._cache[removed] = verdict
        if len(self.records) < MAX_AI_REPAIR_PLANS * MAX_AI_REPAIR_ROUNDS:
            self.records.append(record)
        return verdict

    def _decide(
        self, removed: str, context: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        record: dict[str, Any] = {
            "removed_text": removed[:300],
            "issue_type": str(context.get("issue_type") or "")[:80],
            "shape": str(context.get("shape") or "")[:40],
        }
        if removal_is_duplicated(removed, self.body):
            return True, {**record, "channel": "duplicate_elsewhere", "safe": True}
        llm = _make_llm(self._config)
        if llm is None:
            return False, {
                **record,
                "channel": "unavailable",
                "safe": False,
                "error": "未配置 LLM，无法核验删除是否丢失事实",
            }
        result = verify_removal_keeps_facts(
            removed_text=removed,
            kept_text=str(context.get("replacement") or ""),
            body=self.body,
            llm=llm,
            llm_fallback=_make_fallback_llm(self._config),
        )
        return bool(result.get("safe")), {
            **record,
            "channel": "fact_verification",
            "safe": bool(result.get("safe")),
            "loses_fact": result.get("loses_fact"),
            "confidence": result.get("confidence"),
            "verifier_reason": str(result.get("reason") or "")[:300],
            "error": result.get("error"),
        }


def _repair_fingerprint(body: str) -> dict[str, Any]:
    return {
        "body_sha256": body_safety_stats(body)["sha256"],
        "rule_version": REPAIR_RULE_VERSION,
    }


def _repair_already_consumed(previous_repair: dict[str, Any], body: str) -> bool:
    """Return whether the article already consumed its auto-repair attempt.

    A *successful* repair is final: the body was mutated, so a second round
    could delete more content and must never run again.  A *failed* attempt
    left the body untouched, so it only blocks further attempts for the same
    body under the same rule version — bumping :data:`REPAIR_RULE_VERSION`
    lets improved validation retry an article once.

    Records written before fingerprints existed carry no version information;
    they keep the original one-attempt-forever behaviour so historical review
    items are not silently reprocessed.
    """

    if not isinstance(previous_repair, dict) or not previous_repair.get("attempted"):
        return False
    if previous_repair.get("applied") is True:
        return True
    fingerprint = previous_repair.get("repair_fingerprint")
    if not isinstance(fingerprint, dict):
        return True
    current = _repair_fingerprint(body)
    try:
        previous_version = int(fingerprint.get("rule_version") or 0)
    except (TypeError, ValueError):
        return True
    return bool(
        str(fingerprint.get("body_sha256") or "") == current["body_sha256"]
        and previous_version >= REPAIR_RULE_VERSION
    )


def _clear_non_blocking_photo_advisory(
    quality: dict[str, Any],
    *,
    body: str,
) -> dict[str, Any]:
    """Keep explicit photo-credit advice in audit data without blocking pass."""

    if (
        quality.get("pass") is not True
        or quality.get("needs_review") is not False
    ):
        return quality
    issues = quality.get("issues")
    if not isinstance(issues, dict) or any(issues.get(key) for key in (
        "title_problems",
        "dirty_content",
        "completeness_problems",
        "channel_problems",
        "semantic_problems",
    )):
        return quality
    semantic = quality.get("semantic_check")
    if not isinstance(semantic, dict):
        return quality
    if semantic.get("has_ad_or_dirty") is not False or semantic.get("needs_review") is not False:
        return quality
    plans = quality.get("repair_plans")
    if not is_photo_credit_advisory_plans(plans, body=body):
        return quality
    advisory = [dict(item) for item in (plans if isinstance(plans, list) else [plans])]
    normalized = dict(quality)
    normalized["advisory_repair_plans"] = advisory
    normalized["repair_plans"] = []
    normalized["repair_plan_error"] = None
    normalized["advisory_reason"] = "摄影署名建议仅作记录，未自动修改正文"
    return normalized


def _skipped_plan_errors(matches: list[dict[str, Any]]) -> list[str]:
    """Return the validation errors of plan items silently skipped on apply.

    ``apply_repair_plan`` applies the verifiable items of a multi-item plan and
    records the rejected ones on the first match.  A partially repaired body
    still carries the remaining dirt, so the caller must react instead of
    sending that candidate into the second quality pass.
    """

    if not matches:
        return []
    errors = matches[0].get("skipped_plan_errors")
    if not isinstance(errors, list):
        return []
    return [str(item)[:300] for item in errors if str(item or "").strip()]


def _quality_allows_followup_body_repair(
    quality: dict[str, Any],
) -> bool:
    """Allow another narrowly-scoped repair round for a result with a plan.

    A model response often reports only part of the dirt per round, or marks an
    article clean while still returning a plan for a leftover block.  That plan
    is usable only when every other quality dimension is explicitly clean and
    the target can be validated by ``apply_repair_plan``.  The caller limits the
    path to deletion-shaped operations, caps the number of rounds, and runs a
    complete quality pass after every round.
    """

    if not isinstance(quality, dict):
        return False
    decision_repairable = quality.get("decision") == "repairable"
    legacy_contradiction = (
        quality.get("repair_plan_error") == "AI 修复计划与质检结论矛盾"
    )
    if not decision_repairable and not legacy_contradiction:
        return False
    semantic = quality.get("semantic_check")
    if not isinstance(semantic, dict):
        return False
    if semantic.get("title_complete") is not True or semantic.get("body_complete") is not True:
        return False
    if legacy_contradiction and (
        semantic.get("has_ad_or_dirty") is not False
        or semantic.get("needs_review") is not False
    ):
        return False
    issues = quality.get("issues")
    if not isinstance(issues, dict):
        return False
    if any(
        not isinstance(issues.get(key), list) or issues.get(key)
        for key in (
            "title_problems",
            "completeness_problems",
            "channel_problems",
        )
    ):
        return False
    semantic_problems = issues.get("semantic_problems")
    if not isinstance(semantic_problems, list):
        return False
    if legacy_contradiction and any(
        "修复计划与质检结论矛盾" not in str(item)
        for item in semantic_problems
    ):
        return False
    plans = quality.get("repair_plans")
    if not isinstance(plans, list) or not plans:
        return False
    allowed_issue_types = {
        "duplicate_content",
        "template_artifact",
        "format_noise",
        "extraneous_content",
        "advertisement",
        "media_promotion",
        "traffic_generation",
        "promotion",
        "call_to_action",
        "program_promotion",
        "channel_promotion",
        "external_promotion",
        "schedule_promotion",
        "social_promotion",
        "sponsorship_promotion",
        "video_promotion",
        "无关内容",
        "重复内容",
        "模板残留",
        "格式噪声",
        "引流",
        "推广",
        "广告",
        "广告引流",
        "视频引流",
    }
    reason_markers = (
        "孤立",
        "不完整",
        "残留",
        "重复",
        "无关",
        "推广",
        "引流",
        "广告",
        "链接",
        "跳转",
        "外链",
        "空块",
    )
    for item in plans:
        if not isinstance(item, dict):
            return False
        action = str(item.get("action") or item.get("operation") or "").strip().lower()
        if action not in {
            "remove_block",
            "remove_text_line",
            "remove_link",
            "delete_link",
            "remove_anchor",
            "remove_empty_block",
            "delete_empty_block",
            "delete_duplicate",
            # 后续轮的计划多半是"删掉段尾那一小截"，也就是 replace_text。把它挡在门外
            # 等于让系统看着一份自己刚生成的可执行计划转人工。安全性不靠这份动作白名单，
            # 而靠 apply_repair_plan 对每一条计划做的逐字校验——保留文本必须原样来自
            # 原文，否则整份计划作废。这与第一轮用的是同一套校验。
            "replace_text",
            "replace",
        }:
            return False
        issue_type = str(item.get("issue_type") or item.get("issue_code") or "").strip().lower()
        if issue_type not in allowed_issue_types:
            return False
        evidence = str(item.get("evidence") or item.get("before") or "").strip()
        if not evidence and action not in {
            "remove_link",
            "remove_empty_block",
            "delete_empty_block",
        }:
            return False
        reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
        # 只要求理由是一段像样的说明。此前这里额外要求理由里出现"残留/引流/重复"等
        # 关键词，但模型完全可以用别的说法描述同一件事（"该段是 b2 的简化版，信息被
        # b2 覆盖"），结果是一份本来能执行的计划因为措辞被否掉。问题类别已经限定在可
        # 删除的范围内，计划本身还要过 apply_repair_plan 的逐字校验，措辞不必再管。
        if not 4 <= len(reason) <= 500 or "<" in reason or ">" in reason:
            return False
        if action in {"remove_link", "delete_link", "remove_anchor"} and not any(
            marker in reason for marker in reason_markers
        ):
            # 链接删除没有可比对的替换文本，保留原来的措辞检查作为额外约束。
            return False
    return True


def _followup_body_is_safe(before: str, after: str) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """Apply the same body/image invariants to a follow-up candidate."""

    before_stats = body_safety_stats(before)
    after_stats = body_safety_stats(after)
    safe = bool(
        after != before
        and after_stats["visible_text_length"] >= 30
        and before_stats["parse_ok"]
        and after_stats["parse_ok"]
        and before_stats["image_count"] == after_stats["image_count"]
        and before_stats["image_sources"] == after_stats["image_sources"]
        and before_stats["image_attributes"] == after_stats["image_attributes"]
    )
    return safe, before_stats, after_stats


def _removed_visible_chars(matches: list[dict[str, Any]]) -> int:
    """Count exact removed evidence without HTML block separator artifacts."""

    return sum(
        len(str(item.get("text") or item.get("before") or ""))
        for item in matches
        if str(item.get("action") or "") != "replace_text"
    )


def _record_quality_result(
    article_id: int,
    quality: dict[str, Any],
    quality_round: int,
    connection,
    *,
    status: str,
    quality_claim_token: str | None = None,
) -> None:
    payload = dict(quality)
    payload["quality_round"] = quality_round
    repo.add_article_event(
        article_id,
        "QUALITY_RESULT",
        connection,
        message=quality.get("reason", "质检完成"),
        payload=payload,
        from_status=status,
        to_status=status,
        quality_claim_token=quality_claim_token,
    )


def _apply_quality_title(article_id: int, current: dict[str, Any], quality: dict[str, Any], connection, *, quality_claim_token: str | None = None) -> None:
    title_after = quality.get("title_after") or current.get("title_final", "")
    if title_after == current.get("title_final", ""):
        return
    _update_content(article_id, title=title_after, connection=connection, quality_claim_token=quality_claim_token)
    repo.add_article_event(
        article_id,
        "TITLE_FIXED",
        connection,
        message=f"标题已自动修正（{quality.get('title_fix_method', 'unknown')}）",
        payload={"before": current.get("title_final", ""), "after": title_after},
        quality_claim_token=quality_claim_token,
    )
    current["title_final"] = title_after


def _second_quality_error(first_quality: dict[str, Any], error: Exception) -> dict[str, Any]:
    issues = dict(first_quality.get("issues") or {})
    semantic = list(issues.get("semantic_problems") or [])
    semantic.append("候选正文的二次完整质检异常，需要人工确认")
    issues["semantic_problems"] = semantic
    return {
        **first_quality,
        "pass": False,
        "needs_review": True,
        "score": 50,
        "issues": issues,
        "reason": "候选正文已生成，但二次完整质检异常，需要人工确认",
        "second_quality_error": str(error)[:300],
    }


def _process_article(article: dict[str, Any], config: AppConfig, connection) -> str:
    article_id = int(article["id"])
    if not _quality_eligible(article):
        return article.get("status", "")
    claimed = repo.claim_quality_article(
        article_id,
        article.get("updated_at"),
        connection,
    )
    if claimed is None:
        current = repo.get_article(article_id, connection)
        return current.get("status", "") if current else ""
    article = claimed
    quality_claim_token = str(claimed.get("quality_claim_token") or "")
    if not article.get("tabs") and not article.get("tab_id"):
        try:
            quality = {
                "pass": False,
                "needs_review": True,
                "score": 50,
                "level": "B",
                "issues": {"configuration": ["该来源尚未绑定栏目"]},
                "reason": "来源尚未绑定栏目",
                "weak_channel_check": True,
            }
            repo.save_quality(
                article_id,
                quality,
                connection,
                status="NEEDS_REVIEW",
                quality_claim_token=quality_claim_token,
            )
            return "NEEDS_REVIEW"
        except RuntimeError:
            current = repo.get_article(article_id, connection)
            if current and current.get("quality_claim_token") != quality_claim_token:
                return current.get("status", "")
            raise
        finally:
            repo.release_quality_claim(article_id, quality_claim_token, connection)

    current = repo.get_article(article_id, connection)
    try:
        body_before_preprocess = str(current.get("body_html") or "")
        cleaned_body = preprocess_quality_body(
            body_before_preprocess,
            source=current.get("source"),
        )
        if cleaned_body != body_before_preprocess:
            preprocess_before = body_safety_stats(body_before_preprocess)
            preprocess_after = body_safety_stats(cleaned_body)
            preprocess_safe = bool(
                preprocess_before["parse_ok"]
                and preprocess_after["parse_ok"]
                and preprocess_before["image_count"] == preprocess_after["image_count"]
                and preprocess_before["image_sources"] == preprocess_after["image_sources"]
                and preprocess_before["image_attributes"] == preprocess_after["image_attributes"]
            )
            if not preprocess_safe:
                # Never persist a quality cleanup that could lose or mutate
                # article media; the raw body will fail closed in quality.py.
                cleaned_body = body_before_preprocess
                preprocess_after = preprocess_before
            else:
                _update_content(
                    article_id,
                    body=cleaned_body,
                    connection=connection,
                    quality_claim_token=quality_claim_token,
                )
                current["body_html"] = cleaned_body
            repo.add_article_event(
                article_id,
                "LINKS_REMOVED",
                connection,
                message=(
                    "质检前已清理可跳转内容、播放器、图注署名等采集残留"
                    if preprocess_safe
                    else "检测到可清理的可跳转内容或采集残留，但安全校验未通过，正文未改动"
                ),
                payload={
                    "source": "quality_preprocess",
                    "rule_version": "quality-preprocess-v2",
                    "before_sha256": preprocess_before["sha256"],
                    "after_sha256": preprocess_after["sha256"],
                    "before_length": len(body_before_preprocess),
                    "after_length": len(cleaned_body),
                    "image_count_before": preprocess_before["image_count"],
                    "image_count_after": preprocess_after["image_count"],
                    "image_sources_before": preprocess_before["image_sources"],
                    "image_sources_after": preprocess_after["image_sources"],
                    "image_attributes_before": preprocess_before["image_attributes"],
                    "image_attributes_after": preprocess_after["image_attributes"],
                    "media_artifact_lines": find_media_artifact_lines(body_before_preprocess)[:20],
                    "applied": preprocess_safe,
                },
                quality_claim_token=quality_claim_token,
            )
        previous_repair = current.get("quality", {}).get("promotion_repair", {})
        first_quality = evaluate(
            title=current.get("title_final", ""),
            body=current.get("body_html", ""),
            channels=current.get("channels", []),
            llm=_make_llm(config),
            llm_fallback=_make_fallback_llm(config),
        )
        _record_quality_result(
            article_id,
            first_quality,
            1,
            connection,
            status="QUALITY_CHECKING",
            quality_claim_token=quality_claim_token,
        )

        first_failed = not _quality_passes(first_quality)
        body_before = str(current.get("body_html") or "")
        repair_consumed = _repair_already_consumed(previous_repair, body_before)
        removal_verifier = _RemovalVerifier(config, body=body_before)
        raw_repair_plans = first_quality.get("repair_plans")
        if isinstance(raw_repair_plans, dict):
            repair_plans = [raw_repair_plans]
        elif isinstance(raw_repair_plans, list):
            repair_plans = [item for item in raw_repair_plans if isinstance(item, dict)]
        else:
            repair_plans = []
        repair_plan_error = first_quality.get("repair_plan_error")
        original_repair_plan_error = repair_plan_error
        # 质检已经逐字指出脏内容位置时，直接据此合成删除计划：模型经常一边报脏、
        # 一边以"无法定位"为由不给计划，而位置信息本身是可校验的。合成计划仍要过
        # apply_repair_plan 的逐字校验、信息保全核验和完整的二次质检。
        synthesized_plans: list[dict[str, Any]] = []
        if (
            first_failed
            and not repair_plans
            and not repair_consumed
            and _quality_allows_body_repair_planning(first_quality)
        ):
            synthesized_plans = plans_from_dirty_targets(
                body_before, first_quality.get("dirty_targets")
            )
            # 合成计划来自"模型说这里脏"，而不是模型自己提交的可执行计划，因此每一条
            # 都必须先过信息保全核验——包括整块删除。apply_repair_plan 只在段内替换
            # 时才会问核验器，整块删除没有任何内容判据，若不在这里拦一道，模型把一段
            # 新闻误标成脏内容就会被整段删掉。核验结果按片段缓存，重复问不额外花钱。
            verified_plans: list[dict[str, Any]] = []
            for plan in synthesized_plans:
                removed_text = str(plan.get("evidence") or "")
                if str(plan.get("action")) == "replace_text":
                    fragment, shape = delete_only_replacement(
                        removed_text, str(plan.get("after") or "")
                    )
                    if fragment is None:
                        continue
                    removed_text, plan_shape = fragment, shape
                else:
                    plan_shape = str(plan.get("action"))
                if removal_matches_known_artifact(removed_text) or removal_verifier({
                    "removed_text": removed_text,
                    "evidence": plan.get("evidence"),
                    "replacement": plan.get("after") or "",
                    "issue_type": plan.get("issue_type"),
                    "reason": plan.get("reason"),
                    "shape": plan_shape,
                }):
                    verified_plans.append(plan)
            synthesized_plans = verified_plans
            if synthesized_plans:
                repair_plans = synthesized_plans
                repair_plan_error = None
        repair_planner: dict[str, Any] | None = None
        if (
            first_failed
            and not repair_plans
            and _plan_error_allows_dedicated_planner(repair_plan_error)
            and not repair_consumed
            and _quality_allows_body_repair_planning(first_quality)
        ):
            planner_llm = _make_llm(config)
            if planner_llm is not None:
                try:
                    planner_result = plan_local_repair(
                        title=current.get("title_final", ""),
                        body=body_before,
                        first_quality=first_quality,
                        llm=planner_llm,
                        llm_fallback=_make_fallback_llm(config),
                    )
                    repair_plan_error = planner_result.get("repair_plan_error")
                    planned = planner_result.get("repair_plans")
                    if isinstance(planned, list):
                        repair_plans = [item for item in planned if isinstance(item, dict)]
                    repair_planner = {
                        "repairable": planner_result.get("repairable") is True,
                        "reason": str(planner_result.get("reason") or "")[:500],
                        "trigger_error": (
                            str(original_repair_plan_error)[:300]
                            if original_repair_plan_error else None
                        ),
                        "repair_plans": _audit_repair_plans(repair_plans),
                        "repair_plan_error": (
                            str(repair_plan_error)[:300] if repair_plan_error else None
                        ),
                    }
                except Exception as exc:  # a planning outage must not modify content
                    logger.warning("AI 局部修复规划失败 article_id=%s: %s", article_id, exc)
                    repair_planner = {
                        "repairable": False,
                        "reason": "AI 局部修复规划调用失败",
                        "trigger_error": (
                            str(original_repair_plan_error)[:300]
                            if original_repair_plan_error else None
                        ),
                        "repair_plans": [],
                        "repair_plan_error": str(exc)[:300],
                    }
        ai_plan_error: str | None = None
        ai_plan_matches: list[dict[str, Any]] = []
        ai_body_after = body_before
        repair_replanner: dict[str, Any] | None = None
        promotion_candidates: list[dict[str, Any]] = []
        attribution_candidates: list[dict[str, Any]] = []
        repair_scope_is_dirty_only = _quality_repair_scope_is_dirty_only(first_quality)
        repairable_ai_plan = _quality_has_repairable_ai_plan(first_quality, repair_plans)
        if repair_planner is not None and repair_planner.get("repairable") is True:
            repairable_ai_plan = bool(
                repair_plans and _quality_allows_body_repair_planning(first_quality)
            )
        elif synthesized_plans:
            repairable_ai_plan = _quality_allows_body_repair_planning(first_quality)
        if first_failed and repair_plan_error and not repair_consumed:
            ai_plan_error = str(repair_plan_error)[:300]
        elif (
            first_failed
            and repairable_ai_plan
            and not repair_consumed
        ):
            ai_body_after, ai_plan_matches, ai_plan_error = apply_repair_plan(
                body_before, repair_plans, verifier=removal_verifier
            )
            if (
                _repair_validation_error_allows_replan(ai_plan_error)
                and (
                    first_quality.get("decision") == "repairable"
                    or _quality_allows_body_repair_planning(first_quality)
                )
            ):
                rejected_plans = [dict(item) for item in repair_plans]
                replanner_llm = _make_llm(config)
                if replanner_llm is not None:
                    trigger_error = str(ai_plan_error)[:300]
                    try:
                        replanner_result = plan_local_repair(
                            title=current.get("title_final", ""),
                            body=body_before,
                            first_quality=first_quality,
                            llm=replanner_llm,
                            llm_fallback=_make_fallback_llm(config),
                            validation_error=trigger_error,
                            rejected_plans=rejected_plans,
                        )
                        replanned = replanner_result.get("repair_plans")
                        replanned_plans = (
                            [item for item in replanned if isinstance(item, dict)]
                            if isinstance(replanned, list) else []
                        )
                        replanner_error = replanner_result.get("repair_plan_error")
                        repair_replanner = {
                            "attempted": True,
                            "trigger_error": trigger_error,
                            "rejected_plans": _audit_repair_plans(rejected_plans),
                            "repairable": replanner_result.get("repairable") is True,
                            "reason": str(replanner_result.get("reason") or "")[:500],
                            "repair_plans": _audit_repair_plans(replanned_plans),
                            "repair_plan_error": (
                                str(replanner_error)[:300] if replanner_error else None
                            ),
                        }
                        if (
                            replanner_result.get("repairable") is True
                            and replanned_plans
                            and not replanner_error
                        ):
                            repair_plans = replanned_plans
                            ai_body_after, ai_plan_matches, ai_plan_error = apply_repair_plan(
                                body_before,
                                repair_plans,
                                verifier=removal_verifier,
                            )
                            repair_replanner["validation_error"] = (
                                str(ai_plan_error)[:300] if ai_plan_error else None
                            )
                        else:
                            ai_plan_error = str(
                                replanner_error
                                or "AI 重新规划未提供可执行的局部修复计划"
                            )[:300]
                    except Exception as exc:  # one failed re-plan still preserves the original body
                        logger.warning("AI 局部修复重新规划失败 article_id=%s: %s", article_id, exc)
                        repair_replanner = {
                            "attempted": True,
                            "trigger_error": trigger_error,
                            "rejected_plans": _audit_repair_plans(rejected_plans),
                            "repairable": False,
                            "reason": "AI 局部修复重新规划调用失败",
                            "repair_plans": [],
                            "repair_plan_error": str(exc)[:300],
                        }
                        ai_plan_error = str(exc)[:300]
            if (
                ai_plan_error is None
                and repair_replanner is None
                and _skipped_plan_errors(ai_plan_matches)
                and (
                    first_quality.get("decision") == "repairable"
                    or _quality_allows_body_repair_planning(first_quality)
                )
            ):
                # 部分计划被跳过时候选正文仍残留脏内容，二次质检必然不通过。
                # 把跳过原因交给 AI 重新规划一次；重规划不可用时保留已验证的
                # 部分修复，不改变原有兜底行为。
                skipped_errors = _skipped_plan_errors(ai_plan_matches)
                replanner_llm = _make_llm(config)
                if replanner_llm is not None:
                    trigger_error = skipped_errors[0]
                    rejected_plans = [dict(item) for item in repair_plans]
                    try:
                        replanner_result = plan_local_repair(
                            title=current.get("title_final", ""),
                            body=body_before,
                            first_quality=first_quality,
                            llm=replanner_llm,
                            llm_fallback=_make_fallback_llm(config),
                            validation_error=trigger_error,
                            rejected_plans=rejected_plans,
                        )
                        replanned = replanner_result.get("repair_plans")
                        replanned_plans = (
                            [item for item in replanned if isinstance(item, dict)]
                            if isinstance(replanned, list) else []
                        )
                        replanner_error = replanner_result.get("repair_plan_error")
                        repair_replanner = {
                            "attempted": True,
                            "trigger": "skipped_plan_items",
                            "trigger_error": trigger_error,
                            "skipped_plan_errors": skipped_errors,
                            "rejected_plans": _audit_repair_plans(rejected_plans),
                            "repairable": replanner_result.get("repairable") is True,
                            "reason": str(replanner_result.get("reason") or "")[:500],
                            "repair_plans": _audit_repair_plans(replanned_plans),
                            "repair_plan_error": (
                                str(replanner_error)[:300] if replanner_error else None
                            ),
                        }
                        if (
                            replanner_result.get("repairable") is True
                            and replanned_plans
                            and not replanner_error
                        ):
                            retry_body, retry_matches, retry_error = apply_repair_plan(
                                body_before,
                                replanned_plans,
                                verifier=removal_verifier,
                            )
                            repair_replanner["validation_error"] = (
                                str(retry_error)[:300] if retry_error else None
                            )
                            if (
                                retry_error is None
                                and retry_matches
                                and not _skipped_plan_errors(retry_matches)
                            ):
                                repair_plans = replanned_plans
                                ai_body_after, ai_plan_matches = retry_body, retry_matches
                    except Exception as exc:  # a failed re-plan keeps the verified subset
                        logger.warning(
                            "AI 局部修复跳过项重新规划失败 article_id=%s: %s", article_id, exc
                        )
                        repair_replanner = {
                            "attempted": True,
                            "trigger": "skipped_plan_items",
                            "trigger_error": trigger_error,
                            "skipped_plan_errors": skipped_errors,
                            "rejected_plans": _audit_repair_plans(rejected_plans),
                            "repairable": False,
                            "reason": "AI 局部修复重新规划调用失败",
                            "repair_plans": [],
                            "repair_plan_error": str(exc)[:300],
                        }
            promotion_candidates = []
            attribution_candidates = []
        elif first_failed and repair_plans and not repair_consumed:
            ai_plan_error = "当前质检失败还包含无法通过正文局部修改解决的问题"
            promotion_candidates = []
            attribution_candidates = []
        elif (
            first_failed
            and repair_scope_is_dirty_only
            and isinstance(first_quality.get("semantic_check"), dict)
            and first_quality["semantic_check"].get("has_ad_or_dirty") is True
            and not repair_consumed
        ):
            ai_plan_error = "AI 发现广告或脏内容，但未提供可验证的局部修复计划"
            promotion_candidates = []
            attribution_candidates = []
        elif first_failed and repair_scope_is_dirty_only and not repair_consumed:
            promotion_candidates = find_promotional_blocks(body_before)
            _, attribution_candidates = normalize_photo_credits(body_before)
        else:
            promotion_candidates = []
            attribution_candidates = []
        candidates = [*promotion_candidates, *attribution_candidates]
        if ai_plan_error:
            repair = {
                "attempted": True,
                "applied": False,
                "outcome": "failed",
                "plan_error": ai_plan_error,
                "repair_plans": _audit_repair_plans(repair_plans),
                "repair_fingerprint": _repair_fingerprint(body_before),
                "first_quality": first_quality,
            }
            if removal_verifier.records:
                repair["removal_verification"] = removal_verifier.records
            if synthesized_plans:
                repair["dirty_target_plans"] = _audit_repair_plans(synthesized_plans)
            if repair_planner is not None:
                repair["repair_planner"] = repair_planner
            if repair_replanner is not None:
                repair["repair_replanner"] = repair_replanner
            final_quality = {**first_quality, "promotion_repair": repair}
            repo.save_quality(
                article_id,
                final_quality,
                connection,
                status="NEEDS_REVIEW",
                quality_claim_token=quality_claim_token,
            )
            repo.add_article_event(
                article_id,
                "AUTO_REPAIR_FINISHED",
                connection,
                message="AI 局部修复计划无法验证，已转人工审核",
                payload={
                    "outcome": "failed",
                    "final_status": "NEEDS_REVIEW",
                    "destination": "人工审核",
                    "error": ai_plan_error,
                    "repair_plans": _audit_repair_plans(repair_plans),
                },
                from_status="NEEDS_REVIEW",
                to_status="NEEDS_REVIEW",
                quality_claim_token=quality_claim_token,
            )
            return "NEEDS_REVIEW"
        if ai_plan_matches:
            candidates = ai_plan_matches
        if not candidates:
            final_quality = dict(first_quality)
            if repair_planner is not None:
                final_quality["repair_planner"] = repair_planner
            if repair_consumed:
                # A worker can stop after persisting the one-attempt marker
                # but before the body mutation or second quality pass.  A
                # later ingest must never turn that incomplete history into
                # an automatic pass just because the LLM result changed.
                issues = dict(final_quality.get("issues") or {})
                semantic = list(issues.get("semantic_problems") or [])
                semantic.append("该文章此前已触发正文自动优化，需人工确认处理结果")
                issues["semantic_problems"] = semantic
                final_quality.update({
                    "pass": False,
                    "needs_review": True,
                    "score": min(int(final_quality.get("score") or 50), 50),
                    "issues": issues,
                    "reason": "该文章此前已触发正文自动优化，已阻止重复处理并转人工审核",
                })
                final_quality["promotion_repair"] = previous_repair
                repo.save_quality(
                    article_id,
                    final_quality,
                    connection,
                    status="NEEDS_REVIEW",
                    quality_claim_token=quality_claim_token,
                )
                return "NEEDS_REVIEW"

            _apply_quality_title(
                article_id,
                current,
                first_quality,
                connection,
                quality_claim_token=quality_claim_token,
            )
            target = "NEEDS_REVIEW" if not _quality_passes(first_quality) else "READY_TO_PUBLISH"
            repo.save_quality(
                article_id,
                final_quality,
                connection,
                status=target,
                quality_claim_token=quality_claim_token,
            )
            if target == "READY_TO_PUBLISH" and int(current.get("upstream_archive_id") or 0) > 0:
                repo.transition_status(
                    article_id,
                    "ALREADY_PUBLISHED",
                    connection,
                    event_type="ALREADY_PUBLISHED_DETECTED",
                    message="素材接口已返回非零 archive_id，本阶段不重复发布",
                    quality_claim_token=quality_claim_token,
                )
                return "ALREADY_PUBLISHED"
            dedup_status = _apply_title_dedup(article_id, current, config, connection)
            if dedup_status:
                return dedup_status
            return target

        audit_matches = _audit_matches(candidates)
        repair: dict[str, Any] = {
            "attempted": True,
            "applied": False,
            "outcome": "pending",
            "scope": "bounded_local",
            "schema_version": 2,
            "removed_count": 0,
            "attribution_normalized_count": 0,
            "matches": audit_matches,
            "repair_fingerprint": _repair_fingerprint(body_before),
            "first_quality": first_quality,
        }
        if removal_verifier.records:
            repair["removal_verification"] = removal_verifier.records
        if synthesized_plans:
            repair["dirty_target_plans"] = _audit_repair_plans(synthesized_plans)
        if repair_planner is not None:
            repair["repair_planner"] = repair_planner
        if repair_replanner is not None:
            repair["repair_replanner"] = repair_replanner
        progress_quality = {**first_quality, "promotion_repair": repair}
        # Persist the one-attempt marker before mutating the body. If a worker
        # stops between steps, a later run cannot delete more content.
        repo.save_quality(
            article_id,
            progress_quality,
            connection,
            status="QUALITY_CHECKING",
            quality_claim_token=quality_claim_token,
        )
        repo.add_article_event(
            article_id,
            "AUTO_REPAIR_TRIGGERED",
            connection,
            message=(
                "首轮 AI 质检定位到可验证局部问题，准备生成候选正文"
                if ai_plan_matches
                else "首轮质检命中可安全处理的固定格式，准备自动优化一次"
            ),
            payload={
                "quality_round": 1,
                "trigger_reason": (
                    "首轮质检失败后，AI 返回具体位置、完整证据、问题类别和原因，且通过结构与置信度校验"
                    if ai_plan_matches
                    else "首轮完整质检未通过，且命中高置信固定格式"
                ),
                "match_count": len(audit_matches),
                "rules": list(dict.fromkeys(item["rule"] for item in audit_matches)),
                "matched_texts": [
                    item.get("text") or item.get("before") or ""
                    for item in audit_matches
                ],
                "repair_plans": _audit_repair_plans(repair_plans),
                "first_quality": first_quality,
            },
            from_status="QUALITY_CHECKING",
            to_status="QUALITY_CHECKING",
            quality_claim_token=quality_claim_token,
        )

        if ai_plan_matches:
            body_normalized = body_before
            normalized_attributions = []
            body_after, removed = ai_body_after, ai_plan_matches
        else:
            body_normalized, normalized_attributions = normalize_photo_credits(body_before)
            body_after, removed = remove_promotional_blocks(body_normalized)
        before_stats = body_safety_stats(body_before)
        after_stats = body_safety_stats(body_after)
        removed_visible_chars = _removed_visible_chars(removed)
        net_visible_text_reduction = max(
            0, before_stats["visible_text_length"] - after_stats["visible_text_length"]
        )
        images_unchanged = before_stats["image_sources"] == after_stats["image_sources"]
        image_attributes_unchanged = (
            before_stats["image_attributes"] == after_stats["image_attributes"]
        )
        safe_to_apply = bool(
            (removed or normalized_attributions)
            and body_after != body_before
            and after_stats["visible_text_length"] >= 30
            and before_stats["parse_ok"]
            and after_stats["parse_ok"]
            and before_stats["image_count"] == after_stats["image_count"]
            and images_unchanged
            and image_attributes_unchanged
            and removed_visible_chars <= MAX_AI_REPAIR_REMOVED_CHARS
        )
        repair.update({
            "removed_count": len(removed) if safe_to_apply else 0,
            "attribution_normalized_count": (
                len(normalized_attributions) if safe_to_apply else 0
            ),
            "attribution_changes": (
                _audit_matches(normalized_attributions) if safe_to_apply else []
            ),
            "before": before_stats,
            "after": after_stats,
            "cumulative_removed_visible_chars": removed_visible_chars,
            "net_visible_text_reduction": net_visible_text_reduction,
            "removed_visible_chars_limit": MAX_AI_REPAIR_REMOVED_CHARS,
            "cumulative_limit_exceeded": (
                removed_visible_chars > MAX_AI_REPAIR_REMOVED_CHARS
            ),
            "candidate_created": safe_to_apply,
            "candidate_committed": False,
        })
        if not safe_to_apply:
            repair.update({"outcome": "failed", "safety_check_passed": False})
            safety_error = (
                f"整次局部修复累计删除可见文字超过 {MAX_AI_REPAIR_REMOVED_CHARS} 字上限"
                if removed_visible_chars > MAX_AI_REPAIR_REMOVED_CHARS
                else "优化后正文过短、正文未变化或图片地址发生变化"
            )
            final_quality = {**first_quality, "promotion_repair": repair}
            repo.save_quality(
                article_id,
                final_quality,
                connection,
                status="NEEDS_REVIEW",
                quality_claim_token=quality_claim_token,
            )
            repo.add_article_event(
                article_id,
                "AUTO_REPAIR_FINISHED",
                connection,
                message="自动优化未通过安全校验，正文保持不变并转人工审核",
                payload={
                    "outcome": "failed",
                    "final_status": "NEEDS_REVIEW",
                    "destination": "人工审核（自动优化安全校验未通过）",
                    "error": safety_error,
                },
                from_status="NEEDS_REVIEW",
                to_status="NEEDS_REVIEW",
                quality_claim_token=quality_claim_token,
            )
            return "NEEDS_REVIEW"

        repair.update({"applied": False, "safety_check_passed": True})

        second_error: Exception | None = None
        try:
            second_quality = evaluate(
                title=current.get("title_final", ""),
                body=body_after,
                channels=current.get("channels", []),
                llm=_make_llm(config),
                llm_fallback=_make_fallback_llm(config),
            )
            if not isinstance(second_quality, dict):
                raise TypeError("二次质检返回结果格式错误")
            second_quality = _clear_non_blocking_photo_advisory(
                second_quality,
                body=body_after,
            )
        except Exception as exc:  # uncertainty leaves the canonical body untouched
            logger.exception("二次质检失败 article_id=%s", article_id)
            second_error = exc
            second_quality = _second_quality_error(first_quality, exc)

        suggested_title = second_quality.get("title_after") or current.get("title_final", "")
        if second_error is None and suggested_title != current.get("title_final", ""):
            # This repair is intentionally body-only. Do not let a title fix
            # become a hidden side effect of the second full quality pass.
            second_quality = dict(second_quality)
            issues = dict(second_quality.get("issues") or {})
            title_problems = list(issues.get("title_problems") or [])
            title_problems.append("局部正文修复不修改标题，标题修复需人工确认")
            issues["title_problems"] = title_problems
            second_quality.update({
                "pass": False,
                "needs_review": True,
                "score": min(int(second_quality.get("score") or 50), 50),
                "issues": issues,
                "reason": "候选正文已生成，但标题修复需人工确认",
                "title_after": current.get("title_final", ""),
                "title_fix_method": "blocked_during_promotion_repair",
            })

        _record_quality_result(
            article_id,
            second_quality,
            2,
            connection,
            status="QUALITY_CHECKING",
            quality_claim_token=quality_claim_token,
        )
        second_passed = second_error is None and _quality_passes(second_quality)
        second_quality_round2 = second_quality
        followup_matches: list[dict[str, Any]] = []
        followup_quality: dict[str, Any] | None = None
        followup_error: str | None = None
        followup_before_stats: dict[str, Any] = {}
        followup_after_stats: dict[str, Any] = {}
        followup_rounds = 0
        quality_round = 2
        # 模型每轮只稳定地看到一部分脏内容：第一轮删掉引流块，第二轮才报出段尾的图片
        # 署名。只要上一轮质检明确给出可执行的局部修复计划，就再修一轮，直到通过、
        # 出错或用完 MAX_AI_REPAIR_ROUNDS 轮。每轮都跑同一套逐字校验、正文与图片安全
        # 不变量和累计删除字数上限，最后仍须整体通过质检才提交。
        while (
            second_error is None
            and not second_passed
            and followup_error is None
            and followup_rounds + 1 < MAX_AI_REPAIR_ROUNDS
            and _quality_allows_followup_body_repair(second_quality)
        ):
            removal_verifier.body = body_after
            round_body, round_matches, round_error = apply_repair_plan(
                body_after,
                second_quality.get("repair_plans"),
                verifier=removal_verifier,
            )
            round_safe, round_before_stats, round_after_stats = _followup_body_is_safe(
                body_after, round_body
            )
            if not round_safe:
                round_error = round_error or "二次局部修复未通过正文或图片安全校验"
                round_matches = []
            if round_error is not None or not round_matches:
                # 这一轮修不动就停下：再调用一次模型也只会拿到同一份计划。
                followup_error = round_error
                if followup_quality is None:
                    followup_quality = second_quality
                break
            if not followup_before_stats:
                followup_before_stats = round_before_stats
            followup_after_stats = round_after_stats
            followup_rounds += 1
            quality_round += 1
            try:
                followup_quality = evaluate(
                    title=current.get("title_final", ""),
                    body=round_body,
                    channels=current.get("channels", []),
                    llm=_make_llm(config),
                    llm_fallback=_make_fallback_llm(config),
                )
                if not isinstance(followup_quality, dict):
                    raise TypeError("最终质检返回结果格式错误")
            except Exception as exc:  # fail closed; keep canonical body
                followup_error = str(exc)[:300]
                followup_quality = _second_quality_error(second_quality, exc)
            if followup_quality.get("title_after") not in {
                None,
                current.get("title_final", ""),
            }:
                followup_error = "最终质检提出标题修改，局部修复不自动改标题"
                followup_quality = dict(followup_quality)
                followup_quality["pass"] = False
                followup_quality["needs_review"] = True
            _record_quality_result(
                article_id,
                followup_quality,
                quality_round,
                connection,
                status="QUALITY_CHECKING",
                quality_claim_token=quality_claim_token,
            )
            body_after = round_body
            followup_matches = [*followup_matches, *round_matches]
            cumulative_removed_visible_chars = _removed_visible_chars(
                [*removed, *followup_matches]
            )
            if cumulative_removed_visible_chars > MAX_AI_REPAIR_REMOVED_CHARS:
                followup_error = (
                    "整次局部修复累计删除可见文字超过 "
                    f"{MAX_AI_REPAIR_REMOVED_CHARS} 字上限"
                    f"（实际 {cumulative_removed_visible_chars} 字）"
                )
                second_quality = dict(followup_quality)
                issues = dict(second_quality.get("issues") or {})
                semantic_problems = list(issues.get("semantic_problems") or [])
                semantic_problems.append(followup_error)
                issues["semantic_problems"] = semantic_problems
                semantic_check = dict(second_quality.get("semantic_check") or {})
                semantic_check["needs_review"] = True
                second_quality.update({
                    "pass": False,
                    "needs_review": True,
                    "decision": "manual_review",
                    "decision_reason": followup_error,
                    "score": min(int(second_quality.get("score") or 50), 50),
                    "issues": issues,
                    "semantic_check": semantic_check,
                    "reason": followup_error,
                })
            else:
                second_quality = followup_quality
            second_passed = followup_error is None and _quality_passes(second_quality)
        if followup_matches:
            removed = [*removed, *followup_matches]
            repair["removed_count"] = len(removed)
        after_stats = body_safety_stats(body_after)
        images_unchanged = before_stats["image_sources"] == after_stats["image_sources"]
        image_attributes_unchanged = (
            before_stats["image_attributes"] == after_stats["image_attributes"]
        )
        cumulative_removed_visible_chars = _removed_visible_chars(removed)
        net_visible_text_reduction = max(
            0, before_stats["visible_text_length"] - after_stats["visible_text_length"]
        )
        repair.update({
            "after": after_stats,
            "cumulative_removed_visible_chars": cumulative_removed_visible_chars,
            "net_visible_text_reduction": net_visible_text_reduction,
            "removed_visible_chars_limit": MAX_AI_REPAIR_REMOVED_CHARS,
            "cumulative_limit_exceeded": (
                cumulative_removed_visible_chars > MAX_AI_REPAIR_REMOVED_CHARS
            ),
        })
        applied_event_message: str | None = None
        applied_event_payload: dict[str, Any] | None = None
        if second_passed:
            repair.update({"applied": True, "candidate_committed": True})
            applied_event_message = (
                f"候选正文二次质检通过，已提交 {len(normalized_attributions)} 处署名规范化"
                if normalized_attributions and not removed
                else f"候选正文二次质检通过，已提交 {len(removed)} 处局部修复"
            )
            applied_event_payload = {
                "removed_blocks": _audit_matches(removed),
                "removed_count": len(removed),
                "attribution_normalized_count": len(normalized_attributions),
                "attribution_changes": _audit_matches(normalized_attributions),
                "attribution_only": bool(normalized_attributions and not removed),
                "match_count": len(removed) + len(normalized_attributions),
                "body_length_before": before_stats["body_length"],
                "body_length_after": after_stats["body_length"],
                "body_sha256_before": before_stats["sha256"],
                "body_sha256_after": after_stats["sha256"],
                "image_count_before": before_stats["image_count"],
                "image_count_after": after_stats["image_count"],
                "image_sources_unchanged": images_unchanged,
                "image_attributes_unchanged": image_attributes_unchanged,
                "cumulative_removed_visible_chars": cumulative_removed_visible_chars,
                "removed_visible_chars_limit": MAX_AI_REPAIR_REMOVED_CHARS,
                "net_visible_text_reduction": net_visible_text_reduction,
                "title_unchanged": True,
            }
            if followup_matches:
                applied_event_payload["followup_repair"] = {
                    "removed_blocks": _audit_matches(followup_matches),
                    "removed_count": len(followup_matches),
                    "rounds": followup_rounds,
                    "quality_round": quality_round,
                }
        repair.update({
            "outcome": "error" if second_error else ("passed" if second_passed else "failed"),
            "second_quality": second_quality,
        })
        if removal_verifier.records:
            # 后续修复轮可能追加了新的核验判定，用最终快照覆盖首轮写入的记录。
            repair["removal_verification"] = removal_verifier.records
        if followup_matches or followup_error:
            repair["followup_repair"] = {
                "attempted": True,
                "applied": bool(second_passed and followup_matches),
                "outcome": "passed" if second_passed and followup_matches else (
                    "failed" if followup_error or followup_matches else "skipped"
                ),
                "matches": _audit_matches(followup_matches),
                "rounds": followup_rounds,
                "quality_round": quality_round,
                "before": followup_before_stats,
                "after": followup_after_stats,
                "error": followup_error,
                "cumulative_removed_visible_chars": cumulative_removed_visible_chars,
                "removed_visible_chars_limit": MAX_AI_REPAIR_REMOVED_CHARS,
                "cumulative_limit_exceeded": (
                    cumulative_removed_visible_chars > MAX_AI_REPAIR_REMOVED_CHARS
                ),
            }
        if followup_error:
            repair["error"] = followup_error
        elif second_error is not None:
            repair["error"] = str(second_error)[:300]
        if followup_matches or followup_error:
            repair["second_quality_round2"] = second_quality_round2
        final_quality = {**second_quality, "promotion_repair": repair}
        target = (
            "READY_TO_PUBLISH"
            if second_passed
            else "NEEDS_REVIEW"
        )
        repo.save_quality(
            article_id,
            final_quality,
            connection,
            status=target,
            quality_claim_token=quality_claim_token,
            body_html=body_after if second_passed else None,
        )
        if second_passed:
            current["body_html"] = body_after
            repo.add_article_event(
                article_id,
                "AUTO_REPAIR_APPLIED",
                connection,
                message=applied_event_message,
                payload=applied_event_payload,
                from_status=target,
                to_status=target,
                quality_claim_token=quality_claim_token,
            )
        final_status = target
        if target == "READY_TO_PUBLISH" and int(current.get("upstream_archive_id") or 0) > 0:
            repo.transition_status(
                article_id,
                "ALREADY_PUBLISHED",
                connection,
                event_type="ALREADY_PUBLISHED_DETECTED",
                message="素材接口已返回非零 archive_id，本阶段不重复发布",
                quality_claim_token=quality_claim_token,
            )
            final_status = "ALREADY_PUBLISHED"
        if final_status == "READY_TO_PUBLISH":
            dedup_status = _apply_title_dedup(article_id, current, config, connection)
            if dedup_status:
                final_status = dedup_status
        finish_payload: dict[str, Any] = {
            "outcome": repair["outcome"],
            "candidate_committed": repair["candidate_committed"],
            "final_status": final_status,
            "destination": "发布队列" if final_status == "READY_TO_PUBLISH" else (
                "不重复发布" if final_status == "ALREADY_PUBLISHED" else (
                    "标题查重拦截" if final_status == "TITLE_DUPLICATE" else "人工审核"
                )
            ),
            "cumulative_removed_visible_chars": cumulative_removed_visible_chars,
            "removed_visible_chars_limit": MAX_AI_REPAIR_REMOVED_CHARS,
        }
        if second_error is None:
            finish_payload["second_quality"] = second_quality
        else:
            finish_payload["error"] = str(second_error)[:300]
        if followup_error:
            finish_payload["repair_error"] = followup_error
        repo.add_article_event(
            article_id,
            "AUTO_REPAIR_FINISHED",
            connection,
            message=(
                "候选正文二次完整质检异常，原正文未改动并转人工审核"
                if second_error is not None
                else (
                    "最终质检完成，但累计删除超过安全上限，原正文未改动并转人工审核"
                    if repair.get("cumulative_limit_exceeded")
                    else (
                        "局部修复后二次质检通过，候选正文已提交"
                        if final_status in {"READY_TO_PUBLISH", "ALREADY_PUBLISHED"}
                        else (
                            "标题查重命中近窗口已发布文章，已取消自动发布"
                            if final_status == "TITLE_DUPLICATE"
                            else "候选正文二次质检未通过，原正文未改动并转人工审核"
                        )
                    )
                )
            ),
            payload=finish_payload,
            from_status=final_status,
            to_status=final_status,
            quality_claim_token=quality_claim_token,
        )
        return final_status
    except Exception as exc:  # noqa: BLE001 - one bad item must not stop a batch
        transitioned = repo.transition_status(
            article_id,
            "ERROR",
            connection,
            event_type="QUALITY_ERROR",
            message=str(exc)[:300],
            quality_claim_token=quality_claim_token,
        )
        if transitioned is None and quality_claim_token:
            # A newer worker owns the lease.  Its result is authoritative; do
            # not report the stale worker as an article-level quality error.
            current = repo.get_article(article_id, connection)
            logger.warning("质检租约已被其他 worker 接管 article_id=%s", article_id)
            return current.get("status", "") if current else ""
        logger.exception("质检失败 article_id=%s", article_id)
        return "ERROR"
    finally:
        repo.release_quality_claim(article_id, quality_claim_token, connection)


def recheck_article(article_id: int, config: AppConfig, connection) -> str:
    """Run one manually selected review item through the current quality rules."""

    claimed = repo.claim_quality_recheck(article_id, connection)
    if claimed is None:
        raise ValueError("当前文章不在待人工审核状态，不能重新质检")
    return _process_article(claimed, config, connection)


def _quality_failure_item(article: dict[str, Any], connection) -> dict[str, str]:
    failed = repo.get_article(article["id"], connection) or article
    return {
        "source": str(failed.get("source") or "未知来源"),
        "source_url": str(failed.get("source_url") or ""),
        "error": str(failed.get("error") or "质检失败")[:300],
    }


def _process_one_article(
    article: dict[str, Any], config: AppConfig, connection
) -> tuple[int, str, dict[str, str] | None]:
    """Process one article and classify its outcome for the run summary."""

    try:
        state = _process_article(article, config, connection)
    except Exception as exc:  # noqa: BLE001
        logger.exception("素材处理失败")
        return int(article.get("id") or 0), "ERROR", {
            "source": str(article.get("source") or "未知来源"),
            "source_url": str(article.get("source_url") or ""),
            "error": str(exc)[:300],
        }
    if state == "ERROR":
        return int(article.get("id") or 0), state, _quality_failure_item(article, connection)
    return int(article.get("id") or 0), state, None


def _process_article_worker(
    database_path: str, config: AppConfig, article: dict[str, Any]
) -> tuple[int, str, dict[str, str] | None]:
    """Worker entry: an isolated connection keeps transactions thread-local."""

    connection = _connect(database_path)
    try:
        return _process_one_article(article, config, connection)
    finally:
        connection.close()


def _process_articles(
    articles: list[dict[str, Any]],
    config: AppConfig,
    database_path: str,
    connection,
    *,
    workers: int,
) -> list[tuple[int, str, dict[str, str] | None]]:
    """Run quality processing; LLM-bound work overlaps when workers > 1.

    Concurrency safety relies on the existing quality lease: only one worker
    can claim an article, and every write is guarded by the claim token.
    """

    if not articles:
        return []
    if workers <= 1 or len(articles) == 1:
        return [_process_one_article(article, config, connection) for article in articles]
    results: list[tuple[int, str, dict[str, str] | None]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(articles))) as pool:
        futures = [
            pool.submit(_process_article_worker, database_path, config, article)
            for article in articles
        ]
        for future in futures:
            results.append(future.result())
    return results


def _try_acquire_run_lock(database_path: str):
    """Acquire a crash-safe, non-blocking lock shared by all processes."""

    lock_path = f"{database_path}.run.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return None
        raise
    return handle


def run_once(config: AppConfig, *, database_path: str | None = None) -> dict[str, Any]:
    """Run one ingestion pass, with a process-wide single-flight lock."""

    resolved_database_path = database_path or config.database_path
    lock_handle = _try_acquire_run_lock(resolved_database_path)
    if lock_handle is None:
        return {
            "run_id": None,
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "errors": 0,
            "skipped": True,
            "already_running": True,
            "message": "已有其他进程正在运行，本轮跳过",
        }
    try:
        return _run_once_locked(config, database_path=resolved_database_path)
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def _run_once_locked(config: AppConfig, *, database_path: str | None = None) -> dict[str, Any]:
    """Fetch enabled sources, upsert materials and process eligible articles."""
    database_path = database_path or config.database_path
    conn = _connect(database_path)
    try:
        run_id = repo.start_run_log("INGEST", conn, details={"started_at": _utc_now()})
    except Exception:
        conn.close()
        raise
    fetched = inserted = updated = errors = 0
    status_counts: dict[str, int] = {}
    failed_items: list[dict[str, str]] = []
    publish_result: dict[str, Any] | None = None
    try:
        enabled_sources = repo.list_sources(conn, include_disabled=False)
        source_codes = [item["code"] for item in enabled_sources]
        if not source_codes:
            # Existing draft-confirmation jobs must continue even when
            # ingestion is temporarily disabled or has no active source.
            publish_result = publish_ready_articles(config, conn)
            result = {"run_id": run_id, "fetched": 0, "inserted": 0, "updated": 0,
                      "message": "没有启用的 source", "publish": publish_result}
            repo.finish_run_log(run_id, conn, details=result)
            repo.set_setting("last_run_at", _utc_now(), conn)
            repo.set_setting("last_error", "", conn)
            return result
        if not config.material_configured:
            # A material outage must not prevent already queued draft-result
            # confirmations from being reconciled.
            publish_result = publish_ready_articles(config, conn)
            raise MaterialClientError("素材接口尚未配置 SK/caller")
        client = MaterialClient(
            config.material_base_url,
            config.material_api_key,
            config.material_caller,
            config.request_timeout,
        )
        fetched_result = client.fetch_all(
            source_codes,
            hours=config.fetch_hours,
            limit=config.fetch_limit,
        )
        fetched = len(fetched_result.items)
        pending: list[dict[str, Any]] = []
        for raw in fetched_result.items:
            try:
                item = normalize_item(raw)
                saved = repo.upsert_material(item, conn)
                article = saved["article"]
                if saved["created"]:
                    inserted += 1
                else:
                    updated += 1
                pending.append(article)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.exception("素材处理失败")
                failed_items.append({
                    "source": str(raw.get("source") or "未知来源") if isinstance(raw, dict) else "未知来源",
                    "source_url": str(raw.get("source_url") or "") if isinstance(raw, dict) else "",
                    "error": str(exc)[:300],
                })
        # Transiently-failed articles (LLM outage) scheduled by this or an
        # earlier pass are eligible again now that they are back in RECEIVED.
        try:
            scheduled_ids = schedule_transient_rechecks(config, conn)
        except Exception:  # noqa: BLE001 - retries are best-effort
            logger.exception("瞬时失败自动重试调度失败")
            scheduled_ids = []
        if scheduled_ids:
            seen_ids = {int(article["id"]) for article in pending if article.get("id")}
            for retry_article in repo.list_articles(
                conn, status="RECEIVED", limit=config.transient_recheck_batch_limit
            ):
                if int(retry_article["id"]) in scheduled_ids and int(retry_article["id"]) not in seen_ids:
                    pending.append(retry_article)

        # Scan for orphaned RECEIVED articles that were never processed
        # (e.g., due to LLM timeout or other transient failures during initial ingestion)
        try:
            last_orphan_scan = repo.get_setting("last_orphan_scan_at", default=None, connection=conn)
            now_utc = datetime.now(timezone.utc)
            should_scan = False
            if not last_orphan_scan:
                should_scan = True
            else:
                try:
                    last_scan_time = datetime.fromisoformat(last_orphan_scan.replace("Z", "+00:00"))
                    elapsed = (now_utc - last_scan_time).total_seconds()
                    if elapsed > 600:  # 10 minutes
                        should_scan = True
                except (ValueError, AttributeError):
                    should_scan = True

            if should_scan:
                seen_ids = {int(article["id"]) for article in pending if article.get("id")}
                orphan_candidates = repo.list_articles(conn, status="RECEIVED", limit=200)
                stale_orphans = []
                for art in orphan_candidates:
                    art_id = int(art["id"])
                    if art_id in seen_ids:
                        continue
                    # Only retry articles that have been stuck for more than 10 minutes
                    try:
                        updated_at = datetime.fromisoformat(art["updated_at"].replace("Z", "+00:00"))
                        if (now_utc - updated_at).total_seconds() > 600:
                            stale_orphans.append(art)
                    except (ValueError, AttributeError, KeyError):
                        continue

                if stale_orphans:
                    logger.info(f"发现 {len(stale_orphans)} 篇历史遗留的 RECEIVED 文章，加入处理队列")
                    pending.extend(stale_orphans)

                repo.set_setting("last_orphan_scan_at", now_utc.isoformat(), connection=conn)
        except Exception:  # noqa: BLE001 - orphan scanning is best-effort
            logger.exception("历史遗留文章扫描失败")

        for _article_id, state, failed in _process_articles(
            pending, config, database_path, conn, workers=config.quality_workers
        ):
            status_counts[state] = status_counts.get(state, 0) + 1
            if state == "ERROR":
                errors += 1
                if failed is not None:
                    failed_items.append(failed)
        publish_result = publish_ready_articles(config, conn)
        # 无论 publisher 是否启用，publish_ready_articles 都会执行 recovery（P2-4 修复）
        if publish_result.get("draft_created"):
            status_counts["DRAFT_CREATED"] = status_counts.get("DRAFT_CREATED", 0) + int(publish_result["draft_created"])
        if publish_result.get("published"):
            status_counts["PUBLISHED"] = status_counts.get("PUBLISHED", 0) + int(publish_result["published"])
        if publish_result.get("failed"):
            status_counts["PUBLISH_FAILED"] = status_counts.get("PUBLISH_FAILED", 0) + int(publish_result["failed"])
        if publish_result.get("confirming"):
            status_counts["DRAFT_CONFIRMING"] = status_counts.get("DRAFT_CONFIRMING", 0) + int(publish_result["confirming"])
        if publish_result.get("confirmed"):
            confirmed_published = int(publish_result.get("confirmed_published", 0))
            status_counts["DRAFT_CREATED"] = status_counts.get("DRAFT_CREATED", 0) + max(
                0, int(publish_result["confirmed"]) - confirmed_published
            )
        if publish_result.get("mapping_blocked"):
            status_counts["MAPPING_BLOCKED"] = status_counts.get("MAPPING_BLOCKED", 0) + int(publish_result["mapping_blocked"])
        if publish_result.get("duplicate_skipped"):
            status_counts["SOURCE_DUPLICATE"] = status_counts.get("SOURCE_DUPLICATE", 0) + int(publish_result["duplicate_skipped"])
        if publish_result.get("title_duplicate_skipped"):
            status_counts["TITLE_DUPLICATE"] = status_counts.get("TITLE_DUPLICATE", 0) + int(publish_result["title_duplicate_skipped"])
        message = f"本轮完成，草稿创建 {publish_result.get('draft_created', 0)} 篇"
        if publish_result.get("published"):
            message += f"，直接发布 {publish_result.get('published', 0)} 篇"
        if publish_result.get("recovered"):
            message += f"，自动恢复 {publish_result.get('recovered', 0)} 篇"
        if publish_result.get("timed_out"):
            message += f"，超时恢复 {publish_result.get('timed_out', 0)} 篇"
        if publish_result.get("failed"):
            message += f"，失败 {publish_result.get('failed', 0)} 篇"
        if publish_result.get("confirming"):
            message += f"，{publish_result.get('confirming', 0)} 篇进入自动核对"
        if publish_result.get("title_duplicate_skipped"):
            message += f"，标题查重拦截 {publish_result.get('title_duplicate_skipped')} 篇"
        result = {
            "run_id": run_id,
            "fetched": fetched,
            "inserted": inserted,
            "updated": updated,
            "errors": errors,
            "status_counts": status_counts,
            "failed_items": failed_items,
            "message": message,
        }
        result["publish"] = publish_result
        repo.finish_run_log(
            run_id,
            conn,
            status="SUCCESS" if errors == 0 else "PARTIAL",
            fetched_count=fetched,
            inserted_count=inserted,
            updated_count=updated,
            error_count=errors,
            details=result,
        )
        repo.set_setting("last_run_at", _utc_now(), conn)
        repo.set_setting("last_error", "" if errors == 0 else f"本轮有 {errors} 条素材处理失败", conn)
        return result
    except Exception as exc:
        logger.exception("素材拉取失败")
        repo.finish_run_log(
            run_id,
            conn,
            status="ERROR",
            fetched_count=fetched,
            inserted_count=inserted,
            updated_count=updated,
            error_count=errors + 1,
            error=str(exc)[:500],
            details={"message": str(exc), **({"publish": publish_result} if publish_result else {})},
        )
        repo.set_setting("last_error", str(exc)[:500], conn)
        repo.set_setting("last_run_at", _utc_now(), conn)
        return {
            "run_id": run_id,
            "fetched": fetched,
            "inserted": inserted,
            "updated": updated,
            "errors": errors + 1,
            "message": str(exc),
        }
    finally:
        conn.close()


class RunController:
    """Single-flight controller shared by manual and scheduled runs."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._thread = threading.Thread(target=self._run, name="material-run", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._running = False
                raise
            return True

    def _run(self) -> None:
        result: dict[str, Any] = {
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "errors": 1,
            "message": "运行任务意外终止",
        }
        try:
            result = run_once(self.config)
        except Exception as exc:  # run_once normally converts failures at its boundary
            logger.exception("运行任务意外退出")
            result = {
                "fetched": 0,
                "inserted": 0,
                "updated": 0,
                "errors": 1,
                "message": str(exc)[:500],
            }
        finally:
            with self._lock:
                self._last_result = result
                self._running = False

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the active run; return whether it has finished."""

        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "last_result": self._last_result,
            }


# The exact `quality.reason` strings produced when the primary *and* fallback
# LLM both fail transiently.  An article parked in manual review with one of
# these reasons failed because of an outage, not because of its content, so a
# later pass should retry it automatically (bounded attempts).
TRANSIENT_FAILURE_REASONS = (
    "AI 服务暂时不可用，已重试仍未返回，需要人工确认",
    "AI 语义质检失败，需要人工确认",
    "AI 服务调用失败，需要人工确认",
    "AI 返回格式无效，需要人工确认",
)


def schedule_transient_rechecks(config: AppConfig, connection) -> list[int]:
    """Move due transiently-failed articles back into the quality pipeline.

    Returns the ids actually scheduled.  The function is cheap and safe to
    call on every ingestion pass: candidates are bounded by config, need a
    minimum delay, and every article carries its own attempt counter.
    """

    if not config.transient_recheck_enabled or not config.llm_configured:
        return []
    retryable_reasons = [
        reason for reason in TRANSIENT_FAILURE_REASONS if reason
    ]
    if not retryable_reasons:
        return []
    delay = max(60, int(config.transient_recheck_delay_seconds))
    updated_before = (
        datetime.now(timezone.utc) - timedelta(seconds=delay)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    candidates = repo.list_transient_recheck_candidates(
        retryable_reasons=retryable_reasons,
        updated_before=updated_before,
        limit=config.transient_recheck_batch_limit,
        max_attempts=config.transient_recheck_max_attempts,
        connection=connection,
    )
    scheduled: list[int] = []
    for article_id in candidates:
        try:
            if repo.request_transient_recheck(article_id, connection):
                scheduled.append(article_id)
        except Exception:  # noqa: BLE001 - one bad row must not stop the batch
            logger.exception("瞬时失败自动重试入队失败 article_id=%s", article_id)
    if scheduled:
        logger.info(
            "LLM 瞬时失败自动重试：已重新入队 %d 篇（上限 %d）",
            len(scheduled), config.transient_recheck_batch_limit,
        )
    return scheduled


class TransientRecheckController:
    """Single-flight controller for the transient-failure retry pass."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name="transient-recheck", daemon=True
            )
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._running = False
                raise
            return True

    def _run(self) -> None:
        result: dict[str, Any] = {"scheduled": 0, "message": "没有需要重试的瞬时失败文章"}
        try:
            result = self._run_pass()
        except Exception as exc:  # noqa: BLE001
            logger.exception("瞬时失败重试任务意外退出")
            result = {"scheduled": 0, "message": str(exc)[:300]}
        finally:
            with self._lock:
                self._last_result = result
                self._running = False

    def _run_pass(self) -> dict[str, Any]:
        """Queue due candidates; a scheduled article is picked up by the next
        ingestion pass (or the current one when it runs afterwards)."""

        conn = _connect(self.config.database_path)
        try:
            scheduled = schedule_transient_rechecks(self.config, conn)
            return {
                "scheduled": len(scheduled),
                "article_ids": scheduled,
                "message": (
                    f"已重新入队 {len(scheduled)} 篇瞬时失败文章"
                    if scheduled else "没有需要重试的瞬时失败文章"
                ),
            }
        finally:
            conn.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "last_result": self._last_result,
            }
