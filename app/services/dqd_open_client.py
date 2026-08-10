"""Open-platform client for creating DQD article drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import requests

from ..config import AppConfig
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
    ):
        super().__init__(message)
        self.payload = payload
        self.status_code = status_code
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class DqdOpenDraftResult:
    archive_id: int
    payload: dict[str, Any]
    request_url: str
    form_fields: list[tuple[str, str]]
    diagnostics: dict[str, Any] | None = None


def _has_image(body_html: str) -> bool:
    return "<img" in body_html.lower()


def build_create_article_form(article: Mapping[str, Any], tab: Mapping[str, Any],
                              config: AppConfig) -> list[tuple[str, str]]:
    title = str(article.get("title_final") or article.get("title") or "").strip()
    body = str(article.get("body_html") or article.get("body") or "").strip()
    if not title:
        raise DqdOpenClientError("标题为空，不能创建草稿")
    if not body:
        raise DqdOpenClientError("正文为空，不能创建草稿")
    if not tab.get("backend_tab_id"):
        raise DqdOpenClientError("栏目未绑定后台 tab，不能创建草稿")

    litpic = str(article.get("litpic") or "").strip()
    if not litpic and not _has_image(body):
        raise DqdOpenClientError("正文无图片且 litpic 为空，无法创建草稿")

    fields: list[tuple[str, str]] = [
        ("dqd_enname", config.dqd_open_enname),
        ("title", title),
        ("body", body),
        ("archive_level", config.dqd_open_archive_level),
        ("status", str(config.dqd_open_status)),
        ("tabs[]", str(int(tab["backend_tab_id"]))),
    ]
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


class DqdOpenClient:
    def __init__(self, config: AppConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.open_platform = OpenPlatformClient(config, self.session)

    @property
    def configured(self) -> bool:
        return self.config.dqd_open_configured

    def create_article(self, article: Mapping[str, Any], tab: Mapping[str, Any]) -> DqdOpenDraftResult:
        if not self.configured:
            raise DqdOpenClientError("开放平台尚未配置 appid/appsecret/enname")
        form = build_create_article_form(article, tab, self.config)
        try:
            response, payload, request_url = self.open_platform.post_signed(data=form, require_login=True)
        except OpenPlatformAuthError as exc:
            raise DqdOpenClientError(
                str(exc),
                payload=exc.payload,
                diagnostics=exc.diagnostics,
            ) from exc
        except OpenPlatformConfigError as exc:
            raise DqdOpenClientError(str(exc)) from exc
        except OpenPlatformRequestError as exc:
            raise DqdOpenClientError(
                f"创建草稿请求失败: {str(exc)[:500]}",
                payload=exc.payload,
                status_code=exc.status_code,
                diagnostics=exc.diagnostics,
            ) from exc
        except (requests.RequestException, ValueError, OpenPlatformError) as exc:
            raise DqdOpenClientError(f"创建草稿请求失败: {str(exc)[:500]}") from exc

        if int(payload.get("code", 0)) != 0:
            message = payload.get("message") or payload.get("msg") or "创建草稿接口返回业务错误"
            if int(payload.get("code", 0)) == 10007:
                raise DqdOpenClientError(
                    str(message),
                    payload=payload,
                    status_code=response.status_code,
                    diagnostics={"request_url": request_url, "form_fields": form},
                )
            raise DqdOpenClientError(
                str(message),
                payload=payload,
                status_code=response.status_code,
                diagnostics={"request_url": request_url, "form_fields": form},
            )

        archive_id = _extract_archive_id(payload)
        if archive_id is None:
            raise DqdOpenClientError(
                "创建草稿成功但没有返回 archive_id",
                payload=payload,
                status_code=response.status_code,
                diagnostics={"request_url": request_url, "form_fields": form},
            )

        return DqdOpenDraftResult(
            archive_id=int(archive_id),
            payload=payload,
            request_url=request_url,
            form_fields=[(key, value) for key, value in form],
            diagnostics={"request_url": request_url, "form_fields": form},
        )
