"""Ingestion, quality workflow, and optional draft creation orchestration."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect
from ..statuses import STATUS_LABELS
from .material_client import MaterialClient, MaterialClientError, normalize_item
from .publisher import publish_ready_articles
from .quality import LLMService, evaluate
from .link_sanitizer import remove_clickable_links
from .promotion_repair import (
    body_safety_stats,
    find_promotional_blocks,
    normalize_photo_credits,
    remove_promotional_blocks,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quality_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") in {"RECEIVED", "QUALITY_CHECKING", "ERROR"}


def _update_content(article_id: int, *, title: str | None = None,
                    body: str | None = None, connection) -> None:
    conn = connection
    current = repo.get_article(article_id, conn)
    if current is None:
        raise ValueError("article not found")
    with conn:
        conn.execute(
            "UPDATE articles SET title_final=?, body_html=?, updated_at=? WHERE id=?",
            (
                current.get("title_final", "") if title is None else str(title),
                current.get("body_html", "") if body is None else str(body),
                _utc_now(),
                article_id,
            ),
        )


def _make_llm(config: AppConfig) -> LLMService | None:
    if not config.llm_configured:
        return None
    return LLMService(
        config.llm_api_key,
        config.llm_base_url,
        config.llm_model,
        config.llm_timeout,
    )


def _audit_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal source offsets before persisting repair evidence."""

    audited: list[dict[str, Any]] = []
    for item in matches:
        entry = {
            "rule": str(item.get("rule") or ""),
            "tag": str(item.get("tag") or ""),
        }
        for key in ("text", "caption", "source", "before", "after"):
            value = str(item.get(key) or "")[:300]
            if value:
                entry[key] = value
        audited.append(entry)
    return audited


def _record_quality_result(
    article_id: int,
    quality: dict[str, Any],
    quality_round: int,
    connection,
    *,
    status: str,
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
    )


def _apply_quality_title(article_id: int, current: dict[str, Any], quality: dict[str, Any], connection) -> None:
    title_after = quality.get("title_after") or current.get("title_final", "")
    if title_after == current.get("title_final", ""):
        return
    _update_content(article_id, title=title_after, connection=connection)
    repo.add_article_event(
        article_id,
        "TITLE_FIXED",
        connection,
        message=f"标题已自动修正（{quality.get('title_fix_method', 'unknown')}）",
        payload={"before": current.get("title_final", ""), "after": title_after},
    )
    current["title_final"] = title_after


def _second_quality_error(first_quality: dict[str, Any], error: Exception) -> dict[str, Any]:
    issues = dict(first_quality.get("issues") or {})
    semantic = list(issues.get("semantic_problems") or [])
    semantic.append("正文自动优化后的二次完整质检异常，需要人工确认")
    issues["semantic_problems"] = semantic
    return {
        **first_quality,
        "pass": False,
        "needs_review": True,
        "score": 50,
        "issues": issues,
        "reason": "正文已自动优化，但二次完整质检异常，需要人工确认",
        "second_quality_error": str(error)[:300],
    }


def _process_article(article: dict[str, Any], config: AppConfig, connection) -> str:
    article_id = int(article["id"])
    if not _quality_eligible(article):
        return article.get("status", "")
    if not article.get("tabs") and not article.get("tab_id"):
        quality = {
            "pass": False,
            "needs_review": True,
            "score": 50,
            "level": "B",
            "issues": {"configuration": ["该来源尚未绑定栏目"]},
            "reason": "来源尚未绑定栏目",
            "weak_channel_check": True,
        }
        repo.save_quality(article_id, quality, connection, status="NEEDS_REVIEW")
        return "NEEDS_REVIEW"

    repo.transition_status(
        article_id,
        "QUALITY_CHECKING",
        connection,
        event_type="QUALITY_STARTED",
        message="开始标题、正文和脏内容检查",
    )
    current = repo.get_article(article_id, connection)
    try:
        cleaned_body = remove_clickable_links(current.get("body_html", ""))
        if cleaned_body != current.get("body_html", ""):
            _update_content(article_id, body=cleaned_body, connection=connection)
            repo.add_article_event(
                article_id,
                "LINKS_REMOVED",
                connection,
                message="质检前已移除正文中的超链接及链接文字",
                payload={"source": "quality_preprocess"},
            )
            current["body_html"] = cleaned_body
        previous_repair = current.get("quality", {}).get("promotion_repair", {})
        first_quality = evaluate(
            title=current.get("title_final", ""),
            body=current.get("body_html", ""),
            channels=current.get("channels", []),
            llm=_make_llm(config),
        )
        _record_quality_result(
            article_id, first_quality, 1, connection, status="QUALITY_CHECKING"
        )

        dirty_content = (first_quality.get("issues") or {}).get("dirty_content") or []
        first_failed = bool(first_quality.get("needs_review") or not first_quality.get("pass"))
        body_before = str(current.get("body_html") or "")
        if first_failed and dirty_content and not previous_repair.get("attempted"):
            promotion_candidates = find_promotional_blocks(body_before)
            _, attribution_candidates = normalize_photo_credits(body_before)
        else:
            promotion_candidates = []
            attribution_candidates = []
        candidates = [*promotion_candidates, *attribution_candidates]
        if not candidates:
            final_quality = dict(first_quality)
            if previous_repair.get("attempted"):
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
                    article_id, final_quality, connection, status="NEEDS_REVIEW"
                )
                return "NEEDS_REVIEW"

            _apply_quality_title(article_id, current, first_quality, connection)
            target = "NEEDS_REVIEW" if first_quality.get("needs_review") else "READY_TO_PUBLISH"
            repo.save_quality(article_id, final_quality, connection, status=target)
            if target == "READY_TO_PUBLISH" and int(current.get("upstream_archive_id") or 0) > 0:
                repo.transition_status(
                    article_id,
                    "ALREADY_PUBLISHED",
                    connection,
                    event_type="ALREADY_PUBLISHED_DETECTED",
                    message="素材接口已返回非零 archive_id，本阶段不重复发布",
                )
                return "ALREADY_PUBLISHED"
            return target

        audit_matches = _audit_matches(candidates)
        repair: dict[str, Any] = {
            "attempted": True,
            "applied": False,
            "outcome": "pending",
            "removed_count": 0,
            "attribution_normalized_count": 0,
            "matches": audit_matches,
            "first_quality": first_quality,
        }
        progress_quality = {**first_quality, "promotion_repair": repair}
        # Persist the one-attempt marker before mutating the body. If a worker
        # stops between steps, a later run cannot delete more content.
        repo.save_quality(article_id, progress_quality, connection, status="QUALITY_CHECKING")
        repo.add_article_event(
            article_id,
            "AUTO_REPAIR_TRIGGERED",
            connection,
            message="首轮质检命中可安全处理的固定格式，准备自动优化一次",
            payload={
                "quality_round": 1,
                "trigger_reason": "首轮完整质检未通过，且命中高置信固定格式",
                "match_count": len(audit_matches),
                "rules": list(dict.fromkeys(item["rule"] for item in audit_matches)),
                "matched_texts": [
                    item.get("text") or item.get("before") or ""
                    for item in audit_matches
                ],
                "first_quality": first_quality,
            },
            from_status="QUALITY_CHECKING",
            to_status="QUALITY_CHECKING",
        )

        body_normalized, normalized_attributions = normalize_photo_credits(body_before)
        body_after, removed = remove_promotional_blocks(body_normalized)
        before_stats = body_safety_stats(body_before)
        after_stats = body_safety_stats(body_after)
        images_unchanged = before_stats["image_sources"] == after_stats["image_sources"]
        safe_to_apply = bool(
            (removed or normalized_attributions)
            and body_after != body_before
            and after_stats["visible_text_length"] >= 30
            and before_stats["parse_ok"]
            and after_stats["parse_ok"]
            and before_stats["image_count"] == after_stats["image_count"]
            and images_unchanged
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
        })
        if not safe_to_apply:
            repair.update({"outcome": "failed", "safety_check_passed": False})
            final_quality = {**first_quality, "promotion_repair": repair}
            repo.save_quality(article_id, final_quality, connection, status="NEEDS_REVIEW")
            repo.add_article_event(
                article_id,
                "AUTO_REPAIR_FINISHED",
                connection,
                message="自动优化未通过安全校验，正文保持不变并转人工审核",
                payload={
                    "outcome": "failed",
                    "final_status": "NEEDS_REVIEW",
                    "destination": "人工审核（自动优化安全校验未通过）",
                    "error": "优化后正文过短、正文未变化或图片地址发生变化",
                },
                from_status="NEEDS_REVIEW",
                to_status="NEEDS_REVIEW",
            )
            return "NEEDS_REVIEW"

        _update_content(article_id, body=body_after, connection=connection)
        current["body_html"] = body_after
        repair.update({"applied": True, "safety_check_passed": True})
        repo.add_article_event(
            article_id,
            "AUTO_REPAIR_APPLIED",
            connection,
            message=(
                f"已规范化 {len(normalized_attributions)} 处图片署名，"
                f"删除 {len(removed)} 个高置信独立推广段落"
            ),
            payload={
                "removed_blocks": _audit_matches(removed),
                "removed_count": len(removed),
                "attribution_normalized_count": len(normalized_attributions),
                "attribution_changes": _audit_matches(normalized_attributions),
                "attribution_only": bool(normalized_attributions and not removed),
                "match_count": len(removed) + len(normalized_attributions),
                "body_length_before": before_stats["body_length"],
                "body_length_after": after_stats["body_length"],
                "image_count_before": before_stats["image_count"],
                "image_count_after": after_stats["image_count"],
                "image_sources_unchanged": images_unchanged,
                "title_unchanged": True,
            },
            from_status="QUALITY_CHECKING",
            to_status="QUALITY_CHECKING",
        )

        second_error: Exception | None = None
        try:
            second_quality = evaluate(
                title=current.get("title_final", ""),
                body=current.get("body_html", ""),
                channels=current.get("channels", []),
                llm=_make_llm(config),
            )
        except Exception as exc:  # uncertainty after mutation must fail closed
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
            title_problems.append("正文自动优化不修改标题，标题修复需人工确认")
            issues["title_problems"] = title_problems
            second_quality.update({
                "pass": False,
                "needs_review": True,
                "score": min(int(second_quality.get("score") or 50), 50),
                "issues": issues,
                "reason": "正文已自动优化，但标题修复需人工确认",
                "title_after": current.get("title_final", ""),
                "title_fix_method": "blocked_during_promotion_repair",
            })

        _record_quality_result(
            article_id, second_quality, 2, connection, status="QUALITY_CHECKING"
        )
        repair.update({
            "outcome": "error" if second_error else ("passed" if not second_quality.get("needs_review") else "failed"),
            "second_quality": second_quality,
        })
        if second_error is not None:
            repair["error"] = str(second_error)[:300]
        final_quality = {**second_quality, "promotion_repair": repair}
        target = "NEEDS_REVIEW" if second_error or second_quality.get("needs_review") else "READY_TO_PUBLISH"
        repo.save_quality(article_id, final_quality, connection, status=target)
        final_status = target
        if target == "READY_TO_PUBLISH" and int(current.get("upstream_archive_id") or 0) > 0:
            repo.transition_status(
                article_id,
                "ALREADY_PUBLISHED",
                connection,
                event_type="ALREADY_PUBLISHED_DETECTED",
                message="素材接口已返回非零 archive_id，本阶段不重复发布",
            )
            final_status = "ALREADY_PUBLISHED"
        finish_payload: dict[str, Any] = {
            "outcome": repair["outcome"],
            "final_status": final_status,
            "destination": "发布队列" if final_status == "READY_TO_PUBLISH" else (
                "不重复发布" if final_status == "ALREADY_PUBLISHED" else "人工审核"
            ),
        }
        if second_error is None:
            finish_payload["second_quality"] = second_quality
        else:
            finish_payload["error"] = str(second_error)[:300]
        repo.add_article_event(
            article_id,
            "AUTO_REPAIR_FINISHED",
            connection,
            message=(
                "二次完整质检异常，已转人工审核"
                if second_error is not None
                else (
                    "自动清理后二次质检通过"
                    if final_status in {"READY_TO_PUBLISH", "ALREADY_PUBLISHED"}
                    else "自动清理后二次质检未通过，已转人工审核"
                )
            ),
            payload=finish_payload,
            from_status=final_status,
            to_status=final_status,
        )
        return final_status
    except Exception as exc:  # noqa: BLE001 - one bad item must not stop a batch
        logger.exception("质检失败 article_id=%s", article_id)
        repo.transition_status(
            article_id,
            "ERROR",
            connection,
            event_type="QUALITY_ERROR",
            message=str(exc)[:300],
        )
        return "ERROR"


def run_once(config: AppConfig, *, database_path: str | None = None) -> dict[str, Any]:
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
        for raw in fetched_result.items:
            try:
                item = normalize_item(raw)
                saved = repo.upsert_material(item, conn)
                article = saved["article"]
                if saved["created"]:
                    inserted += 1
                else:
                    updated += 1
                state = _process_article(article, config, conn)
                status_counts[state] = status_counts.get(state, 0) + 1
                if state == "ERROR":
                    errors += 1
                    failed = repo.get_article(article["id"], conn) or article
                    failed_items.append({
                        "source": str(failed.get("source") or "未知来源"),
                        "source_url": str(failed.get("source_url") or ""),
                        "error": str(failed.get("error") or "质检失败")[:300],
                    })
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.exception("素材处理失败")
                failed_items.append({
                    "source": str(raw.get("source") or "未知来源") if isinstance(raw, dict) else "未知来源",
                    "source_url": str(raw.get("source_url") or "") if isinstance(raw, dict) else "",
                    "error": str(exc)[:300],
                })
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
