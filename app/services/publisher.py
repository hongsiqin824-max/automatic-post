"""Bridge READY_TO_PUBLISH articles to the DQD open platform."""

from __future__ import annotations

import logging
import inspect
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import repository as repo
from ..config import AppConfig
from ..db import _connect
from .dqd_open_client import DqdOpenClient, DqdOpenClientError
from .open_platform import build_draft_url
from .quality import analyze_body_language


logger = logging.getLogger(__name__)
PUBLISHING_STALE_SECONDS = 10 * 60
CONFIRMATION_DELAYS_SECONDS = (15, 60, 180, 600, 1800)


class DraftClaimSkipped(RuntimeError):
    """Raised when another request already claimed the draft creation slot."""


class PublishModeConflict(ValueError):
    """Raised when one article maps to tabs with different publish modes."""


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


def _confirmation_at(attempt: int) -> str | None:
    """Return the next automatic check time, or None after the retry budget."""

    index = max(0, int(attempt))
    if index >= len(CONFIRMATION_DELAYS_SECONDS):
        return None
    return (
        datetime.now(timezone.utc)
        + timedelta(seconds=CONFIRMATION_DELAYS_SECONDS[index])
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _retry_at(delay_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(1, int(delay_seconds)))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _initial_confirmation_schedule(
    config: AppConfig,
    error: DqdOpenClientError,
) -> tuple[str | None, str]:
    if config.dqd_open_idempotency_enabled:
        return _confirmation_at(0), "创建请求结果暂不可确认，已进入自动结果确认"
    if config.dqd_open_502_retry_enabled and error.status_code == 502:
        delay = config.dqd_open_502_retry_delay_seconds
        return _retry_at(delay), f"创建接口返回 HTTP 502，将在 {delay} 秒后自动重试一次"
    return None, "创建请求结果暂不可确认，已停止直接重发"


def _upstream_request_id(diagnostics: Any) -> str | None:
    if not isinstance(diagnostics, dict):
        return None
    value = diagnostics.get("request_id")
    return str(value).strip()[:200] if value not in (None, "") else None


def _create_article_with_stable_key(
    client: DqdOpenClient,
    config: AppConfig,
    article: dict[str, Any],
    tabs: dict[str, Any] | list[dict[str, Any]],
    publish_account: dict[str, Any] | None,
    client_request_id: str,
    status: int | None = None,
):
    """Call the client while preserving compatibility with older test clients.

    The request key is sent only after the upstream idempotency capability is
    explicitly enabled. Persisting it locally is still useful for diagnostics
    and for a future reconciliation call when the capability is disabled.
    """

    kwargs: dict[str, Any] = {}
    if publish_account is not None:
        kwargs["publish_account"] = publish_account
    if config.dqd_open_idempotency_enabled:
        kwargs["client_request_id"] = client_request_id
    if status is not None:
        kwargs["status"] = status
    return client.create_article(article, tabs, **kwargs)


def _publish_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") == "READY_TO_PUBLISH"


def _retry_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") in {"READY_TO_PUBLISH", "PUBLISH_FAILED", "MAPPING_BLOCKED"}


def _existing_draft_result(
    article: dict[str, Any],
    *,
    retry: bool,
    status: str | None = None,
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
        "status": status or article.get("status") or "DRAFT_CREATED",
    }


def _current_tabs(article: dict[str, Any]) -> list[dict[str, Any]]:
    tabs = article.get("tabs") or []
    if tabs:
        return [dict(tab) for tab in tabs if tab.get("backend_tab_id") not in (None, "")]
    tab_id = article.get("tab_id")
    backend_tab_id = article.get("backend_tab_id")
    if tab_id in (None, "") or backend_tab_id in (None, ""):
        return []
    return [{
        "id": tab_id,
        "backend_tab_id": backend_tab_id,
        "name": article.get("tab_name") or "",
    }]


def _as_publish_mode(value: Any, default: int | None = None) -> int | None:
    """Normalize a stored DQD mode (0=draft, 1=publish)."""

    if value in (None, ""):
        return default
    try:
        mode = int(value)
    except (TypeError, ValueError):
        return default
    return mode if mode in {0, 1} else default


def _article_snapshot_mode(article: dict[str, Any]) -> int | None:
    """Read the sticky mode field using both current and migration names."""

    for key in ("publish_mode", "publish_mode_snapshot", "article_publish_mode"):
        mode = _as_publish_mode(article.get(key))
        if mode is not None:
            return mode
    return None


def _save_publish_mode_snapshot(
    article_id: int,
    mode: int,
    connection,
    article: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the first-request mode when the repository supports it.

    The repository is being upgraded independently in deployments. Prefer its
    explicit helper, then fall back to a guarded SQL update for databases that
    already have the new columns. Older databases remain fully compatible.
    """

    mode = _as_publish_mode(mode, 0) or 0
    saver = getattr(repo, "set_article_publish_mode", None)
    if callable(saver):
        try:
            updated = saver(article_id, mode, connection)
        except TypeError:
            updated = saver(article_id, connection, publish_mode=mode)
        if isinstance(updated, dict):
            return updated

    try:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(articles)").fetchall()
        }
    except Exception:  # pragma: no cover - defensive for test doubles
        columns = set()
    if "publish_mode" in columns:
        now = _utc_now()
        with connection:
            if "publish_mode_decided_at" in columns:
                connection.execute(
                    "UPDATE articles SET publish_mode=?, publish_mode_decided_at=COALESCE(publish_mode_decided_at,?), updated_at=? WHERE id=? AND publish_mode IS NULL",
                    (mode, now, now, article_id),
                )
            else:
                connection.execute(
                    "UPDATE articles SET publish_mode=?, updated_at=? WHERE id=? AND publish_mode IS NULL",
                    (mode, now, article_id),
                )
    current = repo.get_article(article_id, connection) or dict(article or {})
    current["publish_mode"] = mode
    return current


def _apply_language_publish_guard(
    article: dict[str, Any],
    configured_mode: int,
    connection,
) -> tuple[int, dict[str, Any]]:
    """Downgrade a first direct-publish attempt when most text is non-Chinese."""

    check = analyze_body_language(article.get("body_html"))
    effective_mode = (
        0
        if int(configured_mode) == 1 and check["exceeds_threshold"]
        else int(configured_mode)
    )
    check.update({
        "configured_publish_mode": int(configured_mode),
        "effective_publish_mode": effective_mode,
        "downgraded_to_draft": effective_mode != int(configured_mode),
    })
    if check["downgraded_to_draft"]:
        check["reason"] = "正文非中文文字比例超过60%，已自动降级为创建草稿"

    quality = article.get("quality")
    quality_result = dict(quality) if isinstance(quality, dict) else {}
    if quality_result.get("language_check") != check:
        quality_result["language_check"] = check
        article = repo.save_quality(
            int(article["id"]),
            quality_result,
            connection,
            status=str(article.get("status") or "READY_TO_PUBLISH"),
        )
    return effective_mode, article


def _resolve_publish_mode(
    article: dict[str, Any],
    tabs: list[dict[str, Any]],
    connection,
    config: AppConfig,
) -> tuple[int, dict[str, Any]]:
    """Resolve one stable mode from article, source override, then tabs.

    A snapshot always wins. An explicit source override (0/1) wins over all
    tab settings. A source following its tabs uses the tab modes and mixed tab
    modes are blocked instead of silently preferring direct publish.
    """

    snapshot = _article_snapshot_mode(article)
    if snapshot is not None:
        return snapshot, article

    # New repositories perform the mode read and snapshot write under the
    # same SQLite writer lock. Use that path whenever available so a tab toggle
    # cannot race the first publish request.
    resolver = getattr(repo, "resolve_article_publish_mode", None)
    ensurer = getattr(repo, "ensure_article_publish_mode", None)
    if callable(resolver):
        try:
            resolved = resolver(int(article["id"]), connection)
        except TypeError:
            resolved = resolver(int(article["id"]))
        if isinstance(resolved, dict):
            resolved_snapshot = _as_publish_mode(resolved.get("snapshot"))
            if resolved_snapshot is not None:
                current = repo.get_article(int(article["id"]), connection) or article
                return resolved_snapshot, current
            if resolved.get("conflict"):
                names = [
                    str(tab.get("name") or tab.get("backend_tab_id") or "未知栏目")
                    for tab in tabs
                ]
                raise PublishModeConflict(
                    "文章绑定的多个栏目发布模式不一致，无法提交："
                    + "、".join(names or ["未知栏目"])
                )
            mode = _as_publish_mode(resolved.get("publish_mode"))
            if mode is not None:
                effective_mode, article = _apply_language_publish_guard(
                    article, mode, connection
                )
                if effective_mode != mode:
                    return effective_mode, _save_publish_mode_snapshot(
                        int(article["id"]), effective_mode, connection, article
                    )
                if callable(ensurer):
                    try:
                        updated = ensurer(int(article["id"]), connection)
                    except TypeError:
                        updated = ensurer(int(article["id"]))
                    except ValueError as exc:
                        if "conflict" in str(exc).lower() or "冲突" in str(exc):
                            raise PublishModeConflict(str(exc)) from exc
                        raise
                    if isinstance(updated, dict):
                        # The ensure step re-reads configuration while holding
                        # the writer lock. A concurrent source/tab change can
                        # therefore produce a different snapshot than the
                        # earlier diagnostic read; the persisted snapshot is
                        # the authority for the request we are about to send.
                        snapshot_mode = _article_snapshot_mode(updated)
                        return (mode if snapshot_mode is None else snapshot_mode), updated
                return mode, _save_publish_mode_snapshot(
                    int(article["id"]), mode, connection, article
                )

    source = repo.get_source(str(article.get("source") or ""), connection)
    source_override = _as_publish_mode(
        source.get("publish_mode_override") if source else None
    )
    if source_override is not None:
        effective_mode, article = _apply_language_publish_guard(
            article, source_override, connection
        )
        return effective_mode, _save_publish_mode_snapshot(
            int(article["id"]), effective_mode, connection, article
        )

    tab_rows: list[dict[str, Any]] = []
    for tab in tabs:
        row = dict(tab)
        mode = _as_publish_mode(row.get("publish_mode"))
        if mode is None and row.get("id") not in (None, ""):
            getter = getattr(repo, "get_tab", None)
            if callable(getter):
                try:
                    configured = getter(int(row["id"]), connection)
                except TypeError:
                    configured = getter(int(row["id"]))
                if configured:
                    row.update(configured)
                    mode = _as_publish_mode(configured.get("publish_mode"))
        tab_rows.append({**row, "publish_mode": mode})

    configured_modes = {
        int(row["publish_mode"])
        for row in tab_rows
        if row.get("publish_mode") in {0, 1}
    }
    if len(configured_modes) > 1:
        names = [str(row.get("name") or row.get("backend_tab_id") or "未知栏目") for row in tab_rows]
        raise PublishModeConflict(
            "文章绑定的多个栏目发布模式不一致，无法提交：" + "、".join(names)
        )
    mode = next(iter(configured_modes), _as_publish_mode(config.dqd_open_status, 0) or 0)
    effective_mode, article = _apply_language_publish_guard(article, mode, connection)
    return effective_mode, _save_publish_mode_snapshot(
        int(article["id"]), effective_mode, connection, article
    )


def _success_status(mode: int) -> str:
    return "PUBLISHED" if int(mode) == 1 else "DRAFT_CREATED"


def _success_label(mode: int) -> str:
    return "直接发布" if int(mode) == 1 else "草稿创建"


def _legacy_mode_for_existing_archive(
    article: dict[str, Any],
    connection,
) -> tuple[int, dict[str, Any]]:
    """Use pre-snapshot source configuration for an existing archive."""

    source = repo.get_source(str(article.get("source") or ""), connection)
    mode = _as_publish_mode(source.get("publish_mode") if source else None, 0) or 0
    return mode, _save_publish_mode_snapshot(
        int(article["id"]), mode, connection, article
    )


def _record_confirmation_created(
    article_id: int,
    connection,
    *,
    mode: int,
    dqd_archive_id: int,
    request_id: str | None,
    next_confirm_at: str | None = None,
    message: str = "",
    payload: Any = None,
    expected_updated_at: str | None = None,
    claim_token: str | None = None,
):
    """Record confirmation using the upgraded repository when available."""

    recorder = repo.record_draft_confirmation_result
    kwargs = {
        "outcome": "CREATED",
        "dqd_archive_id": dqd_archive_id,
        "request_id": request_id,
        "next_confirm_at": next_confirm_at,
        "message": message,
        "payload": payload,
        "expected_updated_at": expected_updated_at,
        "claim_token": claim_token,
    }
    try:
        parameters = inspect.signature(recorder).parameters
    except (TypeError, ValueError):  # pragma: no cover
        parameters = {}
    if "publish_mode" in parameters:
        kwargs["publish_mode"] = mode
    if "target_status" in parameters:
        kwargs["target_status"] = _success_status(mode)
    updated = recorder(article_id, connection, **kwargs)
    # Compatibility for old repositories: confirmation always lands in
    # DRAFT_CREATED there, so promote a mode-1 result explicitly.
    if int(mode) == 1 and updated and updated.get("status") != "PUBLISHED":
        try:
            updated = repo.transition_status(
                article_id,
                "PUBLISHED",
                connection,
                from_status=updated.get("status"),
                event_type="PUBLISHED",
                message=message or "开放平台直接发布成功",
                payload=payload,
            )
        except TypeError:
            # The upgraded repository handles this atomically.
            pass
    return updated


def _publish_account_for_attempt(
    article: dict[str, Any],
    connection,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return the article's sticky account snapshot, assigning it once if enabled."""

    user_id = article.get("publish_user_id")
    user_name = str(article.get("publish_user_name") or "").strip()
    if user_id not in (None, "") or user_name:
        if user_id in (None, "") or not user_name:
            raise ValueError("文章的发布账号快照不完整，请检查数据后重试")
        account = {"dqd_user_id": int(user_id), "user_name": user_name}
        return account, article

    pool_enabled = bool(
        repo.get_setting("publish_account_pool_enabled", False, connection)
    )
    if not pool_enabled:
        return None, article

    assigned = repo.assign_publish_account(int(article["id"]), connection)
    account = {
        "dqd_user_id": int(assigned["publish_user_id"]),
        "user_name": str(assigned["publish_user_name"]),
    }
    return account, assigned


def _publish_account_payload(
    publish_account: dict[str, Any] | None,
) -> dict[str, Any]:
    if publish_account is None:
        return {}
    return {
        "publish_user_id": int(publish_account["dqd_user_id"]),
        "publish_user_name": str(publish_account["user_name"]),
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
    duplicate_of = repo.get_duplicate_canonical(article_id, connection)
    if duplicate_of is not None:
        archive_id = int(duplicate_of.get("dqd_archive_id") or 0)
        detail = f"，已有懂球帝草稿 archive_id={archive_id}" if archive_id > 0 else ""
        repo.transition_status(
            article_id,
            "SOURCE_DUPLICATE",
            connection,
            event_type="SOURCE_DUPLICATE_DETECTED",
            message=f"与本地文章 #{duplicate_of['id']} 的来源文章 ID 相同，已自动拦截{detail}",
            payload={
                "duplicate_of_article_id": int(duplicate_of["id"]),
                "archive_id": archive_id or None,
            },
        )
        raise ValueError(
            f"该文章与本地文章 #{duplicate_of['id']} 的来源文章 ID 相同，已自动拦截{detail}"
        )
    if current.get("status") not in allowed_statuses:
        raise ValueError(f"当前状态为 {current.get('status_label') or current.get('status') or '未知'}，不能创建草稿")

    archive_id = int(current.get("dqd_archive_id") or 0)
    if archive_id > 0:
        mode = _article_snapshot_mode(current)
        if mode is None:
            # This archive predates article-level snapshots. Current tab
            # settings may have changed since it was submitted.
            mode, current = _legacy_mode_for_existing_archive(current, connection)
        target_status = _success_status(mode)
        if current.get("status") not in {target_status, "ALREADY_PUBLISHED"}:
            recovered = repo.transition_status_if_current(
                article_id,
                target_status,
                connection,
                allowed_from=allowed_statuses,
                event_type="PUBLISHED" if mode else "DRAFT_ALREADY_EXISTS",
                message=(
                    "检测到已存在懂球帝文章，直接恢复为已发布"
                    if mode
                    else "检测到已存在懂球帝草稿，直接恢复为草稿已创建"
                ),
                payload={
                    "archive_id": archive_id,
                    "draft_url": build_draft_url(archive_id),
                    "retry": retry,
                    "publish_mode": mode,
                },
            )
            if recovered is None:
                refreshed = repo.get_article(article_id, connection)
                if refreshed and int(refreshed.get("dqd_archive_id") or 0) > 0:
                    return _existing_draft_result(refreshed, retry=retry, status=refreshed.get("status"))
                raise ValueError("文章状态已变化，请刷新后重试")
            current = recovered
        return _existing_draft_result(current, retry=retry, status=target_status)

    tabs = _current_tabs(current)
    if not tabs:
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

    try:
        publish_mode, current = _resolve_publish_mode(current, tabs, connection, config)
    except PublishModeConflict as exc:
        repo.transition_status_if_current(
            article_id,
            "MAPPING_BLOCKED",
            connection,
            allowed_from=allowed_statuses,
            event_type="PUBLISH_MODE_CONFLICT",
            message=str(exc),
            payload={"tabs": tabs, "reason": "publish_mode_conflict", "retry": retry},
        )
        raise

    try:
        publish_account, current = _publish_account_for_attempt(current, connection)
    except ValueError as exc:
        repo.transition_status_if_current(
            article_id,
            "PUBLISH_FAILED",
            connection,
            allowed_from=allowed_statuses,
            event_type=blocked_event_type,
            message=str(exc)[:300],
            payload={
                "retry": retry,
                "source": current.get("source"),
                "reason": "publish_account_unavailable",
            },
        )
        raise
    account_payload = _publish_account_payload(publish_account)

    claimed = repo.transition_status_if_current(
        article_id,
        "PUBLISHING",
        connection,
        allowed_from=allowed_statuses,
        event_type=start_event_type,
        message=start_message,
        payload={
            "retry": retry,
            "source": current.get("source"),
            "publish_mode": publish_mode,
            **account_payload,
        },
    )
    if claimed is None:
        refreshed = repo.get_article(article_id, connection)
        if refreshed is not None and int(refreshed.get("dqd_archive_id") or 0) > 0:
            return _existing_draft_result(refreshed, retry=retry)
        raise DraftClaimSkipped(
            f"当前状态为 {refreshed.get('status_label') if refreshed else current.get('status_label') or current.get('status') or '未知'}，已有草稿创建任务正在处理"
        )

    client = DqdOpenClient(config)
    client_request_id = repo.ensure_client_request_id(article_id, connection)

    try:
        selected_tabs = tabs[0] if len(tabs) == 1 else tabs
        draft = _create_article_with_stable_key(
            client,
            config,
            current,
            selected_tabs,
            publish_account,
            client_request_id,
            publish_mode,
        )
        request_id = _upstream_request_id(draft.diagnostics)
        repo.update_article_backend_refs(
            article_id,
            connection,
            dqd_archive_id=draft.archive_id,
            upstream_request_id=request_id,
        )
        success_status = _success_status(publish_mode)
        repo.transition_status(
            article_id,
            success_status,
            connection,
            from_status="PUBLISHING",
            event_type="PUBLISHED" if publish_mode else success_event_type,
            message=(
                f"开放平台直接发布成功，archive_id={draft.archive_id}"
                if publish_mode
                else f"{success_message_prefix}，archive_id={draft.archive_id}"
            ),
            payload={
                "archive_id": draft.archive_id,
                "draft_url": build_draft_url(draft.archive_id),
                "request_url": draft.request_url,
                "request_fields": [key for key, _ in draft.form_fields],
                "diagnostics": draft.diagnostics,
                "client_request_id": client_request_id,
                "upstream_request_id": request_id,
                "retry": retry,
                "publish_mode": publish_mode,
                "result_status": success_status,
                **account_payload,
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
            "client_request_id": client_request_id,
            "upstream_request_id": request_id,
            "status": success_status,
            "publish_mode": publish_mode,
            "published": bool(publish_mode),
            **account_payload,
        }
    except DqdOpenClientError as exc:
        payload = {
            "error": str(exc),
            "status_code": exc.status_code,
            "result_unknown": exc.result_unknown,
            "client_request_id": client_request_id,
            "retry": retry,
            "publish_mode": publish_mode,
            **account_payload,
        }
        if getattr(exc, "payload", None) is not None:
            payload["response_payload"] = exc.payload
        if getattr(exc, "diagnostics", None):
            payload["diagnostics"] = exc.diagnostics
        if exc.result_unknown:
            request_id = _upstream_request_id(exc.diagnostics)
            next_confirm_at, message = _initial_confirmation_schedule(config, exc)
            repo.mark_draft_result_unknown(
                article_id,
                connection,
                request_id=request_id,
                next_confirm_at=next_confirm_at,
                schedule_confirmation=next_confirm_at is not None,
                message=message,
                payload={
                    **payload,
                    "retry_mode": (
                        "idempotent_confirmation"
                        if config.dqd_open_idempotency_enabled
                        else ("single_502_retry" if next_confirm_at else "none")
                    ),
                },
            )
        else:
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
        payload = {"error": str(exc), "retry": retry, **account_payload}
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
    config: AppConfig | None = None,
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
            mode = _article_snapshot_mode(article)
            if mode is None:
                mode, article = _legacy_mode_for_existing_archive(article, conn)
            target_status = _success_status(mode)
            updated = repo.transition_status_if_current(
                int(article["id"]),
                target_status,
                conn,
                allowed_from={"PUBLISHING"},
                current_updated_at=article.get("updated_at"),
                event_type="PUBLISHED" if mode else "PUBLISHING_RECOVERED",
                message=(
                    "检测到文章已发布，自动恢复为已发布"
                    if mode
                    else "检测到草稿已创建，自动恢复为草稿已创建"
                ),
                payload={
                    "archive_id": archive_id,
                    "draft_url": build_draft_url(archive_id),
                    "publish_mode": mode,
                },
            )
            if updated is not None:
                recovered += 1
            continue
        auto_confirm = bool(config and config.dqd_open_idempotency_enabled)
        updated = repo.transition_status_if_current(
            int(article["id"]),
            "DRAFT_CONFIRMING",
            conn,
            allowed_from={"PUBLISHING"},
            current_updated_at=article.get("updated_at"),
            event_type="PUBLISHING_TIMED_OUT",
            message="创建草稿请求超时，结果暂不可确认，已停止直接重发",
            payload={
                "reason": "publishing_timeout",
                "auto_confirmation_enabled": auto_confirm,
            },
        )
        if updated is not None:
            timed_out += 1
            if auto_confirm:
                repo.mark_draft_result_unknown(
                    int(article["id"]),
                    conn,
                    next_confirm_at=_confirmation_at(0),
                    schedule_confirmation=True,
                    message="创建草稿请求超时，已进入自动结果确认",
                    allowed_from={"DRAFT_CONFIRMING"},
                    payload={"reason": "publishing_timeout"},
                )
    return {"recovered": recovered, "timed_out": timed_out, "checked": len(articles)}


def confirm_due_draft_results(
    config: AppConfig,
    connection,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Process due ambiguous-create jobs.

    Idempotent mode retains the existing confirmation schedule. Without an
    idempotency contract, only the explicitly enabled one-time HTTP 502 retry
    is eligible; it is never scheduled a second time.
    """

    confirmation_enabled = (
        config.dqd_open_idempotency_enabled or config.dqd_open_502_retry_enabled
    )
    if not config.publisher_enabled or not confirmation_enabled:
        return {
            "checked": 0,
            "confirmed": 0,
            "pending": 0,
            "failed": 0,
            "exhausted": 0,
            "skipped": True,
        }

    due = repo.list_due_draft_confirmations(connection, limit=limit)
    checked = confirmed = pending = failed = exhausted = 0
    items: list[dict[str, Any]] = []
    for item in due:
        article_id = int(item["id"])
        claimed = repo.claim_due_draft_confirmation(
            article_id,
            str(item.get("updated_at") or ""),
            connection,
        )
        if claimed is None:
            continue
        checked += 1
        request_key = repo.ensure_client_request_id(article_id, connection)
        claimed = repo.get_article(article_id, connection) or claimed
        claim_token = claimed.get("draft_confirm_claim_token")
        expected_updated_at = claimed.get("updated_at")
        tabs = _current_tabs(claimed)
        try:
            publish_mode, claimed = _resolve_publish_mode(
                claimed, tabs, connection, config
            )
            expected_updated_at = claimed.get("updated_at")
            account, claimed = _publish_account_for_attempt(claimed, connection)
            expected_updated_at = claimed.get("updated_at")
            selected_tabs = tabs[0] if len(tabs) == 1 else tabs
            if not tabs:
                raise ValueError("来源尚未绑定后台栏目，无法确认草稿结果")

            draft = _create_article_with_stable_key(
                DqdOpenClient(config),
                config,
                claimed,
                selected_tabs,
                account,
                request_key,
                publish_mode,
            )
            request_id = _upstream_request_id(draft.diagnostics)
            updated = _record_confirmation_created(
                article_id,
                connection,
                mode=publish_mode,
                dqd_archive_id=draft.archive_id,
                request_id=request_id,
                expected_updated_at=expected_updated_at,
                claim_token=claim_token,
                message=(
                    f"自动核对确认{_success_label(publish_mode)}成功，archive_id={draft.archive_id}"
                    if config.dqd_open_idempotency_enabled
                    else f"HTTP 502 后自动重试{_success_label(publish_mode)}成功，archive_id={draft.archive_id}"
                ),
                payload={
                    "diagnostics": draft.diagnostics,
                    "client_request_id": request_key,
                    "publish_mode": publish_mode,
                    "retry_mode": (
                        "idempotent_confirmation"
                        if config.dqd_open_idempotency_enabled
                        else "single_502_retry"
                    ),
                },
            )
            if updated is not None:
                confirmed += 1
                items.append({
                    "article_id": article_id,
                    "archive_id": draft.archive_id,
                    "status": _success_status(publish_mode),
                    "publish_mode": publish_mode,
                    "published": bool(publish_mode),
                })
            continue
        except DqdOpenClientError as exc:
            request_id = _upstream_request_id(exc.diagnostics)
            event_payload = {
                "error": str(exc),
                "status_code": exc.status_code,
                "result_unknown": exc.result_unknown,
                "client_request_id": request_key,
                "diagnostics": exc.diagnostics,
            }
            if config.dqd_open_idempotency_enabled and exc.result_unknown:
                next_at = _confirmation_at(int(claimed.get("draft_confirm_attempts") or 0))
                updated = repo.record_draft_confirmation_result(
                    article_id,
                    connection,
                    outcome="PENDING",
                    request_id=request_id,
                    next_confirm_at=next_at,
                    schedule_confirmation=next_at is not None,
                    expected_updated_at=expected_updated_at,
                    claim_token=claim_token,
                    message="自动核对仍未得到 archive_id，暂不重发",
                    payload=event_payload,
                )
                pending += 1
                if next_at is None:
                    exhausted += 1
                items.append({
                    "article_id": article_id,
                    "error": str(exc)[:300],
                    "pending": True,
                    "exhausted": next_at is None,
                })
            elif config.dqd_open_idempotency_enabled:
                repo.record_draft_confirmation_result(
                    article_id,
                    connection,
                    outcome="FAILED",
                    request_id=request_id,
                    expected_updated_at=expected_updated_at,
                    claim_token=claim_token,
                    message=str(exc)[:300],
                    payload=event_payload,
                )
                failed += 1
            else:
                # The first 502 may already have created a remote draft. A
                # non-idempotent retry must therefore never open a third POST.
                repo.record_draft_confirmation_result(
                    article_id,
                    connection,
                    outcome="PENDING",
                    request_id=request_id,
                    schedule_confirmation=False,
                    expected_updated_at=expected_updated_at,
                    claim_token=claim_token,
                    message="HTTP 502 后已自动重试一次，结果仍未确认，已停止继续重试",
                    payload={**event_payload, "retry_mode": "single_502_retry"},
                )
                pending += 1
                exhausted += 1
                items.append({
                    "article_id": article_id,
                    "error": str(exc)[:300],
                    "pending": True,
                    "exhausted": True,
                })
            continue
        except PublishModeConflict as exc:
            repo.record_draft_confirmation_result(
                article_id,
                connection,
                outcome="FAILED",
                expected_updated_at=expected_updated_at,
                claim_token=claim_token,
                message=str(exc),
                payload={"error": str(exc), "reason": "publish_mode_conflict"},
            )
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - preserve uncertain remote outcomes
            if isinstance(exc, ValueError) and config.dqd_open_idempotency_enabled:
                repo.record_draft_confirmation_result(
                    article_id,
                    connection,
                    outcome="FAILED",
                    expected_updated_at=expected_updated_at,
                    claim_token=claim_token,
                    message=str(exc)[:300],
                    payload={"error": str(exc), "client_request_id": request_key},
                )
                failed += 1
                continue
            next_at = (
                _confirmation_at(int(claimed.get("draft_confirm_attempts") or 0))
                if config.dqd_open_idempotency_enabled
                else None
            )
            repo.record_draft_confirmation_result(
                article_id,
                connection,
                outcome="PENDING",
                next_confirm_at=next_at,
                schedule_confirmation=next_at is not None,
                expected_updated_at=expected_updated_at,
                claim_token=claim_token,
                message=(
                    "自动核对请求异常，暂不重发"
                    if config.dqd_open_idempotency_enabled
                    else "HTTP 502 后的单次自动重试异常，已停止继续重试"
                ),
                payload={
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "client_request_id": request_key,
                },
            )
            pending += 1
            if next_at is None:
                exhausted += 1
            items.append({"article_id": article_id, "error": str(exc)[:300], "pending": True})

    return {
        "checked": checked,
        "confirmed": confirmed,
        "pending": pending,
        "failed": failed,
        "exhausted": exhausted,
        "items": items,
        "skipped": False,
    }


class DraftConfirmationController:
    """Run due confirmation jobs without blocking the scheduler thread."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> bool:
        confirmation_enabled = (
            self.config.dqd_open_idempotency_enabled
            or self.config.dqd_open_502_retry_enabled
        )
        if not self.config.publisher_enabled or not confirmation_enabled:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(
            target=self._run,
            name="draft-confirmation-worker",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True

    def _run(self) -> None:
        connection = _connect(self.config.database_path)
        try:
            confirm_due_draft_results(self.config, connection)
        except Exception:  # noqa: BLE001 - keep future confirmation ticks alive
            logger.exception("自动核对草稿结果失败")
        finally:
            connection.close()
            with self._lock:
                self._running = False


def publish_ready_articles(config: AppConfig, connection, *, limit: int = 200) -> dict[str, Any]:
    # 无论 publisher 是否启用，都先清理卡住的 PUBLISHING 文章，避免状态永久卡死
    recovery = recover_stale_publishing_articles(connection, config=config)
    confirmation = confirm_due_draft_results(config, connection)
    if not config.publisher_enabled:
        return {
            "draft_created": 0,
            "published": 0,
            "confirmed_published": 0,
            "failed": 0,
            "skipped": 0,
            "duplicate_skipped": 0,
            "mapping_blocked": 0,
            "recovered": recovery["recovered"],
            "timed_out": recovery["timed_out"],
            "confirmed": confirmation["confirmed"],
            "confirming": confirmation["pending"],
            "confirmation_exhausted": confirmation["exhausted"],
            "items": [],
            "message": "发布 worker 未启用，跳过创建草稿",
        }
    if not config.dqd_open_configured:
        return {
            "draft_created": 0,
            "published": 0,
            "confirmed_published": 0,
            "failed": 0,
            "skipped": 0,
            "duplicate_skipped": 0,
            "mapping_blocked": 0,
            "recovered": recovery["recovered"],
            "timed_out": recovery["timed_out"],
            "confirmed": confirmation["confirmed"],
            "confirming": confirmation["pending"],
            "confirmation_exhausted": confirmation["exhausted"],
            "items": [],
            "message": "开放平台 appid/appsecret/enname 未配置，跳过创建草稿",
        }

    articles = repo.list_articles(connection, status="READY_TO_PUBLISH", limit=limit)
    result_items: list[dict[str, Any]] = []
    draft_created = published = failed = skipped = duplicate_skipped = mapping_blocked = confirming = 0
    confirmed_published = sum(
        1 for item in confirmation.get("items", []) if item.get("published")
    )
    published += confirmed_published

    for article in articles:
        article_id = int(article["id"])
        current = repo.get_article(article_id, connection) or article
        if not _publish_eligible(current):
            skipped += 1
            continue
        duplicate_of = repo.get_duplicate_canonical(article_id, connection)
        if duplicate_of is not None:
            skipped += 1
            duplicate_skipped += 1
            repo.transition_status(
                article_id,
                "SOURCE_DUPLICATE",
                connection,
                event_type="SOURCE_DUPLICATE_DETECTED",
                message=f"与本地文章 #{duplicate_of['id']} 的来源文章 ID 相同，已自动拦截",
                payload={"duplicate_of_article_id": int(duplicate_of["id"])},
            )
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": f"与本地文章 #{duplicate_of['id']} 的来源文章 ID 相同，已自动拦截",
                "skipped": True,
            })
            continue
        if not _current_tabs(current):
            skipped += 1
            mapping_blocked += 1
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
            if result.get("published") or result.get("status") == "PUBLISHED":
                published += 1
            else:
                draft_created += 1
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "archive_id": result["archive_id"],
                "status": result.get("status") or "DRAFT_CREATED",
                "publish_mode": result.get("publish_mode", 0),
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
            if exc.result_unknown:
                confirming += 1
            else:
                failed += 1
            logger.exception("创建草稿失败 article_id=%s", article_id)
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
                "status_code": exc.status_code,
                "result_unknown": exc.result_unknown,
            })
        except PublishModeConflict as exc:
            skipped += 1
            mapping_blocked += 1
            logger.warning("文章栏目发布模式冲突 article_id=%s: %s", article_id, exc)
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
                "mapping_blocked": True,
            })
        except Exception as exc:  # noqa: BLE001 - isolate one bad article
            failed += 1
            logger.exception("创建草稿时出现未处理异常 article_id=%s", article_id)
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
            })

    message = "发布任务完成"
    if recovery["recovered"] or recovery["timed_out"]:
        message += f"，自动恢复 {recovery['recovered']} 篇"
        if recovery["timed_out"]:
            message += f"，超时恢复 {recovery['timed_out']} 篇"
    if failed:
        message += f"，失败 {failed} 条"
    if confirming:
        message += f"，{confirming} 条进入自动核对"
    if published:
        message += f"，直接发布 {published} 篇"
    return {
        "draft_created": draft_created,
        "published": published,
        "confirmed_published": confirmed_published,
        "failed": failed,
        "skipped": skipped,
        "duplicate_skipped": duplicate_skipped,
        "mapping_blocked": mapping_blocked,
        "confirming": confirming + confirmation["pending"],
        "recovered": recovery["recovered"],
        "timed_out": recovery["timed_out"],
        "confirmed": confirmation["confirmed"],
        "confirmation_exhausted": confirmation["exhausted"],
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
        allowed_statuses={"READY_TO_PUBLISH", "PUBLISH_FAILED", "MAPPING_BLOCKED"},
        start_event_type="DRAFT_RETRY_STARTED",
        success_event_type="DRAFT_RETRY_SUCCEEDED",
        failure_event_type="DRAFT_RETRY_FAILED",
        blocked_event_type="DRAFT_RETRY_BLOCKED",
        start_message="开始重新创建懂球帝草稿",
        success_message_prefix="重新创建懂球帝草稿成功",
        retry=True,
        block_if_missing_tab=True,
    )
