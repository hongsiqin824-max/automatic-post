"""Flask application and JSON endpoints for the pre-publish workflow."""

from __future__ import annotations

import html
import json
import os
import sqlite3
from urllib.parse import urlparse

import requests as _requests
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request

from . import db, repository as repo
from .catalog import seed_catalog
from .config import AppConfig, ensure_instance_dir
from .db import get_db, init_app as init_db_app, init_db
from .services.material_client import cdn_url
from .services.article_images import (
    ArticleImageError,
    build_publish_body,
    effective_litpic,
    fallback_litpic_for_tabs,
)
from .services.open_platform import AUTH_STATUS_LABELS, OpenPlatformClient, auth_record_summary, build_draft_url
from .services.pipeline import RunController, recheck_article
from .services.scheduler import Scheduler
from .services.dqd_open_client import DqdOpenClientError
from .services.publisher import (
    DraftClaimSkipped,
    DraftConfirmationController,
    create_draft_for_article,
)
from .services.preview_html import sanitize_preview_html
from .services.quality import html_to_text
from .statuses import STATUS_LABELS

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

STATUS_ALIASES = {
    "ingested": "RECEIVED",
    "received": "RECEIVED",
    "quality_checking": "QUALITY_CHECKING",
    "needs_review": "NEEDS_REVIEW",
    "ready_to_publish": "READY_TO_PUBLISH",
    "publishing": "PUBLISHING",
    "draft_confirming": "DRAFT_CONFIRMING",
    "draft_created": "DRAFT_CREATED",
    "publish_failed": "PUBLISH_FAILED",
    "published": "PUBLISHED",
    "already_published": "ALREADY_PUBLISHED",
    "source_duplicate": "SOURCE_DUPLICATE",
    "rejected": "REJECTED",
    "failed": "ERROR",
    "error": "ERROR",
}

EVENT_LABELS = {
    "MATERIAL_RECEIVED": "素材进入系统",
    "QUALITY_STARTED": "开始自动质检",
    "QUALITY_CLAIMED": "领取质检任务",
    "QUALITY_SAVED": "保存质检结果",
    "QUALITY_RESULT": "自动质检完成",
    "QUALITY_ERROR": "自动质检失败",
    "LINKS_REMOVED": "已清理可跳转内容",
    "TITLE_FIXED": "自动修正标题",
    "AUTO_REPAIR_TRIGGERED": "已触发自动优化",
    "AUTO_REPAIR_APPLIED": "已应用自动优化",
    "AUTO_REPAIR_FINISHED": "自动优化完成",
    "QUALITY_RECHECK_REQUESTED": "重新执行自动质检",
    "MANUAL_REVIEW": "人工审核",
    "ALREADY_PUBLISHED_DETECTED": "发现已有文章 ID",
    "DRAFT_CREATE_STARTED": "开始创建草稿",
    "DRAFT_CREATED": "草稿创建完成",
    "PUBLISHED": "直接发布完成",
    "DRAFT_CREATE_FAILED": "草稿创建失败",
    "DRAFT_CREATE_BLOCKED": "草稿创建被阻止",
    "DRAFT_RESULT_UNKNOWN": "创建结果待自动核对",
    "DRAFT_CONFIRMATION_CLAIMED": "开始自动核对草稿结果",
    "DRAFT_CONFIRMATION_RECORDED": "草稿结果核对完成",
    "DRAFT_RETRY_STARTED": "开始重新创建草稿",
    "DRAFT_RETRY_SUCCEEDED": "重新创建草稿成功",
    "DRAFT_RETRY_FAILED": "重新创建草稿失败",
    "DRAFT_RETRY_BLOCKED": "草稿重试被阻止",
    "DRAFT_ALREADY_EXISTS": "草稿已存在",
    "PUBLISHING_RECOVERED": "草稿状态已自动恢复",
    "PUBLISHING_TIMED_OUT": "草稿创建超时自动恢复",
    "SOURCE_DUPLICATE_DETECTED": "发现同来源重复文章",
    "STATUS_CHANGED": "状态更新",
}


def format_beijing_time(value: Any) -> str:
    """Format stored UTC timestamps for operators in Beijing time."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _status_value(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return STATUS_ALIASES.get(text.lower(), text.upper())


def _status_key(value: str | None) -> str:
    return str(value or "").lower()


def _json_object_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def _strict_boolean(payload: dict[str, Any], field: str, *, default: Any = None) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是 JSON boolean")
    return value


def _publish_account_fields(payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if creating or "dqd_user_id" in payload:
        user_id = payload.get("dqd_user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("发布账号 ID 必须是正整数")
        fields["dqd_user_id"] = user_id
    if creating or "user_name" in payload:
        user_name = payload.get("user_name")
        if not isinstance(user_name, str) or not user_name.strip():
            raise ValueError("发布账号名称不能为空")
        fields["user_name"] = user_name.strip()
    if "enabled" in payload:
        fields["enabled"] = _strict_boolean(payload, "enabled")
    elif creating:
        fields["enabled"] = True
    if not creating and not fields:
        raise ValueError("至少提供一个需要更新的字段")
    return fields


def _tab_view(tab: dict | None) -> dict:
    tab = dict(tab or {})
    tab.setdefault("name", "未配置")
    tab.setdefault("id", None)
    # Older databases may not have the per-tab mode column until migration
    # runs; keep the UI deterministic and default those rows to draft mode.
    tab.setdefault("publish_mode", 0)
    tab.setdefault("fallback_litpic", "")
    return tab


def _source_view(source: dict) -> dict:
    item = dict(source)
    item["name"] = item.get("display_name") or item.get("code")
    override = item.get("publish_mode_override")
    if override not in (None, 0, 1):
        override = None
    item["publish_mode_override"] = override
    tab_modes = {
        int(tab.get("publish_mode", 0))
        for tab in (item.get("tabs") or [])
        if tab.get("publish_mode", 0) in (0, 1)
    }
    mode_labels = {0: "创建草稿", 1: "直接发布"}
    if override in mode_labels:
        item["publish_mode_effective"] = override
        item["publish_mode_label"] = f"来源强制 · {mode_labels[override]}"
    elif len(tab_modes) == 1:
        effective = next(iter(tab_modes))
        item["publish_mode_effective"] = effective
        item["publish_mode_label"] = f"跟随栏目 · {mode_labels[effective]}"
    elif len(tab_modes) > 1:
        item["publish_mode_effective"] = None
        item["publish_mode_label"] = "跟随栏目 · 模式冲突"
    else:
        item["publish_mode_effective"] = None
        item["publish_mode_label"] = "跟随栏目"
    return item


def _article_view(article: dict | None) -> dict | None:
    if article is None:
        return None
    item = dict(article)
    item["status_key"] = _status_key(item.get("status"))
    item["status_label"] = STATUS_LABELS.get(item.get("status", ""), item.get("status", "未知"))
    quality = item.get("quality") or {}
    issues = quality.get("issues") if isinstance(quality, dict) else {}
    issues = issues if isinstance(issues, dict) else {}
    title_problems = issues.get("title_problems") or []
    completeness = issues.get("completeness_problems") or []
    dirty = issues.get("dirty_content") or []
    item["quality"] = quality
    item["quality_passed"] = quality.get("pass") if isinstance(quality, dict) else None
    item["quality_reason"] = quality.get("reason", "") if isinstance(quality, dict) else ""
    item["title_complete"] = not title_problems
    item["body_complete"] = not completeness
    item["has_ad"] = any("广告" in str(v) or "购买" in str(v) for v in dirty)
    item["has_dirty"] = bool(dirty)
    item["needs_review"] = bool(quality.get("needs_review")) if isinstance(quality, dict) else None
    item["body_excerpt"] = html_to_text(item.get("body_html", ""))[:360]
    item["cover_url"] = cdn_url(item.get("litpic"))
    # Keep the stored material cover for list/quality views, but show the
    # cover that the publisher will actually submit in the publish preview.
    item["publish_cover_path"] = effective_litpic(
        item,
        fallback_litpic=fallback_litpic_for_tabs(item.get("tabs")),
    )
    item["publish_cover_url"] = cdn_url(item["publish_cover_path"])
    draft_archive_id = int(item.get("dqd_archive_id") or 0)
    item["has_dqd_draft"] = draft_archive_id > 0
    item["draft_confirming"] = item["status_key"] == "draft_confirming"
    item["draft_confirmation_scheduled"] = bool(item.get("draft_next_confirm_at"))
    item["draft_recovered"] = item["status_key"] == "publish_failed" and draft_archive_id > 0
    publish_mode = item.get("publish_mode")
    publish_mode_conflict = False
    if publish_mode not in (0, 1) and item["status_key"] in {
        "ready_to_publish", "publish_failed", "mapping_blocked"
    }:
        try:
            resolution = repo.resolve_article_publish_mode(int(item["id"]))
            publish_mode_conflict = bool(resolution.get("conflict"))
            publish_mode = resolution.get("publish_mode")
        except (TypeError, ValueError):
            publish_mode = None
    item["effective_publish_mode"] = publish_mode if publish_mode in (0, 1) else None
    item["publish_mode_conflict"] = publish_mode_conflict
    is_direct_publish = item["effective_publish_mode"] == 1
    is_retry = item["status_key"] in {"publish_failed", "mapping_blocked"}
    item["draft_action_label"] = (
        "自动核对中"
        if item["draft_confirming"]
        else (
            "同步懂球帝状态"
            if draft_archive_id > 0
            else (
                "重新直接发布" if is_direct_publish and is_retry
                else "直接发布到懂球帝" if is_direct_publish
                else "重新创建懂球帝草稿" if is_retry
                else "创建懂球帝草稿"
            )
        )
    )
    item["draft_action_confirm"] = (
        "确认同步懂球帝状态吗？"
        if draft_archive_id > 0
        else (
            "确认直接发布这篇文章吗？提交成功后文章会立即上线，不能再作为草稿审核。"
            if is_direct_publish
            else "确认创建懂球帝草稿吗？"
        )
    )
    item["draft_notice"] = (
        "这条文章其实已经创建过懂球帝草稿，当前失败更可能发生在后续步骤，可直接打开链接。"
        if item["draft_recovered"]
        else ""
    )
    item["tabs"] = [_tab_view(tab) for tab in (item.get("tabs") or [])]
    item["tab_names"] = "、".join(tab.get("name", "") for tab in item["tabs"] if tab.get("name"))
    item["tab"] = _tab_view({
        "id": item.get("tab_id"),
        "name": item.get("tab_name") or "未配置栏目",
        "backend_tab_id": item.get("backend_tab_id"),
    })
    item["channels"] = [{"id": channel_id} for channel_id in (item.get("channels") or [])]
    item["draft_url"] = build_draft_url(item.get("dqd_archive_id"))
    return item


def _event_view(event: dict) -> dict:
    item = dict(event)
    target = event.get("to_status")
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        if event.get("payload_json"):
            try:
                decoded = json.loads(event["payload_json"])
                payload = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                pass
    item["payload"] = payload

    quality_round = payload.get("quality_round")
    if event_type == "QUALITY_RESULT" and quality_round == 1:
        item["label"] = "首轮自动质检完成"
    elif event_type == "QUALITY_RESULT" and quality_round == 2:
        item["label"] = "二次自动质检完成"
    else:
        item["label"] = EVENT_LABELS.get(event_type) or STATUS_LABELS.get(target or "", "状态更新")
    item["message"] = event.get("message") or {
        "QUALITY_SAVED": "质检结果已保存",
        "QUALITY_RESULT": "质检结果已记录",
        "AUTO_REPAIR_TRIGGERED": "首轮质检发现可安全自动优化的内容",
        "AUTO_REPAIR_APPLIED": "已应用高置信正文优化",
        "AUTO_REPAIR_FINISHED": "自动优化和二次质检已结束",
        "STATUS_CHANGED": "状态已更新",
    }.get(event_type, "")
    item["summary_rows"] = _event_summary_rows(event_type, payload)
    return item


def _event_summary_rows(event_type: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    """Turn repair/quality audit payloads into short operator-facing facts.

    Event payloads are an audit contract and older rows may be incomplete.
    Only render a fact when the corresponding value really exists; in
    particular, never turn a missing pass flag into a successful check.
    """

    rows: list[dict[str, str]] = []

    def add(label: str, value: Any, *, tone: str = "") -> None:
        if value is None or value == "" or value == []:
            return
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        rows.append({"label": label, "value": str(value), "tone": tone})

    def bool_result(value: Any, true_text: str, false_text: str) -> tuple[str, str] | None:
        if value is True:
            return true_text, "good"
        if value is False:
            return false_text, "bad"
        return None

    def add_bool(label: str, value: Any, true_text: str, false_text: str) -> None:
        result = bool_result(value, true_text, false_text)
        if result:
            add(label, result[0], tone=result[1])

    def add_llm_error(prefix: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        add(f"{prefix}异常类型", value.get("category"), tone="bad")
        add(f"{prefix}HTTP 状态", value.get("status_code"), tone="bad")
        add(f"{prefix}模型", value.get("model"))
        elapsed_ms = value.get("elapsed_ms")
        add(f"{prefix}请求耗时", f"{elapsed_ms} ms" if elapsed_ms is not None else None)
        add(f"{prefix}调用次数", value.get("attempts"))
        add(f"{prefix}request_id", value.get("request_id"))
        add(f"{prefix}错误信息", value.get("message"), tone="bad")

    def text_list(value: Any, *, key: str | None = None) -> str:
        if not isinstance(value, (list, tuple)):
            return ""
        parts: list[str] = []
        for entry in value:
            selected = entry.get(key) if key and isinstance(entry, dict) else entry
            text = str(selected or "").strip()
            if text:
                parts.append(text)
        return "；".join(parts)

    def rule_list(value: Any) -> str:
        labels = {
            "standalone_call_to_action": "独立引流段落",
            "standalone_link_prompt": "独立链接提示",
            "standalone_cooperation_contact": "独立合作联系方式",
            "standalone_media_call_to_action": "独立音视频引流段落",
            "standalone_video_teaser": "独立视频引流行",
            "ai_targeted_repair": "AI 定向局部优化",
            "standalone_branded_podcast_prompt": "独立品牌播客引流段落",
            "standalone_branded_watch_prompt": "独立品牌观看引流段落",
            "photo_credit_marker": "图片署名格式",
            "photographer_credit_marker": "摄影署名格式",
        }
        if not isinstance(value, (list, tuple)):
            return ""
        return "；".join(
            labels.get(str(entry or ""), str(entry or ""))
            for entry in value
            if str(entry or "").strip()
        )

    def attribution_changes(value: Any) -> str:
        if not isinstance(value, (list, tuple)):
            return ""
        changes: list[str] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            before = str(entry.get("before") or "").strip()
            after = str(entry.get("after") or "").strip()
            if before and after:
                changes.append(f"{before} → {after}")
        return "；".join(changes)

    if event_type == "QUALITY_RESULT":
        round_number = payload.get("quality_round")
        if round_number in (1, 2):
            add("质检轮次", "首轮" if round_number == 1 else "二次")
        add_bool("质检结论", payload.get("pass"), "通过", "未通过")
        needs_review = payload.get("needs_review")
        if needs_review is True:
            add("人工审核", "需要", tone="bad")
        elif needs_review is False:
            add("人工审核", "不需要", tone="good")
        add("质检等级", payload.get("level"))
        add("质检得分", payload.get("score"))
        add("结果说明", payload.get("reason"))
        add_llm_error("AI ", payload.get("semantic_error"))
        add_llm_error("标题 AI ", payload.get("title_error"))

    elif event_type == "TITLE_FIXED":
        add("修正前", payload.get("before"))
        add("修正后", payload.get("after"))
        add("修正方式", payload.get("method") or payload.get("title_fix_method"))

    elif event_type == "LINKS_REMOVED":
        # Older events only carry ``source``.  Render optional counters when
        # a newer producer supplies them, without inventing a count.
        add("清理数量", payload.get("removed_count") or payload.get("count"))
        before_count = payload.get("before_count")
        after_count = payload.get("after_count")
        if before_count is not None and after_count is not None:
            add("可跳转内容数量", f"{before_count} → {after_count}")
        add("清理来源", payload.get("source"))

    elif event_type == "AUTO_REPAIR_TRIGGERED":
        first_quality = payload.get("first_quality")
        if not isinstance(first_quality, dict):
            first_quality = {}
        add_bool("首轮质检", first_quality.get("pass"), "通过", "未通过")
        add("触发原因", payload.get("trigger_reason"))
        add("命中数量", payload.get("match_count"))
        add("命中规则", rule_list(payload.get("rules")))
        add("命中内容", text_list(payload.get("matched_texts")))
        add("首轮说明", first_quality.get("reason"))

    elif event_type == "AUTO_REPAIR_APPLIED":
        removed_blocks = payload.get("removed_blocks")
        removed_count = payload.get("removed_count")
        if removed_count is None and payload.get("attribution_normalized_count") is None:
            # Historical events used match_count exclusively for deletions.
            removed_count = payload.get("match_count")
        if removed_count is None and isinstance(removed_blocks, list):
            removed_count = len(removed_blocks)
        if removed_count:
            add("删除内容数", removed_count)
            add("删除的推广内容", text_list(removed_blocks, key="text"))

        normalized_count = payload.get("attribution_normalized_count")
        if normalized_count:
            add("图片署名", f"已规范化 {normalized_count} 处（未删除图片说明）")
            add("规范化内容", attribution_changes(payload.get("attribution_changes")))

        body_before = payload.get("body_length_before")
        body_after = payload.get("body_length_after")
        if body_before is not None and body_after is not None:
            add("正文长度", f"{body_before} → {body_after}")
        image_before = payload.get("image_count_before")
        image_after = payload.get("image_count_after")
        if image_before is not None and image_after is not None:
            add("图片数量", f"{image_before} → {image_after}")
        add_bool("图片地址", payload.get("image_sources_unchanged"), "未改变", "发生变化")
        add_bool("文章标题", payload.get("title_unchanged"), "未改变", "发生变化")

    elif event_type == "AUTO_REPAIR_FINISHED":
        outcome = payload.get("outcome")
        outcome_labels = {
            "passed": ("优化后质检通过", "good"),
            "failed": ("优化后仍未通过", "bad"),
            "error": ("优化过程异常", "bad"),
        }
        if outcome in outcome_labels:
            add("优化结果", outcome_labels[outcome][0], tone=outcome_labels[outcome][1])
        final_status = payload.get("final_status")
        final_status_key = str(final_status or "").upper()
        add("最终状态", STATUS_LABELS.get(final_status_key, final_status))
        add("最终去向", payload.get("destination"))
        second_quality = payload.get("second_quality")
        if isinstance(second_quality, dict):
            add_bool("二次质检", second_quality.get("pass"), "通过", "未通过")
            add("二次说明", second_quality.get("reason"))
        add("异常原因", payload.get("error"), tone="bad")

    return rows


def _settings_view(app: Flask) -> dict:
    cfg: AppConfig = app.extensions["app_config"]
    scheduler: Scheduler = app.extensions["scheduler"]
    conn = get_db()
    enabled_sources = len(repo.list_sources(conn, include_disabled=False))
    token_service_url = os.getenv("TOKEN_SERVICE_URL", "").rstrip("/")
    if token_service_url:
        # 统一授权模式：从 token-service 读取真实授权状态和回调地址
        try:
            resp = _requests.get(f"{token_service_url}/auth/status", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            ts_status = data.get("auth_status", "UNKNOWN")
            open_auth = {
                "configured": True,
                "auth_status": ts_status,
                "auth_status_label": AUTH_STATUS_LABELS.get(ts_status, ts_status),
                "has_access_token": data.get("has_access_token", False),
                "has_refresh_token": data.get("has_refresh_token", False),
                "pending_state": "",
                "pending_state_expires_at": None,
                "token_expires_at": data.get("token_expires_at"),
                "refresh_token_expires_at": data.get("refresh_token_expires_at"),
                "expires_in_seconds": data.get("expires_in_seconds"),
                "refresh_expires_in_seconds": data.get("refresh_expires_in_seconds"),
                "authorized_user": data.get("authorized_user") or {},
                "last_error": data.get("last_error") or "",
                "last_authorize_url": data.get("last_authorize_url") or "",
            }
            open_redirect_uri = f"{token_service_url}/auth/callback"
        except Exception:
            open_auth = auth_record_summary(db.get_open_platform_auth(cfg.database_path))
            open_redirect_uri = cfg.dqd_open_redirect_uri
    else:
        open_auth = auth_record_summary(db.get_open_platform_auth(cfg.database_path))
        open_redirect_uri = cfg.dqd_open_redirect_uri
    return {
        "scheduler_enabled": bool(scheduler.enabled),
        "interval_minutes": max(1, cfg.scheduler_interval_seconds // 60),
        "last_run_at": repo.get_setting("last_run_at", "", conn),
        "next_run_at": scheduler.next_run_at or "",
        "last_error": repo.get_setting("last_error", "", conn),
        "enabled_sources": enabled_sources,
        "material_configured": cfg.material_configured,
        "llm_configured": cfg.llm_configured,
        "dqd_configured": cfg.dqd_configured,
        "dqd_open_configured": cfg.dqd_open_configured,
        "dqd_open_idempotency_enabled": cfg.dqd_open_idempotency_enabled,
        "dqd_open_redirect_uri": open_redirect_uri,
        "open_platform_auth": open_auth,
        "publisher_enabled": cfg.publisher_enabled,
        "publish_account_pool_enabled": bool(
            repo.get_setting("publish_account_pool_enabled", False, conn)
        ),
    }


def _run_views(conn: Any) -> list[dict]:
    result = []
    for row in repo.list_run_logs(conn, limit=12):
        item = dict(row)
        try:
            details = json.loads(item.get("details_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        item["fetched"] = item.get("fetched_count", details.get("fetched", 0))
        item["created"] = item.get("inserted_count", details.get("inserted", 0))
        item["duplicates"] = item.get("updated_count", details.get("updated", 0))
        publish = details.get("publish") if isinstance(details, dict) else {}
        item["drafts"] = int((publish or {}).get("draft_created", 0)) if isinstance(publish, dict) else 0
        item["published"] = int((publish or {}).get("published", 0)) if isinstance(publish, dict) else 0
        failed_items = details.get("failed_items") if isinstance(details, dict) else []
        item["message"] = item.get("error") or details.get("message") or "任务执行完成"
        if failed_items:
            first = failed_items[0] if isinstance(failed_items[0], dict) else {}
            item["message"] = f"{len(failed_items)} 条素材失败；首条 {first.get('source', '未知来源')}：{first.get('error', '处理失败')}"
        item["status"] = str(item.get("status", "")).lower()
        item["status_label"] = {"success": "完成", "partial": "部分完成", "error": "失败", "running": "运行中"}.get(item["status"], item["status"])
        result.append(item)
    return result


def _dashboard_status_groups(stats: dict[str, int]) -> list[dict]:
    definitions = [
        ("RECEIVED", "已获取", "等待进入质检"),
        ("QUALITY_CHECKING", "质检中", "正在检查标题和正文"),
        ("NEEDS_REVIEW", "待人工审核", "需要人工确认或修正"),
        ("READY_TO_PUBLISH", "待发队列", "质检通过，等待发布适配"),
        ("PUBLISHING", "提交懂球帝中", "正在调用开放平台"),
        ("DRAFT_CONFIRMING", "提交结果确认中", "502 会延迟重试一次，其他情况停止自动重发"),
        ("DRAFT_CREATED", "草稿已创建", "开放平台已返回 archive_id"),
        ("PUBLISHED", "已直接发布", "开放平台已返回 archive_id"),
        ("PUBLISH_FAILED", "提交懂球帝失败", "开放平台调用失败"),
        ("ALREADY_PUBLISHED", "已存在后台文章", "接口记录已有文章 ID"),
        ("SOURCE_DUPLICATE", "来源重复（已拦截）", "相同来源文章 ID 已存在"),
        ("REJECTED", "已驳回", "不会进入发布队列"),
        ("ERROR", "处理失败", "可查看详情后重试"),
    ]
    return [
        {"status": _status_key(code), "label": label, "description": desc, "count": stats.get(code, 0)}
        for code, label, desc in definitions
    ]


def create_app(test_config: dict | None = None) -> Flask:
    ensure_instance_dir()
    cfg = AppConfig()
    if test_config and test_config.get("DATABASE"):
        cfg = replace(cfg, database_path=str(test_config["DATABASE"]))
    _secret_key = os.getenv("AUTOMATIC_POST_SECRET")
    if not _secret_key:
        if test_config is not None:
            _secret_key = "test-only-secret-not-for-production"
        else:
            raise RuntimeError(
                "环境变量 AUTOMATIC_POST_SECRET 未设置。"
                "请在 .env 文件中配置：AUTOMATIC_POST_SECRET=<随机字符串>"
            )
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        static_folder=str(Path(__file__).resolve().parents[1] / "static"),
    )
    app.config.from_mapping(
        SECRET_KEY=_secret_key,
        DATABASE=cfg.database_path,
        TESTING=bool(test_config and test_config.get("TESTING")),
    )
    if test_config:
        app.config.update(test_config)
    app.extensions["app_config"] = cfg
    init_db_app(app)
    with app.app_context():
        init_db()
        seed_catalog()

    controller = RunController(cfg)
    confirmation_controller = DraftConfirmationController(cfg)
    scheduler = Scheduler(
        controller,
        cfg.scheduler_interval_seconds,
        maintenance_controller=confirmation_controller,
        maintenance_interval_seconds=min(15, cfg.dqd_open_502_retry_delay_seconds),
    )
    app.extensions["run_controller"] = controller
    app.extensions["draft_confirmation_controller"] = confirmation_controller
    app.extensions["scheduler"] = scheduler
    with app.app_context():
        scheduler_enabled = bool(repo.get_setting("scheduler_enabled", cfg.scheduler_enabled))
    if scheduler_enabled and not app.config.get("TESTING") and not test_config:
        scheduler.start()

    app.jinja_env.globals["cdn_url"] = cdn_url
    app.jinja_env.globals["status_label"] = lambda value: STATUS_LABELS.get(value, value)
    app.jinja_env.filters["beijing_time"] = format_beijing_time

    def _safe_back_url(fallback: str) -> str:
        """Return request.referrer only when it is same-origin; fall back otherwise."""
        ref = request.referrer
        if not ref:
            return fallback
        try:
            parsed = urlparse(ref)
            if parsed.scheme in ("http", "https") and parsed.netloc == request.host:
                return ref
        except Exception:
            pass
        return fallback

    app.jinja_env.globals["safe_back_url"] = _safe_back_url

    @app.context_processor
    def inject_navigation_context():
        try:
            conn = get_db()
            review_count = repo.count_articles(conn, status="NEEDS_REVIEW")
            settings = _settings_view(app)
        except Exception:
            app.logger.exception("导航上下文加载失败")
            review_count, settings = 0, {"scheduler_enabled": False}
        return {"review_count": review_count, "settings": settings}

    @app.get("/")
    def dashboard():
        conn = get_db()
        stats = repo.article_counts(conn)
        recent = [_article_view(row) for row in repo.list_articles(conn, limit=12)]
        runs = _run_views(conn)
        return render_template(
            "dashboard.html",
            stats={key.lower(): value for key, value in stats.items()},
            status_groups=_dashboard_status_groups(stats),
            recent=recent,
            runs=runs,
        )

    @app.get("/articles")
    def articles():
        conn = get_db()
        status = _status_value(request.args.get("status"))
        tab_arg = request.args.get("tab")
        try:
            tab_id = int(tab_arg) if tab_arg else None
        except ValueError:
            tab_id = None
        rows = repo.list_articles(
            conn,
            status=status,
            source=request.args.get("source") or None,
            tab_id=tab_id,
            query=request.args.get("q") or None,
            limit=200,
        )
        return render_template(
            "articles.html",
            articles=[_article_view(row) for row in rows],
            tabs=[_tab_view(row) for row in repo.list_tabs(conn)],
            sources=[_source_view(row) for row in repo.list_sources(conn)],
            status_labels={_status_key(key): value for key, value in STATUS_LABELS.items()},
            filters={"q": request.args.get("q", ""), "status": _status_key(status),
                     "source": request.args.get("source", ""), "tab": tab_arg or ""},
        )

    @app.get("/articles/<int:article_id>")
    def article_detail(article_id: int):
        conn = get_db()
        article = _article_view(repo.get_article(article_id, conn))
        if article is None:
            abort(404)
        requested_view = request.args.get("view", "quality").strip().lower()
        active_view = requested_view if requested_view in {"quality", "preview"} else "quality"
        preview_html = ""
        if active_view == "preview":
            publish_body = article.get("body_html", "")
            try:
                publish_body = build_publish_body(
                    publish_body,
                    article.get("publish_cover_path"),
                )
            except ArticleImageError:
                # The publisher will report the blocking image error. The
                # preview still renders the stored body for diagnosis.
                pass
            preview_html = sanitize_preview_html(publish_body)
        events = [_event_view(row) for row in repo.list_article_events(article_id, conn)]
        settings = _settings_view(app)
        return render_template(
            "article_detail.html",
            article=article,
            tab=article.get("tab", {}),
            tabs=article.get("tabs", []),
            channels=article.get("channels", []),
            quality=article.get("quality", {}),
            status_label=article.get("status_label"),
            events=events,
            settings=settings,
            active_view=active_view,
            preview_html=preview_html,
            can_create_draft=(
                article.get("status_key") in {"ready_to_publish", "publish_failed", "mapping_blocked"}
                and not article.get("publish_mode_conflict")
                and settings["publisher_enabled"]
                and settings["dqd_open_configured"]
            ),
        )

    @app.get("/review")
    def review():
        conn = get_db()
        article_id = request.args.get("article_id")
        rows = repo.list_articles(conn, status="NEEDS_REVIEW", limit=200)
        if article_id:
            try:
                selected = repo.get_article(int(article_id), conn)
                if selected and selected not in rows:
                    rows.insert(0, selected)
            except ValueError:
                pass
        return render_template("review.html", articles=[_article_view(row) for row in rows])

    @app.get("/config", endpoint="config")
    def config_page():
        conn = get_db()
        return render_template(
            "config.html",
            tabs=[_tab_view(row) for row in repo.list_tabs(conn)],
            sources=[_source_view(row) for row in repo.list_sources(conn)],
            event_tab_rules=repo.list_event_tab_rules(conn),
            publish_accounts=repo.list_publish_accounts(conn),
            settings=_settings_view(app),
        )

    @app.post("/api/run")
    def api_run():
        controller: RunController = app.extensions["run_controller"]
        if not controller.start():
            return jsonify({"success": False, "error": "已有任务正在运行"}), 409
        return jsonify({"success": True, "message": "拉取任务已启动", "status": controller.status()})

    @app.get("/api/run/status")
    def api_run_status():
        return jsonify({"success": True, **app.extensions["run_controller"].status()})

    @app.get("/api/open/auth/status")
    def api_open_auth_status():
        cfg: AppConfig = app.extensions["app_config"]
        token_service_url = os.getenv("TOKEN_SERVICE_URL", "").rstrip("/")
        if token_service_url:
            # 统一授权模式：从 token-service 读取真实授权状态
            try:
                resp = _requests.get(f"{token_service_url}/auth/status", timeout=5)
                resp.raise_for_status()
                data = resp.json()
                ts_status = data.get("auth_status", "UNKNOWN")
                return jsonify({
                    "success": True,
                    "configured": bool(cfg.dqd_open_appid and cfg.dqd_open_appsecret),
                    "redirect_uri": f"{token_service_url}/auth/callback",
                    "auth": {
                        "configured": True,
                        "auth_status": ts_status,
                        "auth_status_label": AUTH_STATUS_LABELS.get(ts_status, ts_status),
                        "has_access_token": data.get("has_access_token", False),
                        "has_refresh_token": data.get("has_refresh_token", False),
                        "pending_state": "",
                        "pending_state_expires_at": None,
                        "token_expires_at": data.get("token_expires_at"),
                        "refresh_token_expires_at": data.get("refresh_token_expires_at"),
                        "expires_in_seconds": data.get("expires_in_seconds"),
                        "refresh_expires_in_seconds": data.get("refresh_expires_in_seconds"),
                        "authorized_user": data.get("authorized_user") or {},
                        "last_error": data.get("last_error") or "",
                        "last_authorize_url": data.get("last_authorize_url") or "",
                    },
                })
            except Exception as exc:
                return jsonify({"success": False, "error": f"token-service 不可达: {exc}"}), 502
        return jsonify({
            "success": True,
            "configured": bool(cfg.dqd_open_appid and cfg.dqd_open_appsecret),
            "redirect_uri": cfg.dqd_open_redirect_uri,
            "auth": auth_record_summary(db.get_open_platform_auth(cfg.database_path)),
        })

    @app.post("/api/open/auth/start")
    def api_open_auth_start():
        cfg: AppConfig = app.extensions["app_config"]
        token_service_url = os.getenv("TOKEN_SERVICE_URL", "").rstrip("/")
        if token_service_url:
            # 统一授权模式：将授权发起委托给 token-service，
            # 使 redirect_uri 指向 :9000/auth/callback，token 统一存在 token-service 的 DB。
            try:
                resp = _requests.post(f"{token_service_url}/auth/start", timeout=10)
                resp.raise_for_status()
                data = resp.json()
                return jsonify({"success": True, "authorize_url": data["authorize_url"]})
            except Exception as exc:
                return jsonify({"success": False, "error": f"token-service 不可达: {exc}"}), 502
        # 未配置 TOKEN_SERVICE_URL 时降级到本地授权流程
        try:
            result = OpenPlatformClient(cfg).start_authorization()
            return jsonify({"success": True, **result})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/api/open/auth/callback")
    def api_open_auth_callback():
        cfg: AppConfig = app.extensions["app_config"]
        error = request.args.get("error", "").strip()
        error_description = request.args.get("error_description", "").strip()
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        client = OpenPlatformClient(cfg)
        if error:
            db.update_open_platform_auth(
                cfg.database_path,
                auth_status="ERROR",
                last_error=f"{error}: {error_description}" if error_description else error,
            )
            return (
                f"<html><body><h1>授权失败</h1><p>{html.escape(error)}</p><p>{html.escape(error_description)}</p><p>你可以关闭这个页面后重试。</p></body></html>",
                400,
            )
        if not code:
            return ("<html><body><h1>缺少授权 code</h1><p>请从开放平台重新发起授权。</p></body></html>", 400)
        try:
            client.handle_callback(code, state or None)
            return "<html><body><h1>授权成功</h1><p>可以关闭这个页面，返回工作台继续创建草稿。</p></body></html>"
        except Exception as exc:
            return (f"<html><body><h1>授权失败</h1><pre>{html.escape(str(exc))}</pre></body></html>", 400)

    @app.post("/api/open/auth/refresh")
    def api_open_auth_refresh():
        cfg: AppConfig = app.extensions["app_config"]
        try:
            token = OpenPlatformClient(cfg).refresh_access_token()
            return jsonify({"success": True, "has_token": bool(token)})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/open/auth/reset")
    def api_open_auth_reset():
        cfg: AppConfig = app.extensions["app_config"]
        item = db.reset_open_platform_auth(cfg.database_path)
        return jsonify({"success": True, "item": item})

    @app.post("/api/scheduler/toggle")
    def api_scheduler_toggle():
        enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
        scheduler: Scheduler = app.extensions["scheduler"]
        if enabled:
            scheduler.start()
        else:
            scheduler.stop()
        repo.set_setting("scheduler_enabled", enabled, get_db())
        return jsonify({"success": True, "enabled": enabled, "message": "自动任务已恢复" if enabled else "自动任务已暂停"})

    @app.post("/api/publish-accounts/toggle")
    def api_publish_account_pool_toggle():
        conn = get_db()
        try:
            payload = _json_object_payload()
            enabled = _strict_boolean(payload, "enabled")
            repo.set_publish_account_pool_enabled(enabled, conn)
            return jsonify({
                "success": True,
                "enabled": enabled,
                "message": "发布账号池已启用" if enabled else "发布账号池已停用",
            })
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/publish-accounts")
    def api_create_publish_account():
        conn = get_db()
        try:
            fields = _publish_account_fields(_json_object_payload(), creating=True)
            account = repo.create_publish_account(
                fields["dqd_user_id"],
                fields["user_name"],
                fields["enabled"],
                connection=conn,
            )
            return jsonify({
                "success": True,
                "message": "发布账号已新增",
                "account": account,
            })
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/publish-accounts/<int:account_id>")
    def api_update_publish_account(account_id: int):
        conn = get_db()
        try:
            fields = _publish_account_fields(_json_object_payload(), creating=False)
            account = repo.update_publish_account(account_id, conn, **fields)
            return jsonify({
                "success": True,
                "message": "发布账号已更新",
                "account": account,
            })
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/api/articles")
    def api_articles():
        conn = get_db()
        status = _status_value(request.args.get("status"))
        rows = repo.list_articles(conn, status=status, source=request.args.get("source"), limit=200)
        return jsonify({"success": True, "items": [_article_view(row) for row in rows]})

    @app.get("/api/articles/<int:article_id>")
    def api_article(article_id: int):
        conn = get_db()
        article = _article_view(repo.get_article(article_id, conn))
        if article is None:
            return jsonify({"success": False, "error": "文章不存在"}), 404
        article["events"] = [_event_view(row) for row in repo.list_article_events(article_id, conn)]
        return jsonify({"success": True, "article": article})

    @app.post("/api/articles/<int:article_id>/review")
    def api_review(article_id: int):
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        if action == "fix_pass":
            action = "manual_fix_then_pass"
        try:
            result = repo.manual_review_update(
                article_id,
                action,
                get_db(),
                title=payload.get("title"),
                body_html=payload.get("body", payload.get("body_html")),
                litpic=payload.get("litpic"),
                channels=payload.get("channels"),
                tab_id=payload.get("tab_id"),
                note=str(payload.get("reason") or payload.get("note") or ""),
            )
            return jsonify({"success": True, "message": "审核操作已保存", "article": _article_view(result)})
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/articles/<int:article_id>/recheck-quality")
    def api_recheck_quality(article_id: int):
        cfg: AppConfig = app.extensions["app_config"]
        conn = get_db()
        try:
            final_status = recheck_article(article_id, cfg, conn)
            article = _article_view(repo.get_article(article_id, conn))
            message = (
                "重新优化并质检通过，文章已进入待发队列"
                if final_status in {"READY_TO_PUBLISH", "ALREADY_PUBLISHED"}
                else "重新优化与质检已完成，文章仍需人工审核"
                if final_status == "NEEDS_REVIEW"
                else "重新质检执行完成"
            )
            return jsonify({
                "success": True,
                "message": message,
                "status": final_status,
                "article": article,
            })
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 409

    @app.post("/api/articles/<int:article_id>/create-draft")
    def api_create_draft(article_id: int):
        cfg: AppConfig = app.extensions["app_config"]
        conn = get_db()
        current = _article_view(repo.get_article(article_id, conn))
        if current is not None and current.get("draft_confirming"):
            message = "草稿结果正在自动处理中；如已安排任务，系统会按计划执行，请勿手动重复创建。"
            return jsonify({
                "success": False,
                "error": message,
                "message": message,
                "result": {"skipped": True, "reason": "draft_confirming"},
                "article": current,
            }), 409
        try:
            result = create_draft_for_article(cfg, conn, article_id)
            article = _article_view(repo.get_article(article_id, conn))
            if article is not None and article.get("draft_confirming"):
                message = (
                    "创建结果暂未确认，系统已安排自动处理，无需手动操作"
                    if article.get("draft_confirmation_scheduled")
                    else "创建结果暂未确认，当前不会继续重发，请根据 request_id 核对结果"
                )
            elif result.get("reused_existing_archive"):
                message = "检测到已存在懂球帝文章链接，已同步状态"
            elif result.get("published") or result.get("status") == "PUBLISHED":
                message = "文章已直接发布到懂球帝"
            else:
                message = "懂球帝草稿已创建"
            return jsonify({
                "success": True,
                "message": message,
                "result": result,
                "article": article,
            })
        except DraftClaimSkipped as exc:
            article = _article_view(repo.get_article(article_id, conn))
            return jsonify({
                "success": True,
                "message": str(exc),
                "result": {"skipped": True},
                "article": article,
            })
        except DqdOpenClientError as exc:
            article = _article_view(repo.get_article(article_id, conn))
            if exc.result_unknown and article is not None and article.get("draft_confirming"):
                message = (
                    "创建结果暂未确认，系统已安排自动处理，无需手动操作"
                    if article.get("draft_confirmation_scheduled")
                    else "创建结果暂未确认，当前不会继续重发，请根据 request_id 核对结果"
                )
                return jsonify({
                    "success": True,
                    "message": message,
                    "operation_status": "DRAFT_CONFIRMING",
                    "result": {
                        "result_unknown": True,
                        "request_id": exc.diagnostics.get("request_id"),
                        "client_request_id": article.get("client_request_id"),
                    },
                    "article": article,
                }), 202
            status_code = exc.status_code if exc.status_code in {401, 403, 409, 422, 502, 503, 504} else 400
            return jsonify({
                "success": False,
                "error": str(exc),
                "operation_status": article.get("status") if article else None,
                "result_unknown": exc.result_unknown,
            }), status_code
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/tabs")
    def api_create_tab():
        payload = request.get_json(silent=True) or {}
        try:
            backend_id = payload.get("backend_tab_id")
            if backend_id in (None, ""):
                raise ValueError("后台栏目 ID 必须填写，避免创建无法发布的栏目")
            publish_mode = payload.get("publish_mode", 0)
            if isinstance(publish_mode, bool) or not isinstance(publish_mode, int) or publish_mode not in {0, 1}:
                raise ValueError("publish_mode 必须是 0（草稿）或 1（直接发布）")
            tab = repo.create_tab(
                str(payload.get("name") or ""),
                int(backend_id),
                publish_mode=publish_mode,
                fallback_litpic=payload.get("fallback_litpic", ""),
            )
            return jsonify({"success": True, "message": "栏目已新增", "tab": tab})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/tabs/<int:tab_id>")
    def api_update_tab(tab_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            update_args = {
                "name": payload.get("name") if "name" in payload else None,
                "backend_tab_id": (
                    int(payload["backend_tab_id"])
                    if payload.get("backend_tab_id") not in (None, "")
                    else None
                ),
                "enabled": payload.get("enabled") if "enabled" in payload else None,
            }
            if "publish_mode" in payload:
                publish_mode = payload.get("publish_mode")
                if (
                    isinstance(publish_mode, bool)
                    or not isinstance(publish_mode, int)
                    or publish_mode not in {0, 1}
                ):
                    raise ValueError("publish_mode 必须是 0（草稿）或 1（直接发布）")
                update_args["publish_mode"] = publish_mode
            if "fallback_litpic" in payload:
                fallback_litpic = payload.get("fallback_litpic")
                if not isinstance(fallback_litpic, str):
                    raise ValueError("fallback_litpic 必须是字符串")
                update_args["fallback_litpic"] = fallback_litpic
            tab = repo.update_tab(tab_id, **update_args)
            affected_sources = [
                _source_view(source)
                for source in repo.list_sources(get_db(), tab_id=tab_id)
            ]
            return jsonify({
                "success": True,
                "message": "栏目已更新",
                "tab": tab,
                "affected_sources": affected_sources,
            })
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/sources")
    def api_create_source():
        try:
            payload = _json_object_payload()
            code = payload.get("code")
            display_name = payload.get("display_name")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("source code 不能为空")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError("来源名称不能为空")
            tab_ids = payload.get("tab_ids", [])
            if tab_ids is None:
                tab_ids = []
            if not isinstance(tab_ids, list):
                raise ValueError("tab_ids 必须是 JSON 数组")
            enabled = payload.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是 JSON boolean")
            publish_mode_override = payload.get("publish_mode_override")
            if (
                publish_mode_override is not None
                and (
                    isinstance(publish_mode_override, bool)
                    or not isinstance(publish_mode_override, int)
                    or publish_mode_override not in {0, 1}
                )
            ):
                raise ValueError("publish_mode_override 必须是 null、0（草稿）或 1（直接发布）")
            result = repo.create_source(
                code,
                display_name,
                tab_ids=tab_ids,
                enabled=enabled,
                publish_mode_override=publish_mode_override,
                connection=get_db(),
            )
            return jsonify({
                "success": True,
                "message": "来源已新增",
                "source": _source_view(result),
            })
        except sqlite3.IntegrityError as exc:
            if "sources.code" in str(exc):
                return jsonify({"success": False, "error": "source code 已存在"}), 400
            return jsonify({"success": False, "error": str(exc)}), 400
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/sources/<path:code>")
    def api_update_source(code: str):
        payload = request.get_json(silent=True) or {}
        try:
            tab_id = payload.get("tab_id")
            has_tab_field = "tab_id" in payload
            clear_tab = bool(payload.get("clear_tab")) if "clear_tab" in payload else (has_tab_field and tab_id in (None, ""))
            update_args = {
                "display_name": payload.get("display_name") if "display_name" in payload else None,
                "enabled": bool(payload.get("enabled")) if "enabled" in payload else None,
                "tab_id": int(tab_id) if tab_id not in (None, "") else None,
                "clear_tab": clear_tab,
            }
            if "tab_ids" in payload:
                update_args["tab_ids"] = payload.get("tab_ids")
            if "publish_mode" in payload:
                publish_mode = payload.get("publish_mode")
                if isinstance(publish_mode, bool) or not isinstance(publish_mode, int) or publish_mode not in {0, 1}:
                    raise ValueError("publish_mode 必须是 0（草稿）或 1（直接发布）")
                update_args["publish_mode"] = publish_mode
            if "publish_mode_override" in payload:
                publish_mode_override = payload.get("publish_mode_override")
                if (
                    publish_mode_override is not None
                    and (
                        isinstance(publish_mode_override, bool)
                        or not isinstance(publish_mode_override, int)
                        or publish_mode_override not in {0, 1}
                    )
                ):
                    raise ValueError("publish_mode_override 必须是 null、0（草稿）或 1（直接发布）")
                update_args["publish_mode_override"] = publish_mode_override
            result = repo.update_source(code, **update_args)
            return jsonify({"success": True, "message": "来源配置已更新", "source": _source_view(result)})
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/api/event-tab-rules")
    def api_list_event_tab_rules():
        pending_text = str(request.args.get("pending", "")).strip().lower()
        if pending_text not in {"", "0", "1", "false", "true"}:
            return jsonify({"success": False, "error": "pending 必须是 boolean"}), 400
        try:
            list_args: dict[str, Any] = {
                "marker_type": request.args.get("marker_type"),
                "pending_only": pending_text in {"1", "true"},
                "search": request.args.get("search"),
            }
            if "source_code" in request.args:
                list_args["source_code"] = request.args.get("source_code") or None
            rules = repo.list_event_tab_rules(
                get_db(),
                **list_args,
            )
            return jsonify({"success": True, "event_tab_rules": rules})
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/event-tab-rules")
    def api_create_event_tab_rule():
        try:
            payload = _json_object_payload()
            enabled = payload.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是 JSON boolean")
            source_code = payload.get("source_code")
            if source_code is not None and not isinstance(source_code, str):
                raise ValueError("source_code 必须是字符串或 null")
            source_code = str(source_code or "").strip() or None
            publish_mode_override = payload.get("publish_mode_override")
            if (
                publish_mode_override is not None
                and (
                    isinstance(publish_mode_override, bool)
                    or not isinstance(publish_mode_override, int)
                    or publish_mode_override not in {0, 1}
                )
            ):
                raise ValueError("publish_mode_override 必须是 null、0（草稿）或 1（直接发布）")
            rule = repo.create_event_tab_rule(
                payload.get("marker_type"),
                payload.get("marker_code"),
                tab_id=payload.get("tab_id"),
                enabled=enabled,
                source_code=source_code,
                publish_mode_override=publish_mode_override,
                connection=get_db(),
            )
            return jsonify({
                "success": True,
                "message": "赛事栏目规则已新增",
                "event_tab_rule": rule,
            })
        except sqlite3.IntegrityError as exc:
            if "event_tab_rules" in str(exc) and "UNIQUE constraint failed" in str(exc):
                return jsonify({"success": False, "error": "该适用范围内的同类型短码规则已存在"}), 400
            return jsonify({"success": False, "error": str(exc)}), 400
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/event-tab-rules/<int:rule_id>")
    def api_update_event_tab_rule(rule_id: int):
        try:
            payload = _json_object_payload()
            update_args: dict[str, Any] = {}
            for field in (
                "marker_type", "marker_code", "tab_id", "source_code",
                "publish_mode_override", "enabled",
            ):
                if field in payload:
                    update_args[field] = payload[field]
            if not update_args:
                raise ValueError("至少提供一个需要更新的字段")
            if "enabled" in update_args and not isinstance(update_args["enabled"], bool):
                raise ValueError("enabled 必须是 JSON boolean")
            if "source_code" in update_args:
                source_code = update_args["source_code"]
                if source_code is not None and not isinstance(source_code, str):
                    raise ValueError("source_code 必须是字符串或 null")
                update_args["source_code"] = str(source_code or "").strip() or None
            if "publish_mode_override" in update_args:
                publish_mode_override = update_args["publish_mode_override"]
                if (
                    publish_mode_override is not None
                    and (
                        isinstance(publish_mode_override, bool)
                        or not isinstance(publish_mode_override, int)
                        or publish_mode_override not in {0, 1}
                    )
                ):
                    raise ValueError("publish_mode_override 必须是 null、0（草稿）或 1（直接发布）")
            rule = repo.update_event_tab_rule(
                rule_id,
                get_db(),
                **update_args,
            )
            return jsonify({
                "success": True,
                "message": "赛事栏目规则已更新",
                "event_tab_rule": rule,
            })
        except sqlite3.IntegrityError as exc:
            if "event_tab_rules" in str(exc) and "UNIQUE constraint failed" in str(exc):
                return jsonify({"success": False, "error": "该适用范围内的同类型短码规则已存在"}), 400
            return jsonify({"success": False, "error": str(exc)}), 400
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/health")
    def health():
        cfg: AppConfig = app.extensions["app_config"]
        return jsonify({
            "ok": True,
            "material_api_configured": cfg.material_configured,
            "llm_configured": cfg.llm_configured,
            "dqd_session_configured": cfg.dqd_configured,
            "dqd_open_configured": cfg.dqd_open_configured,
            "dqd_open_idempotency_enabled": cfg.dqd_open_idempotency_enabled,
            "dqd_open_redirect_uri": cfg.dqd_open_redirect_uri,
            "publisher_enabled": cfg.publisher_enabled,
        })

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="没有找到这个页面或文章。"), 404

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.exception("未处理的页面错误", exc_info=error)
        return render_template("error.html", code=500, message="系统处理请求时遇到错误，请稍后重试。"), 500

    return app
