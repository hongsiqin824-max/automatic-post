"""Client for the translated-materials API described in the PDF contract."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

import requests

from .channel_filter import filter_blocked_channels

logger = logging.getLogger(__name__)


class MaterialClientError(RuntimeError):
    """An upstream API error with a user-readable message."""


@dataclass
class MaterialFetchResult:
    items: list[dict[str, Any]]
    total: int
    pages: int


def cdn_url(path: str | None) -> str:
    """Expand the API's relative image paths for browser display."""
    value = str(path or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urljoin("https://img1.qunliao.info", "/" + value.lstrip("/"))


def _as_channels(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MaterialClientError("channels 不是数组")
    result: list[int] = []
    seen: set[int] = set()
    for raw in value:
        try:
            channel_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise MaterialClientError(f"channels 包含无效 ID: {raw!r}") from exc
        if channel_id not in seen:
            seen.add(channel_id)
            result.append(channel_id)
    return filter_blocked_channels(result)


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert one upstream item into the local article shape."""
    title = str(item.get("translate_title") or "").strip()
    body = str(item.get("translate_body") or "").strip()
    source = str(item.get("source") or "").strip()
    source_url = str(item.get("source_url") or "").strip()
    if not title or not body or not source or not source_url:
        raise MaterialClientError("素材缺少 title/body/source/source_url 必填值")
    try:
        upstream_archive_id = int(item.get("archive_id") or 0)
    except (TypeError, ValueError) as exc:
        raise MaterialClientError("archive_id 不是整数") from exc
    return {
        "source": source,
        "source_url": source_url,
        # Keep the material marker separate from publish-account user_name.
        "material_user_name": str(
            item.get("user_name") or item.get("username") or ""
        ).strip(),
        "title_original": title,
        "title_current": title,
        "body_original": body,
        "body_current": body,
        "litpic": str(item.get("dqd_litpic") or "").strip(),
        "litpic_display": cdn_url(item.get("dqd_litpic")),
        "channels": _as_channels(item.get("channels") or []),
        "upstream_archive_id": upstream_archive_id,
        "raw_payload": item,
    }


class MaterialClient:
    path = "/v1/url_ingest/ai_materials"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        caller: str,
        timeout: int = 20,
        session: requests.Session | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.caller = caller
        self.timeout = timeout
        self.session = session or requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.session.headers.update({
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "automatic-post/1.0",
        })

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.caller)

    def fetch_page(
        self,
        sources: Iterable[str],
        *,
        hours: int = 24,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        source_list = [str(s).strip() for s in sources if str(s).strip()]
        if not source_list:
            return {"items": [], "total": 0, "has_more": False}
        if not self.configured:
            raise MaterialClientError("素材接口尚未配置 MATERIAL_API_KEY 或 MATERIAL_API_CALLER")
        params = {
            "source": ",".join(source_list),
            "caller": self.caller,
            "hours": max(1, min(24, int(hours))),
            "limit": max(1, min(500, int(limit))),
            "offset": max(0, int(offset)),
        }
        url = f"{self.base_url}{self.path}"
        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise MaterialClientError(f"素材接口网络错误: {exc}") from exc
                time.sleep(self.backoff_seconds * (2 ** attempt))
                continue
            if response.status_code != 429:
                break
            if attempt >= self.max_retries:
                raise MaterialClientError("素材接口限频（429），重试次数已用尽")
            time.sleep(self.backoff_seconds * (2 ** attempt))
        if response is None:  # pragma: no cover - defensive guard
            raise MaterialClientError("素材接口没有返回响应")
        if response.status_code in {401, 403}:
            raise MaterialClientError(
                f"素材接口鉴权失败（{response.status_code}），请检查 SK 与 caller"
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            raise MaterialClientError(f"素材接口错误 HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MaterialClientError("素材接口返回的不是 JSON") from exc
        if payload.get("code") not in (0, None):
            raise MaterialClientError(str(payload.get("msg") or payload))
        data = payload.get("data") or {}
        items = data.get("items") or []
        if not isinstance(items, list):
            raise MaterialClientError("素材接口 data.items 不是数组")
        return {
            "items": items,
            "total": int(data.get("total") or 0),
            "has_more": bool(data.get("has_more")),
        }

    def fetch_all(
        self,
        sources: Iterable[str],
        *,
        hours: int = 24,
        limit: int = 500,
        max_pages: int = 100,
    ) -> MaterialFetchResult:
        items: list[dict[str, Any]] = []
        offset = 0
        total = 0
        pages = 0
        for _ in range(max_pages):
            page = self.fetch_page(
                sources,
                hours=hours,
                limit=limit,
                offset=offset,
            )
            pages += 1
            batch = page["items"]
            items.extend(batch)
            total = max(total, page["total"])
            offset += len(batch)
            if not batch or (not page["has_more"] and offset >= total):
                break
            # A short pause prevents a tight loop from tripping caller limits.
            time.sleep(0.1)
        return MaterialFetchResult(items=items, total=total, pages=pages)
