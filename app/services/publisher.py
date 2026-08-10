"""Bridge READY_TO_PUBLISH articles to the DQD open platform."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import repository as repo
from ..config import AppConfig
from .dqd_open_client import DqdOpenClient, DqdOpenClientError
from .open_platform import build_draft_url


logger = logging.getLogger(__name__)
PUBLISHING_STALE_SECONDS = 10 * 60


class DraftClaimSkipped(RuntimeError):
    """Raised when another request already claimed the draft creation slot."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publish_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") == "READY_TO_PUBLISH"


def _retry_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") in {"READY_TO_PUBLISH", "PUBLISH_FAILED"}


def _existing_draft_result(
    article: dict[str, Any],
    *,
    retry: bool,
) -> dict[str, Any]:
    archive_id = int(article.get("dqd_archive_id") or 0)
    return {
        "article_id": int(article["id"]),
        "source": article.get("source"),
        "archive_id": archive_id,
        "draft_url": build_draft_url(archive_id),
        "request_url": "",
        "request_fields": [],
        "retry": retry,
        "reused_existing_archive": True,
    }


def _current_tab(article: dict[str, Any]) -> dict[str, Any] | None:
    tab_id = article.get("tab_id")
    backend_tab_id = article.get("backend_tab_id")
    if tab_id in (None, "") or backend_tab_id in (None, ""):
        return None
    return {
        "backend_tab_id": backend_tab_id,
        "name": article.get("tab_name") or "",
    }


def _create_draft_attempt(
    config: AppConfig,
    connection,
    article_id: int,
    *,
    allowed_statuses: set[str],
    start_event_type: str,
    success_event_type: str,
    failure_event_type: str,
    blocked_event_type: str,
    start_message: str,
    success_message_prefix: str,
    retry: bool = False,
    block_if_missing_tab: bool = False,
) -> dict[str, Any]:
    current = repo.get_article(article_id, connection)
    if current is None:
        raise ValueError("article not found")
    if current.get("status") not in allowed_statuses:
        raise ValueError(f"当前状态为 {current.get('status_label') or current.get('status') or '未知'}，不能创建草稿")

    archive_id = int(current.get("dqd_archive_id") or 0)
    if archive_id > 0:
        if current.get("status") != "DRAFT_CREATED":
            recovered = repo.transition_status_if_current(
                article_id,
                "DRAFT_CREATED",
                connection,
                allowed_from=allowed_statuses,
                event_type="DRAFT_ALREADY_EXISTS",
                message="检测到已存在懂球帝草稿，直接恢复为草稿已创建",
                payload={
                    "archive_id": archive_id,
                    "draft_url": build_draft_url(archive_id),
                    "retry": retry,
                },
            )
            if recovered is None:
                refreshed = repo.get_article(article_id, connection)
                if refreshed and int(refreshed.get("dqd_archive_id") or 0) > 0:
                    return _existing_draft_result(refreshed, retry=retry)
                raise ValueError("文章状态已变化，请刷新后重试")
            current = recovered
        return _existing_draft_result(current, retry=retry)

    tab = _current_tab(current)
    if tab is None:
        if block_if_missing_tab:
            repo.transition_status(
                article_id,
                "MAPPING_BLOCKED",
                connection,
                event_type=blocked_event_type,
                message="来源尚未绑定后台栏目，无法创建草稿",
                payload={"retry": retry, "source": current.get("source")},
            )
        raise ValueError("来源尚未绑定后台栏目，无法创建草稿")

    claimed = repo.transition_status_if_current(
        article_id,
        "PUBLISHING",
        connection,
        allowed_from=allowed_statuses,
        event_type=start_event_type,
        message=start_message,
        payload={"retry": retry, "source": current.get("source")},
    )
    if claimed is None:
        refreshed = repo.get_article(article_id, connection)
        if refreshed is not None and int(refreshed.get("dqd_archive_id") or 0) > 0:
            return _existing_draft_result(refreshed, retry=retry)
        raise DraftClaimSkipped(
            f"当前状态为 {refreshed.get('status_label') if refreshed else current.get('status_label') or current.get('status') or '未知'}，已有草稿创建任务正在处理"
        )

    client = DqdOpenClient(config)
    try:
        draft = client.create_article(current, tab)
        repo.update_article_backend_refs(
            article_id,
            connection,
            dqd_archive_id=draft.archive_id,
        )
        repo.transition_status(
            article_id,
            "DRAFT_CREATED",
            connection,
            from_status="PUBLISHING",
            event_type=success_event_type,
            message=f"{success_message_prefix}，archive_id={draft.archive_id}",
            payload={
                "archive_id": draft.archive_id,
                "draft_url": build_draft_url(draft.archive_id),
                "request_url": draft.request_url,
                "request_fields": [key for key, _ in draft.form_fields],
                "diagnostics": draft.diagnostics,
                "retry": retry,
            },
        )
        return {
            "article_id": article_id,
            "source": current.get("source"),
            "archive_id": draft.archive_id,
            "draft_url": build_draft_url(draft.archive_id),
            "request_url": draft.request_url,
            "request_fields": [key for key, _ in draft.form_fields],
            "retry": retry,
        }
    except DqdOpenClientError as exc:
        payload = {"error": str(exc), "status_code": exc.status_code, "retry": retry}
        if getattr(exc, "payload", None) is not None:
            payload["response_payload"] = exc.payload
        if getattr(exc, "diagnostics", None):
            payload["diagnostics"] = exc.diagnostics
        repo.transition_status(
            article_id,
            "PUBLISH_FAILED",
            connection,
            from_status="PUBLISHING",
            event_type=failure_event_type,
            message=str(exc)[:300],
            payload=payload,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - isolate one bad article
        payload = {"error": str(exc), "retry": retry}
        repo.transition_status(
            article_id,
            "PUBLISH_FAILED",
            connection,
            from_status="PUBLISHING",
            event_type=failure_event_type,
            message=str(exc)[:300],
            payload=payload,
        )
        raise


def recover_stale_publishing_articles(
    connection,
    *,
    stale_after_seconds: int = PUBLISHING_STALE_SECONDS,
) -> dict[str, int]:
    conn = connection
    articles = repo.list_articles(conn, status="PUBLISHING", limit=1000)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(stale_after_seconds)))
    recovered = timed_out = 0
    for article in articles:
        updated_at = _parse_utc(article.get("updated_at"))
        if updated_at is not None and updated_at > cutoff:
            continue
        archive_id = int(article.get("dqd_archive_id") or 0)
        if archive_id > 0:
            updated = repo.transition_status_if_current(
                int(article["id"]),
                "DRAFT_CREATED",
                conn,
                allowed_from={"PUBLISHING"},
                current_updated_at=article.get("updated_at"),
                event_type="PUBLISHING_RECOVERED",
                message="检测到草稿已创建，自动恢复为草稿已创建",
                payload={"archive_id": archive_id, "draft_url": build_draft_url(archive_id)},
            )
            if updated is not None:
                recovered += 1
            continue
        updated = repo.transition_status_if_current(
            int(article["id"]),
            "PUBLISH_FAILED",
            conn,
            allowed_from={"PUBLISHING"},
            current_updated_at=article.get("updated_at"),
            event_type="PUBLISHING_TIMED_OUT",
            message="创建草稿超时，已自动恢复为失败状态，请重新尝试",
            payload={"reason": "publishing_timeout"},
        )
        if updated is not None:
            timed_out += 1
    return {"recovered": recovered, "timed_out": timed_out, "checked": len(articles)}


def publish_ready_articles(config: AppConfig, connection, *, limit: int = 200) -> dict[str, Any]:
    # 无论 publisher 是否启用，都先清理卡住的 PUBLISHING 文章，避免状态永久卡死
    recovery = recover_stale_publishing_articles(connection)
    if not config.publisher_enabled:
        return {
            "draft_created": 0,
            "failed": 0,
            "skipped": 0,
            "recovered": recovery["recovered"],
            "timed_out": recovery["timed_out"],
            "items": [],
            "message": "发布 worker 未启用，跳过创建草稿",
        }
    if not config.dqd_open_configured:
        return {
            "draft_created": 0,
            "failed": 0,
            "skipped": 0,
            "recovered": recovery["recovered"],
            "timed_out": recovery["timed_out"],
            "items": [],
            "message": "开放平台 appid/appsecret/enname 未配置，跳过创建草稿",
        }

    articles = repo.list_articles(connection, status="READY_TO_PUBLISH", limit=limit)
    result_items: list[dict[str, Any]] = []
    draft_created = failed = skipped = 0

    for article in articles:
        article_id = int(article["id"])
        current = repo.get_article(article_id, connection) or article
        if not _publish_eligible(current):
            skipped += 1
            continue
        tab_id = current.get("tab_id")
        if tab_id is None or current.get("backend_tab_id") in (None, ""):
            skipped += 1
            repo.transition_status(
                article_id,
                "MAPPING_BLOCKED",
                connection,
                event_type="DRAFT_CREATE_BLOCKED",
                message="来源尚未绑定后台栏目，无法创建草稿",
            )
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": "来源尚未绑定后台栏目",
            })
            continue
        try:
            result = _create_draft_attempt(
                config,
                connection,
                article_id,
                allowed_statuses={"READY_TO_PUBLISH"},
                start_event_type="DRAFT_CREATE_STARTED",
                success_event_type="DRAFT_CREATED",
                failure_event_type="DRAFT_CREATE_FAILED",
                blocked_event_type="DRAFT_CREATE_BLOCKED",
                start_message="开始调用开放平台创建草稿",
                success_message_prefix="开放平台草稿创建成功",
            )
            draft_created += 1
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "archive_id": result["archive_id"],
                "reused_existing_archive": result.get("reused_existing_archive", False),
            })
        except DraftClaimSkipped as exc:
            skipped += 1
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
                "skipped": True,
            })
        except DqdOpenClientError as exc:
            failed += 1
            logger.exception("创建草稿失败 article_id=%s", article_id)
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
                "status_code": exc.status_code,
            })
        except Exception as exc:  # noqa: BLE001 - isolate one bad article
            failed += 1
            logger.exception("创建草稿时出现未处理异常 article_id=%s", article_id)
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
            })

    message = "草稿创建完成"
    if recovery["recovered"] or recovery["timed_out"]:
        message += f"，自动恢复 {recovery['recovered']} 篇"
        if recovery["timed_out"]:
            message += f"，超时恢复 {recovery['timed_out']} 篇"
    if failed:
        message += f"，失败 {failed} 条"
    return {
        "draft_created": draft_created,
        "failed": failed,
        "skipped": skipped,
        "recovered": recovery["recovered"],
        "timed_out": recovery["timed_out"],
        "items": result_items,
        "message": message,
        "started_at": _utc_now(),
    }


def create_draft_for_article(config: AppConfig, connection, article_id: int) -> dict[str, Any]:
    if not config.publisher_enabled:
        raise ValueError("发布 worker 未启用，无法创建草稿")
    if not config.dqd_open_configured:
        raise ValueError("开放平台 appid/appsecret/enname 未配置，无法创建草稿")
    current = repo.get_article(article_id, connection)
    if current is None:
        raise ValueError("article not found")
    if not _retry_eligible(current):
        raise ValueError(f"当前状态为 {current.get('status_label') or current.get('status') or '未知'}，不能重新创建草稿")
    return _create_draft_attempt(
        config,
        connection,
        article_id,
        allowed_statuses={"READY_TO_PUBLISH", "PUBLISH_FAILED"},
        start_event_type="DRAFT_RETRY_STARTED",
        success_event_type="DRAFT_RETRY_SUCCEEDED",
        failure_event_type="DRAFT_RETRY_FAILED",
        blocked_event_type="DRAFT_RETRY_BLOCKED",
        start_message="开始重新创建懂球帝草稿",
        success_message_prefix="重新创建懂球帝草稿成功",
        retry=True,
        block_if_missing_tab=True,
    )
