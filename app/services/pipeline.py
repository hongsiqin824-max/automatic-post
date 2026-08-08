"""Ingestion and pre-publish quality workflow.

This module intentionally stops at ``READY_TO_PUBLISH``.  The future DQD
publisher can consume that queue without changing the local article model.
"""

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
from .quality import LLMService, evaluate

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quality_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") in {"RECEIVED", "QUALITY_CHECKING", "ERROR"}


def _update_content(article_id: int, *, title: str | None = None,
                    body: str | None = None, connection=None) -> None:
    conn = connection
    if conn is None:
        from ..db import get_db

        conn = get_db()
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


def _process_article(article: dict[str, Any], config: AppConfig, connection) -> str:
    article_id = int(article["id"])
    if not _quality_eligible(article):
        return article.get("status", "")
    if not article.get("tab_id"):
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
        quality = evaluate(
            title=current.get("title_final", ""),
            body=current.get("body_html", ""),
            channels=current.get("channels", []),
            llm=_make_llm(config),
        )
        title_after = quality.get("title_after") or current.get("title_final", "")
        if title_after != current.get("title_final", ""):
            _update_content(article_id, title=title_after, connection=connection)
            repo.add_article_event(
                article_id,
                "TITLE_FIXED",
                connection,
                message=f"标题已自动修正（{quality.get('title_fix_method', 'unknown')}）",
                payload={"before": current.get("title_final", ""), "after": title_after},
            )
        target = "NEEDS_REVIEW" if quality.get("needs_review") else "READY_TO_PUBLISH"
        repo.save_quality(article_id, quality, connection, status=target)
        repo.add_article_event(
            article_id,
            "QUALITY_RESULT",
            connection,
            message=quality.get("reason", "质检完成"),
            payload=quality,
            from_status=target,
            to_status=target,
        )
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
    run_id = repo.start_run_log("INGEST", conn, details={"started_at": _utc_now()})
    fetched = inserted = updated = errors = 0
    status_counts: dict[str, int] = {}
    failed_items: list[dict[str, str]] = []
    try:
        enabled_sources = repo.list_sources(conn, include_disabled=False)
        source_codes = [item["code"] for item in enabled_sources]
        if not source_codes:
            result = {"run_id": run_id, "fetched": 0, "inserted": 0, "updated": 0,
                      "message": "没有启用的 source"}
            repo.finish_run_log(run_id, conn, details=result)
            repo.set_setting("last_run_at", _utc_now(), conn)
            repo.set_setting("last_error", "", conn)
            return result
        if not config.material_configured:
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
        result = {
            "run_id": run_id,
            "fetched": fetched,
            "inserted": inserted,
            "updated": updated,
            "errors": errors,
            "status_counts": status_counts,
            "failed_items": failed_items,
            "message": "本轮完成",
        }
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
            details={"message": str(exc)},
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
