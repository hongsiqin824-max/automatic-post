"""Open-platform client for creating DQD article drafts."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, Mapping

import requests

from ..config import AppConfig
from .article_images import (
    ArticleImageError,
    build_publish_body,
    effective_litpic,
    fallback_litpic_for_tabs,
)
from .dqd_publish_html import DqdPublishHtmlError, sanitize_dqd_publish_html
from .link_sanitizer import remove_clickable_links
from .open_platform import (
    OpenPlatformAuthError,
    OpenPlatformClient,
    OpenPlatformConfigError,
    OpenPlatformError,
    OpenPlatformRequestError,
)


class DqdOpenClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        payload: Any = None,
        status_code: int | None = None,
        diagnostics: dict[str, Any] | None = None,
        result_unknown: bool = False,
    ):
        super().__init__(message)
        self.payload = payload
        self.status_code = status_code
        self.result_unknown = bool(result_unknown)
        self.diagnostics = dict(diagnostics or {})
        self.diagnostics["result_unknown"] = self.result_unknown
        self.diagnostics.setdefault(
            "error_kind", "remote_result_unknown" if self.result_unknown else "request_failed"
        )


@dataclass(frozen=True)
class DqdOpenDraftResult:
    archive_id: int
    payload: dict[str, Any]
    request_url: str
    form_fields: list[tuple[str, str]]
    diagnostics: dict[str, Any] | None = None


def _backend_tab_ids(tabs: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> list[int]:
    values = [tabs] if isinstance(tabs, Mapping) else list(tabs)
    result: list[int] = []
    for tab in values:
        backend_tab_id = tab.get("backend_tab_id")
        if backend_tab_id in (None, ""):
            raise DqdOpenClientError("栏目未绑定后台 tab，不能创建草稿")
        numeric = int(backend_tab_id)
        if numeric not in result:
            result.append(numeric)
    if not result:
        raise DqdOpenClientError("文章未配置栏目，不能创建草稿")
    return result


def _publish_account_fields(publish_account: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    if publish_account is None:
        return []
    if not isinstance(publish_account, Mapping):
        raise DqdOpenClientError("发布账号格式无效")

    raw_user_id = publish_account.get("dqd_user_id")
    if isinstance(raw_user_id, bool):
        raise DqdOpenClientError("发布账号 dqd_user_id 必须为正整数")
    if isinstance(raw_user_id, int):
        user_id = raw_user_id
    elif isinstance(raw_user_id, str) and raw_user_id.strip().isdigit():
        user_id = int(raw_user_id.strip())
    else:
        raise DqdOpenClientError("发布账号 dqd_user_id 必须为正整数")
    if user_id <= 0:
        raise DqdOpenClientError("发布账号 dqd_user_id 必须为正整数")

    raw_user_name = publish_account.get("user_name")
    if not isinstance(raw_user_name, str) or not raw_user_name.strip():
        raise DqdOpenClientError("发布账号 user_name 不能为空")
    return [("user_id", str(user_id)), ("user_name", raw_user_name.strip())]


def build_create_article_form(
    article: Mapping[str, Any],
    tabs: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    config: AppConfig,
    publish_account: Mapping[str, Any] | None = None,
    client_request_id: str | None = None,
    status: int | None = None,
) -> list[tuple[str, str]]:
    title = str(article.get("title_final") or article.get("title") or "").strip()
    body = remove_clickable_links(
        str(article.get("body_html") or article.get("body") or "")
    )
    if not title:
        raise DqdOpenClientError("标题为空，不能创建草稿")
    if not body.strip():
        raise DqdOpenClientError("正文为空，不能创建草稿")
    # Materialize iterables once because the same tab rows are needed for the
    # backend IDs and for selecting the primary fallback cover.
    normalized_tabs = tabs if isinstance(tabs, Mapping) else list(tabs)
    backend_tab_ids = _backend_tab_ids(normalized_tabs)

    publish_status = status if status is not None else config.dqd_open_status
    if isinstance(publish_status, bool):
        raise DqdOpenClientError("懂球帝提交 status 必须是 0（草稿）或 1（直接发布）")
    try:
        publish_status = int(publish_status)
    except (TypeError, ValueError) as exc:
        raise DqdOpenClientError(
            "懂球帝提交 status 必须是 0（草稿）或 1（直接发布）"
        ) from exc
    if publish_status not in {0, 1}:
        raise DqdOpenClientError("懂球帝提交 status 必须是 0（草稿）或 1（直接发布）")

    # The backend list cover is independent from the rich-text body. Prefer
    # the first usable body image for this request, while retaining the
    # original material cover as the fallback and in local storage.
    litpic = effective_litpic(
        article,
        fallback_litpic=fallback_litpic_for_tabs(normalized_tabs),
    )
    try:
        body = build_publish_body(body, litpic)
    except ArticleImageError as exc:
        raise DqdOpenClientError(f"正文图片处理失败：{exc}") from exc
    try:
        body = sanitize_dqd_publish_html(body)
    except DqdPublishHtmlError as exc:
        raise DqdOpenClientError(f"懂球帝正文安全校验失败：{exc}") from exc

    fields: list[tuple[str, str]] = [
        ("dqd_enname", config.dqd_open_enname),
        ("title", title),
        ("body", body),
        ("archive_level", config.dqd_open_archive_level),
        ("status", str(publish_status)),
    ]
    request_id = str(client_request_id or "").strip()
    if request_id and getattr(config, "dqd_open_idempotency_enabled", False):
        field_name = str(
            getattr(config, "dqd_open_idempotency_field", "client_request_id")
            or "client_request_id"
        ).strip()
        if not field_name:
            raise DqdOpenClientError("开放平台幂等请求字段名不能为空")
        fields.append((field_name, request_id))
    fields.extend(_publish_account_fields(publish_account))
    fields.extend(("tabs[]", str(tab_id)) for tab_id in backend_tab_ids)
    channels = article.get("channels") or []
    if channels:
        channel_values = ",".join(str(int(value)) for value in channels)
        fields.append(("channels", channel_values))
    channelsnew = article.get("channelsnew") or []
    if channelsnew:
        channel_values = ",".join(str(int(value)) for value in channelsnew)
        fields.append(("channelsnew", channel_values))
    if litpic:
        fields.append(("litpic", litpic))
    published_at = str(article.get("published_at") or "").strip()
    if published_at:
        fields.append(("published_at", published_at))
    return fields


def _extract_archive_id(payload: Any) -> int | None:
    if isinstance(payload, Mapping):
        for direct_key in ("archive_id", "archiveId", "article_id", "articleId"):
            direct = payload.get(direct_key)
            if direct not in (None, ""):
                try:
                    numeric = int(direct)
                except (TypeError, ValueError):
                    numeric = None
                if numeric is not None and numeric > 0:
                    return numeric
        for key in ("data", "result"):
            nested = payload.get(key)
            found = _extract_archive_id(nested)
            if found is not None:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _extract_archive_id(item)
            if found is not None:
                return found
    return None


def _numeric_code(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _explicitly_failed(payload: Mapping[str, Any]) -> bool:
    code = _numeric_code(payload.get("code"))
    if code is not None and code != 0:
        return True
    success = payload.get("success")
    return success is False or success == 0 or (
        isinstance(success, str) and success.strip().lower() in {"false", "0"}
    )


def _business_failure_message(payload: Any) -> str | None:
    """Return the most specific message from an explicit business failure."""

    if not isinstance(payload, Mapping):
        return None

    child_failures: list[str] = []
    for key in ("data", "result"):
        nested = payload.get(key)
        if isinstance(nested, list):
            values = nested
        else:
            values = [nested]
        for value in values:
            message = _business_failure_message(value)
            if message:
                child_failures.append(message)

    if child_failures:
        return child_failures[0]
    if not _explicitly_failed(payload):
        return None
    return str(
        payload.get("message")
        or payload.get("msg")
        or "创建草稿接口返回业务错误"
    ).strip()


def _business_failure_result_unknown(payload: Any) -> bool:
    """Code 5 means the backend may have failed after starting the write."""

    if isinstance(payload, Mapping):
        code = _numeric_code(payload.get("code"))
        if code == 5:
            return True
        return any(
            _business_failure_result_unknown(payload.get(key))
            for key in ("data", "result")
        )
    if isinstance(payload, list):
        return any(_business_failure_result_unknown(item) for item in payload)
    return False


def _request_diagnostics(
    request_url: str,
    form: list[tuple[str, str]],
    payload: Mapping[str, Any],
    *,
    client_request_id: str | None = None,
    idempotency_field: str | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"request_url": request_url, "form_fields": form}
    request_id = payload.get("request_id")
    if request_id not in (None, ""):
        diagnostics["request_id"] = str(request_id)
    if client_request_id:
        diagnostics["client_request_id"] = client_request_id
    if idempotency_field:
        diagnostics["idempotency_field"] = idempotency_field
    return diagnostics


def _idempotency_context(
    config: AppConfig,
    client_request_id: str | None,
) -> tuple[str | None, str | None]:
    request_id = str(client_request_id or "").strip()
    if not request_id or not getattr(config, "dqd_open_idempotency_enabled", False):
        return None, None
    field_name = str(
        getattr(config, "dqd_open_idempotency_field", "client_request_id")
        or "client_request_id"
    ).strip()
    return request_id, field_name or None


def _enrich_error_diagnostics(
    diagnostics: Mapping[str, Any] | None,
    *,
    payload: Any = None,
    client_request_id: str | None = None,
    idempotency_field: str | None = None,
    exception: BaseException | None = None,
) -> dict[str, Any]:
    result = dict(diagnostics or {})
    if isinstance(payload, Mapping):
        request_id = payload.get("request_id")
        if request_id not in (None, ""):
            result["request_id"] = str(request_id)
    if client_request_id:
        result["client_request_id"] = client_request_id
    if idempotency_field:
        result["idempotency_field"] = idempotency_field
    if exception is not None:
        result["exception_type"] = type(exception).__name__
    return result


def _request_error_result_unknown(error: OpenPlatformRequestError) -> bool:
    if error.payload is None:
        return True
    status_code = error.status_code
    if status_code is None:
        return True
    return status_code >= 500 or 200 <= status_code < 300


class DqdOpenClient:
    def __init__(self, config: AppConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.open_platform = OpenPlatformClient(config, self.session)

    @property
    def configured(self) -> bool:
        return self.config.dqd_open_configured

    def create_article(
        self,
        article: Mapping[str, Any],
        tabs: Mapping[str, Any] | Iterable[Mapping[str, Any]],
        publish_account: Mapping[str, Any] | None = None,
        client_request_id: str | None = None,
        status: int | None = None,
    ) -> DqdOpenDraftResult:
        if not self.configured:
            raise DqdOpenClientError("开放平台尚未配置 appid/appsecret/enname")
        form = build_create_article_form(
            article,
            tabs,
            self.config,
            publish_account,
            client_request_id,
            status,
        )
        submission_label = "直接发布" if dict(form).get("status") == "1" else "创建草稿"
        idempotency_request_id, idempotency_field = _idempotency_context(
            self.config,
            client_request_id,
        )
        try:
            response, payload, request_url = self.open_platform.post_signed(data=form, require_login=True)
        except OpenPlatformAuthError as exc:
            raise DqdOpenClientError(
                str(exc),
                payload=exc.payload,
                diagnostics=_enrich_error_diagnostics(
                    exc.diagnostics,
                    payload=exc.payload,
                    client_request_id=idempotency_request_id,
                    idempotency_field=idempotency_field,
                ),
            ) from exc
        except OpenPlatformConfigError as exc:
            raise DqdOpenClientError(str(exc)) from exc
        except OpenPlatformRequestError as exc:
            result_unknown = _request_error_result_unknown(exc)
            raise DqdOpenClientError(
                f"{submission_label}请求失败: {str(exc)[:500]}",
                payload=exc.payload,
                status_code=exc.status_code,
                diagnostics=_enrich_error_diagnostics(
                    exc.diagnostics,
                    payload=exc.payload,
                    client_request_id=idempotency_request_id,
                    idempotency_field=idempotency_field,
                ),
                result_unknown=result_unknown,
            ) from exc
        except requests.RequestException as exc:
            raise DqdOpenClientError(
                f"{submission_label}请求失败: {str(exc)[:500]}",
                diagnostics=_enrich_error_diagnostics(
                    None,
                    client_request_id=idempotency_request_id,
                    idempotency_field=idempotency_field,
                    exception=exc,
                ),
                result_unknown=True,
            ) from exc
        except (ValueError, OpenPlatformError) as exc:
            raise DqdOpenClientError(
                f"{submission_label}请求失败: {str(exc)[:500]}",
                diagnostics=_enrich_error_diagnostics(
                    None,
                    client_request_id=idempotency_request_id,
                    idempotency_field=idempotency_field,
                    exception=exc,
                ),
            ) from exc

        diagnostics = _request_diagnostics(
            request_url,
            form,
            payload,
            client_request_id=idempotency_request_id,
            idempotency_field=idempotency_field,
        )
        failure_message = _business_failure_message(payload)
        if failure_message:
            raise DqdOpenClientError(
                f"懂球帝{submission_label}失败：{failure_message}",
                payload=payload,
                status_code=response.status_code,
                diagnostics=diagnostics,
                result_unknown=_business_failure_result_unknown(payload),
            )

        archive_id = _extract_archive_id(payload)
        if archive_id is None:
            raise DqdOpenClientError(
                f"{submission_label}成功但没有返回 archive_id",
                payload=payload,
                status_code=response.status_code,
                diagnostics=diagnostics,
                result_unknown=True,
            )

        return DqdOpenDraftResult(
            archive_id=int(archive_id),
            payload=payload,
            request_url=request_url,
            form_fields=[(key, value) for key, value in form],
            diagnostics=diagnostics,
        )
