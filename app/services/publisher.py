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
from . import title_dedup
from .dqd_open_client import DqdOpenClient, DqdOpenClientError
from .open_platform import build_draft_url
from .quality import (
    LLMCallError,
    LLMService,
    analyze_body_language,
    check_league_membership,
    classify_article_tab,
    normalize_competition_name,
    should_check_fallback_tab,
)


logger = logging.getLogger(__name__)
PUBLISHING_STALE_SECONDS = 10 * 60

# 这些状态说明另一个发布轮次已经接管了同一篇文章，重复领取属于无害竞态。
_DOWNSTREAM_CLAIMED_STATUSES = frozenset({
    "PUBLISHING",
    "DRAFT_CONFIRMING",
    "DRAFT_CREATED",
    "PUBLISHED",
    "ALREADY_PUBLISHED",
})


class DraftClaimSkipped(RuntimeError):
    """Raised when another request already claimed the draft creation slot."""


class PublishModeConflict(ValueError):
    """Raised when one article maps to tabs with different publish modes."""


class ManualReconcileRequired(RuntimeError):
    """Raised when retrying could duplicate an article that upstream may already hold."""


class TitleDuplicateBlocked(RuntimeError):
    """Raised when the post-guard dedup pass stops a draft that was already claimed."""


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


def _retry_at(delay_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(1, int(delay_seconds)))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _initial_confirmation_schedule(
    config: AppConfig,
    error: DqdOpenClientError,
    publish_mode: int,
) -> tuple[str | None, str]:
    """Decide whether exactly one automatic retry is safe.

    上游既没有幂等请求键也没有查询接口，重发等于赌「上一次其实没成功」。只有创建
    草稿值得赌：重复草稿留在后台且能人工删除。直接发布的重复对读者可见，一律不
    重发，宁可落人工核对。
    """

    if not config.dqd_open_502_retry_enabled or error.status_code != 502:
        return None, "创建请求结果暂不可确认，已停止直接重发"
    if publish_mode:
        return None, "直接发布结果暂不可确认，为避免读者看到重复文章，不再自动重发"
    delay = config.dqd_open_502_retry_delay_seconds
    return _retry_at(delay), f"创建接口返回 HTTP 502，将在 {delay} 秒后自动重试一次"


def _upstream_request_id(diagnostics: Any) -> str | None:
    if not isinstance(diagnostics, dict):
        return None
    value = diagnostics.get("request_id")
    return str(value).strip()[:200] if value not in (None, "") else None


def _submit_article(
    client: DqdOpenClient,
    article: dict[str, Any],
    tabs: dict[str, Any] | list[dict[str, Any]],
    publish_account: dict[str, Any] | None,
    client_request_id: str,
    status: int | None = None,
):
    """Submit one article to the open platform.

    ``client_request_id`` 只进本地诊断，不会作为表单字段发给上游——开放平台没有
    幂等请求键。保留它是为了把本地记录和上游返回的 request_id 对上，排查重复
    发布时能追溯到是哪一次提交。
    """

    kwargs: dict[str, Any] = {"client_request_id": client_request_id}
    if publish_account is not None:
        kwargs["publish_account"] = publish_account
    if status is not None:
        kwargs["status"] = status
    return client.create_article(article, tabs, **kwargs)


def _publish_eligible(article: dict[str, Any]) -> bool:
    return article.get("status") == "READY_TO_PUBLISH"


def _guard_upgraded_to_publish(article: dict[str, Any]) -> bool:
    """Whether the league guard turned a draft-mode article into a direct publish."""

    quality = article.get("quality")
    guard = quality.get("league_guard") if isinstance(quality, dict) else None
    return bool(guard.get("upgraded_to_publish")) if isinstance(guard, dict) else False


def _title_dedup_gate(config: AppConfig, connection, current: dict[str, Any]) -> dict[str, Any] | None:
    """Safety net: re-run title dedup right before draft creation.

    Catches duplicates whose earlier twin was published after the ingestion
    time check ran (manual review releases, slow batches).
    """

    if not config.title_dedup_enabled:
        return None
    mode = title_dedup.direct_publish_mode(int(current["id"]), current, connection)
    if mode != 1:
        return None
    candidates = repo.list_title_dedup_candidates(
        title_dedup.dedup_window_since(config.title_dedup_hours),
        int(current["id"]),
        connection,
    )
    return title_dedup.check_title_duplicate(
        config,
        current,
        candidates,
        publish_mode=mode,
        body_loader=lambda target_id: repo.get_article_body(target_id, connection),
    )


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
    """Return the columns that may be submitted for *article*.

    Non-publishable columns (「精选」/「法甲」) are dropped here so a legacy
    mapping left on an article ingested before those columns stopped being
    routed can never be sent to the backend.

    Switched-off columns are kept: pausing a column stops its articles from
    being published, not from being filed. They are submitted as drafts
    instead, which the mode resolver enforces by forcing mode 0 whenever any
    mapped column is switched off.
    """

    tabs = article.get("tabs") or []
    if tabs:
        return [dict(tab) for tab in tabs if repo.is_publishable_tab(tab)]
    tab_id = article.get("tab_id")
    backend_tab_id = article.get("backend_tab_id")
    if tab_id in (None, "") or backend_tab_id in (None, ""):
        return []
    legacy = {
        "id": tab_id,
        "backend_tab_id": backend_tab_id,
        "name": article.get("tab_name") or "",
    }
    return [legacy] if repo.is_publishable_tab(legacy) else []


def _missing_tab_message(article: dict[str, Any]) -> str:
    """Explain why an article has no submittable column."""

    dropped = [
        str(tab.get("name") or tab.get("backend_tab_id") or "未知栏目")
        for tab in _mapped_tabs(article)
        if not repo.is_publishable_tab(tab)
        and tab.get("backend_tab_id") not in (None, "")
    ]
    if dropped:
        return (
            "文章仅绑定了不可发布的栏目「"
            + "、".join(dict.fromkeys(dropped))
            + "」，无法创建草稿"
        )
    return "来源尚未绑定后台栏目，无法创建草稿"


def _mapped_tabs(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every column mapped to *article*, publishable or not."""

    tabs = article.get("tabs") or []
    if tabs:
        return [dict(tab) for tab in tabs]
    if article.get("tab_id") in (None, "") or article.get("backend_tab_id") in (None, ""):
        return []
    return [{
        "id": article.get("tab_id"),
        "backend_tab_id": article.get("backend_tab_id"),
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
        # 与 league guard 同理：沿用数据库现值，避免回写陈旧状态。
        article = repo.save_quality(
            int(article["id"]),
            quality_result,
            connection,
        )
    return effective_mode, article


def _make_league_guard_llm(config: AppConfig) -> LLMService | None:
    if not getattr(config, "llm_configured", False):
        return None
    return LLMService(
        config.llm_api_key,
        config.llm_base_url,
        config.llm_model,
        config.llm_timeout,
        config.llm_max_retries,
        config.llm_retry_delay_seconds,
    )


def _apply_ai_league_guard(
    article: dict[str, Any],
    configured_mode: int,
    tabs: list[dict[str, Any]],
    connection,
    config: AppConfig,
) -> tuple[int, dict[str, Any]]:
    """Upgrade a fallback-column draft to direct publish when AI confirms it fits.

    Only runs when the article would otherwise be a draft (mode 0) and reached a
    column with the guard enabled. A source set to direct publish (mode 1) is
    trusted as-is and never triggers the AI check. Only a positive,
    high-confidence verdict upgrades the article to publish; any negative,
    low-confidence, or model failure keeps it as a draft (fail closed).

    A resolved league short code normally means the column is already known, so
    those articles skip the check — unless their routing rule opted in via
    ``event_tab_rules.ai_guard_enabled``. That switch exists for catch-all short
    codes such as ``intl``, whose routed column is a guess rather than a fact.

    Cascade fallback: when the article does not belong to the guard column and
    that column configures ``ai_fallback_tab_ids``, the candidates are checked
    in order and the first positive, high-confidence verdict wins. A keyword
    prefilter gates each extra call. The article is then moved into that
    candidate column and upgraded to publish.

    Classifier mode (``league_guard_classifier_enabled``, default on) replaces
    that cascade once the article is rejected by the guard column: a single call
    picks one column out of every candidate returned by
    :func:`repository.list_guard_candidate_tabs`, so coverage no longer depends
    on an operator having filled in ``ai_fallback_tab_ids`` — roughly half the
    live guard columns leave it empty, which is why articles used to pile up as
    drafts after a correct "does not belong" verdict. Whether a reassigned
    article is published or stays a draft follows
    ``league_guard_reassign_publish_mode``.
    """

    if int(configured_mode) != 0:
        return int(configured_mode), article
    if str(article.get("route_league") or "").strip() and not repo.event_tab_rule_allows_ai_guard(
        article.get("route_rule_id"), connection
    ):
        return int(configured_mode), article
    guard_tab = next(
        (tab for tab in tabs if int(tab.get("ai_league_guard_enabled") or 0) == 1),
        None,
    )
    if guard_tab is None:
        return int(configured_mode), article

    tab_name = str(guard_tab.get("name") or "")
    definition = str(guard_tab.get("ai_league_guard_definition") or "")
    upgrade_reason: str | None = None
    keep_reason: str | None = None
    verdict: dict[str, Any] | None = None
    fallback_verdict: dict[str, Any] | None = None
    fallback_tab_name: str | None = None
    fallback_tab_id: int | None = None
    fallback_publish_mode: int | None = None
    fallback_tab_active: bool = True
    fallback_candidates: list[str] = []
    classifier_result: dict[str, Any] | None = None
    llm = _make_league_guard_llm(config)
    if llm is None:
        keep_reason = "AI 归属校验未配置模型，维持创建草稿"
    else:
        try:
            verdict = check_league_membership(
                str(article.get("title_final") or article.get("title") or ""),
                str(article.get("body_html") or ""),
                tab_name,
                definition,
                llm,
            )
        except LLMCallError as exc:
            keep_reason = f"AI 归属校验调用失败，维持创建草稿：{exc}"
        else:
            if (
                verdict["belongs"] is True
                and verdict["confidence"] >= config.league_guard_min_confidence
            ):
                upgrade_reason = (
                    f"AI 判断本篇属于「{tab_name or '兜底栏目'}」"
                    f"（confidence={verdict['confidence']:.2f}），"
                    f"已升级为直接发布：{verdict['reason']}"
                )
            elif config.league_guard_classifier_enabled:
                # 分类模式：不再逐个追问预配候选，一次调用就在全部候选栏目里选一个。
                # 覆盖面因此与 tabs.ai_fallback_tab_ids 配过没有无关——线上近半数
                # 栏目该字段为空，正是「AI 答完不属于就无处可去」的根因。
                article_title = str(
                    article.get("title_final") or article.get("title") or ""
                )
                article_body = str(article.get("body_html") or "")
                choices = [
                    row
                    for row in repo.list_guard_candidate_tabs(connection)
                    if int(row.get("id") or 0) != int(guard_tab.get("id") or 0)
                ]
                fallback_candidates = [str(row.get("name") or "") for row in choices]
                try:
                    classifier_result = classify_article_tab(
                        article_title, article_body, choices, llm
                    )
                except LLMCallError as exc_classify:
                    keep_reason = (
                        f"AI 判断本篇不属于「{tab_name or '兜底栏目'}」，"
                        f"栏目分类调用失败，维持创建草稿：{exc_classify}"
                    )
                else:
                    routed_id = classifier_result.get("tab_id")
                    routed_confidence = float(classifier_result.get("confidence") or 0.0)
                    routed_reason = str(classifier_result.get("reason") or "")
                    routed_row = (
                        repo.get_tab(int(routed_id), connection)
                        if routed_id is not None
                        else None
                    )
                    if routed_row is None:
                        keep_reason = (
                            f"AI 判断本篇不属于「{tab_name or '兜底栏目'}」，"
                            f"也不属于任何候选栏目，维持创建草稿：{routed_reason}"
                        )
                    elif routed_confidence < config.league_guard_min_confidence:
                        keep_reason = (
                            f"AI 判断本篇不属于「{tab_name or '兜底栏目'}」，"
                            f"分类到「{routed_row.get('name')}」但置信度不足"
                            f"（confidence={routed_confidence:.2f}），维持创建草稿：{routed_reason}"
                        )
                    else:
                        fallback_tab_id = int(routed_row["id"])
                        fallback_tab_name = str(routed_row.get("name") or "")
                        fallback_publish_mode = int(routed_row.get("publish_mode") or 0)
                        fallback_tab_active = repo.is_active_tab(routed_row)
                        fallback_verdict = classifier_result
                        upgrade_reason = (
                            f"AI 判断本篇不属于「{tab_name}」，分类到"
                            f"「{fallback_tab_name}」"
                            f"（confidence={routed_confidence:.2f}），"
                            f"已改挂该栏目"
                            + ("" if fallback_tab_active else "（该栏目已停用，只创建草稿）")
                            + f"：{routed_reason}"
                        )
            else:
                # Cascade: the article does not fit this column, so walk the
                # column's configured candidates (tabs.ai_fallback_tab_ids) in
                # order and take the first positive verdict. A cheap keyword
                # prefilter runs per candidate so the extra calls are only paid
                # for plausible ones.
                article_title = str(
                    article.get("title_final") or article.get("title") or ""
                )
                article_body = str(article.get("body_html") or "")
                skipped: list[str] = []
                rejected: list[str] = []
                failed: list[str] = []
                for candidate_id in repo.tab_fallback_tab_ids(guard_tab):
                    candidate_row = repo.get_tab(int(candidate_id), connection)
                    if candidate_row is None:
                        continue
                    candidate_name = str(candidate_row.get("name") or "")
                    fallback_candidates.append(candidate_name)
                    if not should_check_fallback_tab(
                        article_title, article_body, candidate_name
                    ):
                        skipped.append(candidate_name)
                        continue
                    try:
                        candidate_verdict = check_league_membership(
                            article_title,
                            article_body,
                            candidate_name,
                            str(candidate_row.get("ai_league_guard_definition") or ""),
                            llm,
                        )
                    except LLMCallError as exc_fallback:
                        failed.append(f"「{candidate_name}」({exc_fallback})")
                        continue
                    if (
                        candidate_verdict["belongs"] is True
                        and candidate_verdict["confidence"] >= config.league_guard_min_confidence
                    ):
                        fallback_tab_id = int(candidate_row["id"])
                        fallback_tab_name = candidate_name
                        fallback_publish_mode = int(candidate_row.get("publish_mode") or 0)
                        fallback_tab_active = repo.is_active_tab(candidate_row)
                        fallback_verdict = candidate_verdict
                        upgrade_reason = (
                            f"AI 判断本篇不属于「{tab_name}」，但属于「{candidate_name}」"
                            f"（confidence={candidate_verdict['confidence']:.2f}），"
                            f"已归属到「{candidate_name}」"
                            + (
                                "并升级为直接发布"
                                if fallback_tab_active
                                else "（该栏目已停用，只创建草稿）"
                            )
                            + f"：{candidate_verdict['reason']}"
                        )
                        break
                    rejected.append(
                        f"「{candidate_name}」"
                        f"（belongs={candidate_verdict['belongs']}, "
                        f"confidence={candidate_verdict['confidence']:.2f}）"
                    )
                if not upgrade_reason:
                    notes: list[str] = []
                    if rejected:
                        notes.append("也不属于" + "、".join(rejected))
                    if skipped:
                        notes.append(
                            "不含特征词跳过"
                            + "、".join(f"「{name}」" for name in skipped)
                        )
                    if failed:
                        notes.append("级联判断失败" + "、".join(failed))
                    keep_reason = (
                        f"AI 判断本篇不属于「{tab_name or '兜底栏目'}」"
                        f"（belongs={verdict['belongs']}, confidence={verdict['confidence']:.2f}）"
                        + ("，" + "；".join(notes) if notes else "")
                        + f"，维持创建草稿：{verdict['reason']}"
                    )

    # 目标栏目停用时只归档不发布——这是 enabled 的统一语义，压过 reassign 配置和
    # 「确认属于当前栏目」的升级意图。栏目仍然要改对，方便之后栏目恢复或扩展。
    if fallback_tab_id is not None:
        if not fallback_tab_active:
            effective_mode = 0
        elif config.league_guard_reassign_publish_mode == "always_direct":
            effective_mode = 1
        else:
            effective_mode = int(fallback_publish_mode or 0)
    elif upgrade_reason:
        effective_mode = 1 if repo.is_active_tab(guard_tab) else 0
    else:
        effective_mode = int(configured_mode)
    final_tab_id = guard_tab.get("id")
    final_tab_name = tab_name

    # Move the article into the column the AI picked. This rewrites the event
    # column in article_tabs (keeping the generic 「精选」 column); ``channels``
    # are DQD content tags, not columns, and must not be touched here.
    # 改挂与发布在这里是解耦的：目标栏目停用、或 target 模式下目标栏目本就是草稿
    # 配置时，栏目要纠正过来，但文章仍然留在草稿。
    if fallback_tab_id is not None:
        final_tab_id = fallback_tab_id
        final_tab_name = fallback_tab_name or ""
        article = repo.reassign_article_event_tab(
            int(article["id"]),
            fallback_tab_id,
            connection,
        )

    # 归一化只在落库时做一次：展示和聚合读的是同一个字段，各自清洗会让同一赛事
    # 重新分叉成几种写法。原文另存一列，既能回溯模型实际写了什么，也是补别名表
    # 的依据。栏目名一并交给归一化，已有栏目的写法优先。
    raw_competition = (classifier_result or {}).get("actual_competition") or None
    normalized_competition = normalize_competition_name(
        raw_competition, [*fallback_candidates, tab_name]
    )
    guard_record = {
        "tab_id": final_tab_id,
        "tab_name": final_tab_name,
        # 被校验的原栏目。``tab_name`` 记的是最终落点，改挂成功后两者不同，
        # 于是「这篇原本挂在哪」只剩 reason 里的自然语言可查——按栏目统计护栏
        # 效果时会把改挂走的文章算到目标栏目名下。单独留一列结构化的原栏目。
        "guard_tab_id": guard_tab.get("id"),
        "guard_tab_name": tab_name,
        "configured_publish_mode": int(configured_mode),
        "effective_publish_mode": effective_mode,
        "upgraded_to_publish": effective_mode != int(configured_mode),
        "reassigned_tab_id": fallback_tab_id,
        "reassigned_tab_active": fallback_tab_active if fallback_tab_id is not None else None,
        "classifier_used": classifier_result is not None,
        "actual_competition": normalized_competition or None,
        "actual_competition_raw": raw_competition,
        "min_confidence": config.league_guard_min_confidence,
        "verdict": verdict,
        "fallback_verdict": fallback_verdict,
        "fallback_tab_name": fallback_tab_name,
        "fallback_candidates": fallback_candidates,
        "reason": upgrade_reason or keep_reason,
    }
    quality = article.get("quality")
    quality_result = dict(quality) if isinstance(quality, dict) else {}
    if quality_result.get("league_guard") != guard_record:
        quality_result["league_guard"] = guard_record
        # 不传 status：护栏在发布流程中运行，内存里的 article 可能是抢占前的
        # 旧快照，显式回写会把 PUBLISHING/DRAFT_CREATED 覆盖成陈旧状态。
        # save_quality 在 status 为 None 时沿用数据库现值。
        article = repo.save_quality(
            int(article["id"]),
            quality_result,
            connection,
        )
    if upgrade_reason:
        repo.add_article_event(
            int(article["id"]),
            "LEAGUE_GUARD_UPGRADED",
            connection,
            message=upgrade_reason,
            payload=guard_record,
            from_status=str(article.get("status") or "READY_TO_PUBLISH"),
            to_status=str(article.get("status") or "READY_TO_PUBLISH"),
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
                effective_mode, article = _apply_ai_league_guard(
                    article, mode, tabs, connection, config
                )
                effective_mode, article = _apply_language_publish_guard(
                    article, effective_mode, connection
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
        effective_mode, article = _apply_ai_league_guard(
            article, source_override, tabs, connection, config
        )
        effective_mode, article = _apply_language_publish_guard(
            article, effective_mode, connection
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
    effective_mode, article = _apply_ai_league_guard(
        article, mode, tabs, connection, config
    )
    effective_mode, article = _apply_language_publish_guard(
        article, effective_mode, connection
    )
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
        repo.transition_status_if_current(
            article_id,
            "SOURCE_DUPLICATE",
            connection,
            allowed_from=allowed_statuses,
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
        label = current.get("status_label") or current.get("status") or "未知"
        if current.get("status") in _DOWNSTREAM_CLAIMED_STATUSES:
            # 批量发布轮次会先列出待发文章，再逐篇处理；标题查重要调大模型，期间
            # 另一个轮次可能已经把同一篇推进到提交中或已发布。这是无害竞态，必须
            # 走 skipped 通道，否则会被计成 failed 并打 ERROR，把真故障淹没掉。
            raise DraftClaimSkipped(f"当前状态为 {label}，已由其他发布轮次处理")
        raise ValueError(f"当前状态为 {label}，不能创建草稿")

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
        blocked_message = _missing_tab_message(current)
        if block_if_missing_tab:
            repo.transition_status_if_current(
                article_id,
                "MAPPING_BLOCKED",
                connection,
                allowed_from=allowed_statuses,
                event_type=blocked_event_type,
                message=blocked_message,
                payload={"retry": retry, "source": current.get("source")},
            )
        raise ValueError(blocked_message)

    # 先抢占再解析发布模式：AI 归属护栏会在 _resolve_publish_mode 里调用大模型，
    # 若抢占留在提交前，抓取轮末尾的发布与独立发布 worker 会同时跑同一篇，各自
    # 调用一次大模型并可能得到相反结论，先落地的那一次决定最终状态，后落地的
    # 那一次只会覆盖 quality_json，导致「已升级直发」与「草稿已创建」并存。
    claimed = repo.transition_status_if_current(
        article_id,
        "PUBLISHING",
        connection,
        allowed_from=allowed_statuses,
        event_type="PUBLISH_CLAIMED",
        message="已锁定发布任务，开始解析发布模式",
        payload={"retry": retry, "source": current.get("source")},
    )
    if claimed is None:
        refreshed = repo.get_article(article_id, connection)
        if refreshed is not None and int(refreshed.get("dqd_archive_id") or 0) > 0:
            return _existing_draft_result(refreshed, retry=retry)
        raise DraftClaimSkipped(
            f"当前状态为 {refreshed.get('status_label') if refreshed else current.get('status_label') or current.get('status') or '未知'}，已有草稿创建任务正在处理"
        )
    current = claimed

    try:
        publish_mode, current = _resolve_publish_mode(current, tabs, connection, config)
    except PublishModeConflict as exc:
        repo.transition_status_if_current(
            article_id,
            "MAPPING_BLOCKED",
            connection,
            allowed_from={"PUBLISHING"},
            event_type="PUBLISH_MODE_CONFLICT",
            message=str(exc),
            payload={"tabs": tabs, "reason": "publish_mode_conflict", "retry": retry},
        )
        raise

    # 归属护栏的级联兜底会在解析发布模式时把文章改判到兜底栏目，此处必须基于
    # 改判后的文章重新取栏目，否则提交的仍是改判前的旧栏目（AI 判到「日职乙」
    # 却仍发到「日职联」）。改判失败时保留原栏目，不让提交因此落空。
    tabs = _current_tabs(current) or tabs

    # 护栏还会把草稿配置的文章升级成直接发布，而两道标题查重关卡都只认
    # publish_mode==1：质检那道运行时它还是草稿，提交前那道运行在护栏之前，
    # 等升级落定时两道都已经错过。不在这里补一次，同一场比赛的多篇报道会原样
    # 全部直发出去。此刻文章已被抢占为 PUBLISHING，所以终态按该状态做 CAS。
    if publish_mode == 1 and _guard_upgraded_to_publish(current):
        upgrade_dedup = title_dedup.check_title_duplicate(
            config,
            current,
            repo.list_title_dedup_candidates(
                title_dedup.dedup_window_since(config.title_dedup_hours),
                article_id,
                connection,
            ),
            publish_mode=publish_mode,
            body_loader=lambda target_id: repo.get_article_body(target_id, connection),
        )
        if upgrade_dedup["outcome"] == "duplicate":
            matched = upgrade_dedup["matched"] or {}
            blocked_message = (
                f"与文章 #{matched.get('id')}《{matched.get('title')}》标题高度相似"
                f"（{title_dedup.match_scope_label(upgrade_dedup)}），已取消自动发布"
            )
            repo.transition_status_if_current(
                article_id,
                "TITLE_DUPLICATE",
                connection,
                allowed_from={"PUBLISHING"},
                event_type="TITLE_DUPLICATE_DETECTED",
                message=blocked_message,
                payload=upgrade_dedup,
            )
            raise TitleDuplicateBlocked(blocked_message)
        if upgrade_dedup["outcome"] == "needs_review":
            blocked_message = f"标题查重判定失败：{upgrade_dedup['error']}，转人工审核"
            repo.transition_status_if_current(
                article_id,
                "NEEDS_REVIEW",
                connection,
                allowed_from={"PUBLISHING"},
                event_type="TITLE_DUPLICATE_REVIEW",
                message=blocked_message,
                payload=upgrade_dedup,
            )
            raise TitleDuplicateBlocked(blocked_message)
        title_dedup.record_body_confirm_release(article_id, upgrade_dedup, connection)

    try:
        publish_account, current = _publish_account_for_attempt(current, connection)
    except ValueError as exc:
        repo.transition_status_if_current(
            article_id,
            "PUBLISH_FAILED",
            connection,
            allowed_from={"PUBLISHING"},
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

    # 状态已是 PUBLISHING，这里不再改变状态，只为保留带 publish_mode 的开始事件。
    # 仍走 CAS：若期间被超时恢复抢走，返回 None 并按已有任务处理。
    claimed = repo.transition_status_if_current(
        article_id,
        "PUBLISHING",
        connection,
        allowed_from={"PUBLISHING"},
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
        draft = _submit_article(
            client,
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

        # A "重复请求" (duplicate) signal, or any error raised after a draft was
        # already persisted, means the draft exists upstream. Never overwrite a
        # good archive_id with PUBLISH_FAILED; recover the success state instead.
        duplicate_request = bool(getattr(exc, "duplicate_request", False))
        existing = repo.get_article(article_id, connection)
        existing_archive_id = int((existing or {}).get("dqd_archive_id") or 0)
        if existing_archive_id > 0:
            recovered_status = _success_status(publish_mode)
            repo.transition_status_if_current(
                article_id,
                recovered_status,
                connection,
                allowed_from={"PUBLISHING"},
                event_type="PUBLISHED" if publish_mode else success_event_type,
                message=(
                    f"检测到已创建草稿，忽略重复请求，archive_id={existing_archive_id}"
                    if duplicate_request
                    else f"提交返回错误但草稿已存在，保留成功状态，archive_id={existing_archive_id}"
                ),
                payload={
                    **payload,
                    "archive_id": existing_archive_id,
                    "draft_url": build_draft_url(existing_archive_id),
                    "duplicate_request": duplicate_request,
                    "result_status": recovered_status,
                },
            )
            return _existing_draft_result(
                existing,
                retry=retry,
                status=recovered_status,
            )
        if duplicate_request:
            # 上游已按自己的去重规则接受过这次提交，但本地没拿到 archive_id。
            # 上游既没有幂等键也没有查询接口，重发有可能被当成新请求而产生第二篇。
            # 这里不重发，直接落人工可见的失败态并写清处置指引。
            payload["duplicate_request"] = True
            repo.transition_status(
                article_id,
                "PUBLISH_FAILED",
                connection,
                from_status="PUBLISHING",
                event_type=failure_event_type,
                message="上游判定为重复请求但本地没有 archive_id，请到懂球帝后台按标题核对是否已有草稿",
                payload={**payload, "retry_mode": "none", "needs_manual_reconcile": True},
            )
            raise

        if exc.result_unknown:
            request_id = _upstream_request_id(exc.diagnostics)
            next_confirm_at, message = _initial_confirmation_schedule(
                config, exc, publish_mode
            )
            if next_confirm_at is None:
                # 不变量：DRAFT_CONFIRMING 必须带确认排期。认领确认任务的查询要求
                # draft_next_confirm_at IS NOT NULL，没有排期就进这个状态等于承诺了
                # 自动确认却永不执行，文章会无声卡死。宁可落人工可见的失败态。
                repo.transition_status(
                    article_id,
                    "PUBLISH_FAILED",
                    connection,
                    from_status="PUBLISHING",
                    event_type=failure_event_type,
                    message=f"{message}：{str(exc)[:200]}",
                    payload={
                        **payload,
                        "retry_mode": "none",
                        "unschedulable_confirmation": True,
                        "needs_manual_reconcile": True,
                    },
                )
            else:
                repo.mark_draft_result_unknown(
                    article_id,
                    connection,
                    request_id=request_id,
                    next_confirm_at=next_confirm_at,
                    message=message,
                    payload={**payload, "retry_mode": "single_502_retry"},
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
        # 请求已经发出但结果未知，上游可能已经建好文章。没有幂等键也没有查询
        # 接口，重发只会制造第二篇，因此直接落人工可见的失败态等人核对，绝不
        # 留在「确认中」——那个状态没有排期就永远不会被认领。
        updated = repo.transition_status_if_current(
            int(article["id"]),
            "PUBLISH_FAILED",
            conn,
            allowed_from={"PUBLISHING"},
            current_updated_at=article.get("updated_at"),
            event_type="PUBLISHING_TIMED_OUT",
            message="创建草稿请求超时，结果不可确认，请到懂球帝后台按标题核对是否已创建",
            payload={
                "reason": "publishing_timeout",
                "retry_mode": "none",
                "needs_manual_reconcile": True,
            },
        )
        if updated is not None:
            timed_out += 1
    return {"recovered": recovered, "timed_out": timed_out, "checked": len(articles)}


def confirm_due_draft_results(
    config: AppConfig,
    connection,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Run the one-time HTTP 502 retry for drafts whose result is unknown.

    上游没有幂等键也没有查询接口，所以这里唯一被允许的动作是「创建草稿」的单次
    502 重试。重试仍不成功就落 PUBLISH_FAILED，绝不开第三次请求。
    """

    if not config.publisher_enabled or not config.dqd_open_502_retry_enabled:
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
            # 与创建草稿路径同理：解析发布模式时护栏可能改判栏目，重新取一次。
            tabs = _current_tabs(claimed) or tabs
            expected_updated_at = claimed.get("updated_at")
            account, claimed = _publish_account_for_attempt(claimed, connection)
            expected_updated_at = claimed.get("updated_at")
            selected_tabs = tabs[0] if len(tabs) == 1 else tabs
            if not tabs:
                raise ValueError("来源尚未绑定后台栏目，无法确认草稿结果")

            draft = _submit_article(
                DqdOpenClient(config),
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
                message=f"HTTP 502 后自动重试{_success_label(publish_mode)}成功，archive_id={draft.archive_id}",
                payload={
                    "diagnostics": draft.diagnostics,
                    "client_request_id": request_key,
                    "publish_mode": publish_mode,
                    "retry_mode": "single_502_retry",
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
            # 第一次 502 可能已经在上游建好文章，这次重试仍未确认结果，绝不
            # 再开第三次请求。落 PUBLISH_FAILED 交人工核对。
            repo.record_draft_confirmation_result(
                article_id,
                connection,
                outcome="PENDING",
                request_id=request_id,
                schedule_confirmation=False,
                expected_updated_at=expected_updated_at,
                claim_token=claim_token,
                message="HTTP 502 后已自动重试一次，结果仍未确认，请到懂球帝后台按标题核对是否已创建",
                payload={
                    **event_payload,
                    "retry_mode": "single_502_retry",
                    "needs_manual_reconcile": True,
                },
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
            repo.record_draft_confirmation_result(
                article_id,
                connection,
                outcome="PENDING",
                schedule_confirmation=False,
                expected_updated_at=expected_updated_at,
                claim_token=claim_token,
                message="HTTP 502 后的单次自动重试异常，已停止继续重试",
                payload={
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "client_request_id": request_key,
                    "needs_manual_reconcile": True,
                },
            )
            pending += 1
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
        if not self.config.publisher_enabled or not self.config.dqd_open_502_retry_enabled:
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
            "title_duplicate_skipped": 0,
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
            "title_duplicate_skipped": 0,
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
    title_duplicate_skipped = 0
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
        # Abandoned articles never reach READY_TO_PUBLISH (they land in a
        # terminal state on ingest), but guard against a stale mode anyway.
        if repo.resolve_article_publish_mode(article_id, connection).get("publish_mode") == 2:
            skipped += 1
            repo.transition_status_if_current(
                article_id,
                "ABANDONED",
                connection,
                allowed_from={"READY_TO_PUBLISH"},
                event_type="ARTICLE_ABANDONED",
                message="命中放弃配置，跳过提交",
            )
            continue
        duplicate_of = repo.get_duplicate_canonical(article_id, connection)
        if duplicate_of is not None:
            skipped += 1
            blocked = repo.transition_status_if_current(
                article_id,
                "SOURCE_DUPLICATE",
                connection,
                allowed_from={"READY_TO_PUBLISH"},
                event_type="SOURCE_DUPLICATE_DETECTED",
                message=f"与本地文章 #{duplicate_of['id']} 的来源文章 ID 相同，已自动拦截",
                payload={"duplicate_of_article_id": int(duplicate_of["id"])},
            )
            if blocked is None:
                continue
            duplicate_skipped += 1
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": f"与本地文章 #{duplicate_of['id']} 的来源文章 ID 相同，已自动拦截",
                "skipped": True,
            })
            continue
        dedup_result = _title_dedup_gate(config, connection, current)
        if dedup_result is not None and dedup_result["outcome"] not in {"duplicate", "needs_review"}:
            title_dedup.record_body_confirm_release(article_id, dedup_result, connection)
        if dedup_result is not None and dedup_result["outcome"] in {"duplicate", "needs_review"}:
            skipped += 1
            # 查重要调用大模型，期间另一个发布轮次可能已经抢占并提交成功。必须
            # 用 CAS 落终态，否则会把线上已发布的文章改写成「已取消自动发布」。
            if dedup_result["outcome"] == "duplicate":
                matched = dedup_result["matched"] or {}
                blocked = repo.transition_status_if_current(
                    article_id,
                    "TITLE_DUPLICATE",
                    connection,
                    allowed_from={"READY_TO_PUBLISH"},
                    event_type="TITLE_DUPLICATE_DETECTED",
                    message=(
                        f"与文章 #{matched.get('id')}《{matched.get('title')}》标题高度相似"
                        f"（{title_dedup.match_scope_label(dedup_result)}），已取消自动发布"
                    ),
                    payload=dedup_result,
                )
                if blocked is None:
                    continue
                title_duplicate_skipped += 1
                error_text = f"与文章 #{matched.get('id')} 标题高度相似，已取消自动发布"
            else:
                blocked = repo.transition_status_if_current(
                    article_id,
                    "NEEDS_REVIEW",
                    connection,
                    allowed_from={"READY_TO_PUBLISH"},
                    event_type="TITLE_DUPLICATE_REVIEW",
                    message=f"标题查重判定失败：{dedup_result['error']}，转人工审核",
                    payload=dedup_result,
                )
                if blocked is None:
                    continue
                error_text = f"标题查重判定失败：{dedup_result['error']}，转人工审核"
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": error_text,
                "skipped": True,
            })
            continue
        if not _current_tabs(current):
            skipped += 1
            mapping_blocked += 1
            blocked_message = _missing_tab_message(current)
            repo.transition_status_if_current(
                article_id,
                "MAPPING_BLOCKED",
                connection,
                allowed_from={"READY_TO_PUBLISH"},
                event_type="DRAFT_CREATE_BLOCKED",
                message=blocked_message,
            )
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": blocked_message,
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
        except TitleDuplicateBlocked as exc:
            # 护栏升级后才查出的重复。拦截是正常结果，不能计成 failed，否则
            # 真故障会被这类稿件淹没。
            skipped += 1
            title_duplicate_skipped += 1
            result_items.append({
                "article_id": article_id,
                "source": current.get("source"),
                "error": str(exc)[:300],
                "skipped": True,
            })
        except DqdOpenClientError as exc:
            if exc.result_unknown:
                # 结果未知已排期自动确认，属于预期分支，用 ERROR 会把真故障淹没。
                confirming += 1
                logger.warning(
                    "创建草稿结果未知，已进入自动确认 article_id=%s: %s", article_id, exc
                )
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
    if title_duplicate_skipped:
        message += f"，标题查重拦截 {title_duplicate_skipped} 篇"
    if published:
        message += f"，直接发布 {published} 篇"
    return {
        "draft_created": draft_created,
        "published": published,
        "confirmed_published": confirmed_published,
        "failed": failed,
        "skipped": skipped,
        "duplicate_skipped": duplicate_skipped,
        "title_duplicate_skipped": title_duplicate_skipped,
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


class PublishController:
    """Drain READY_TO_PUBLISH on a short cadence, decoupled from ingestion.

    ``publish_ready_articles`` already performs stale recovery, due-confirmation
    reconciliation, and draft creation in one pass, so this controller subsumes
    the work of ``DraftConfirmationController`` while also emptying the publish
    queue without waiting for the heavy end-of-``run_once`` publish step.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict[str, Any] | None = None

    def start(self) -> bool:
        # Publishing is the explicit purpose here; when the publisher is off
        # nothing should be created and the scheduler stays quiet, matching the
        # previous draft-confirmation gating.
        if not self.config.publisher_enabled:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(
            target=self._run,
            name="publish-worker",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._running, "last_result": self._last_result}

    def _run(self) -> None:
        connection = _connect(self.config.database_path)
        try:
            result = publish_ready_articles(self.config, connection)
            with self._lock:
                self._last_result = result
        except Exception:  # noqa: BLE001 - keep future publish ticks alive
            logger.exception("独立发布 worker 执行失败")
        finally:
            connection.close()
            with self._lock:
                self._running = False


def create_draft_for_article(
    config: AppConfig,
    connection,
    article_id: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    if not config.publisher_enabled:
        raise ValueError("发布 worker 未启用，无法创建草稿")
    if not config.dqd_open_configured:
        raise ValueError("开放平台 appid/appsecret/enname 未配置，无法创建草稿")
    current = repo.get_article(article_id, connection)
    if current is None:
        raise ValueError("article not found")
    if not _retry_eligible(current):
        raise ValueError(f"当前状态为 {current.get('status_label') or current.get('status') or '未知'}，不能重新创建草稿")
    if not force and repo.draft_needs_manual_reconcile(article_id, connection):
        # 上一次提交结果不可确认，上游可能已经有这篇了。没有幂等键，重发就可能
        # 变成两篇，所以必须先人工核对后再显式确认重试。
        raise ManualReconcileRequired(
            "上一次提交的结果无法确认，上游可能已经创建过这篇文章。"
            "请先到懂球帝后台按标题核对：若已存在请直接关联或放弃，确认不存在后再强制重试。"
        )
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
