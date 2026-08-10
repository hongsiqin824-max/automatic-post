"""Open-platform signing and OAuth helpers for the DQD workflow."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .. import db
from ..config import AppConfig


logger = logging.getLogger(__name__)

OPEN_AUTHORIZE_URL = "https://platform.dongqiudi.com/open/oauth/authorize"
OPEN_TOKEN_URL = "https://platform.dongqiudi.com/open/oauth/token"
OPEN_TOKEN_REFRESH_URL = "https://platform.dongqiudi.com/open/oauth/token/refresh"
DEFAULT_DQD_DRAFT_URL_TEMPLATE = "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId={archive_id}"

AUTH_STATUS_LABELS = {
    "UNAUTHORIZED": "未授权",
    "AUTHORIZING": "授权中",
    "AUTHORIZED": "已授权",
    "REFRESHING": "刷新中",
    "EXPIRED": "已过期",
    "ERROR": "异常",
}


class OpenPlatformError(RuntimeError):
    """Base error for open platform auth or signing failures."""


class OpenPlatformConfigError(OpenPlatformError):
    """Raised when the app cannot build a valid open-platform request."""


class OpenPlatformAuthError(OpenPlatformError):
    """Raised when user interaction or re-authorization is needed."""

    def __init__(
        self,
        message: str,
        *,
        status: str = "AUTH_REQUIRED",
        authorize_url: str | None = None,
        payload: Any = None,
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.authorize_url = authorize_url
        self.payload = payload
        self.diagnostics = diagnostics or {}


class OpenPlatformRequestError(OpenPlatformError):
    """Raised for non-auth transport or business failures."""

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


def build_draft_url(archive_id: Any, template: str = DEFAULT_DQD_DRAFT_URL_TEMPLATE) -> str:
    try:
        numeric_archive_id = int(archive_id)
    except (TypeError, ValueError):
        return ""
    if numeric_archive_id <= 0:
        return ""
    return template.format(archive_id=numeric_archive_id)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(seconds: int | float | None) -> str | None:
    if seconds in (None, "", 0):
        return None
    try:
        delta = float(seconds)
    except (TypeError, ValueError):
        return None
    return (_utc_now() + timedelta(seconds=delta)).isoformat(timespec="seconds")


def _api_name_from_url(url: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    api_name = str(query.get("api_name") or "").strip()
    if not api_name:
        raise OpenPlatformConfigError("DQD_OPEN_BASE_URL 缺少 api_name")
    return api_name


def _base_request_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise OpenPlatformConfigError("DQD_OPEN_BASE_URL 格式无效")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))


def build_authorize_url(settings: AppConfig, state: str | None = None) -> tuple[str, str]:
    if not settings.dqd_open_appid:
        raise OpenPlatformConfigError("DQD_OPEN_APPID 未配置")
    api_name = settings.dqd_open_api_name or _api_name_from_url(settings.dqd_open_base_url)
    current_state = state or secrets.token_urlsafe(16)
    query = urlencode(
        [
            ("appid", settings.dqd_open_appid),
            ("api_name", api_name),
            ("redirect_uri", settings.dqd_open_redirect_uri),
            ("state", current_state),
        ]
    )
    return f"{OPEN_AUTHORIZE_URL}?{query}", current_state


def sign_query_params(params: list[tuple[str, str]], appsecret: str) -> str:
    canonical = sorted((str(key), str(value)) for key, value in params if key != "sign")
    raw = "&".join(f"{key}={value}" for key, value in canonical)
    return hmac.new(appsecret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def build_signed_request_url(settings: AppConfig, *, extra_query: dict[str, str] | None = None) -> str:
    if not settings.dqd_open_appid:
        raise OpenPlatformConfigError("DQD_OPEN_APPID 未配置")
    if not settings.dqd_open_appsecret:
        raise OpenPlatformConfigError("DQD_OPEN_APPSECRET 未配置")
    parsed = urlsplit(_base_request_url(settings.dqd_open_base_url))
    query_pairs: list[tuple[str, str]] = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    query_pairs.extend(
        [
            ("api_name", settings.dqd_open_api_name),
            ("appid", settings.dqd_open_appid),
            ("timestamp", str(int(time.time()))),
            ("nonce", secrets.token_hex(16)),
        ]
    )
    if extra_query:
        query_pairs.extend((str(key), str(value)) for key, value in extra_query.items())
    sign = sign_query_params(query_pairs, settings.dqd_open_appsecret)
    query_pairs.append(("sign", sign))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_pairs, doseq=True), parsed.fragment))


def _normalise_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    user_info = data.get("user_info")
    if not isinstance(user_info, dict):
        user_info = {}
    return {
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "token_type": str(data.get("token_type") or "Bearer").strip() or "Bearer",
        "expires_in": data.get("expires_in"),
        "user_info": user_info,
        "raw": payload,
    }


def _response_payload(response: requests.Response) -> tuple[dict[str, Any] | None, str]:
    text = response.text if isinstance(response.text, str) else ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return payload if isinstance(payload, dict) else None, text


def _response_diagnostics(
    *,
    request_url: str,
    request_headers: dict[str, str],
    request_fields: list[tuple[str, str]],
    response: requests.Response,
    payload: dict[str, Any] | None,
    response_text: str,
) -> dict[str, Any]:
    safe_headers = {
        key: ("[redacted]" if key.lower() == "authorization" else value)
        for key, value in request_headers.items()
    }
    return {
        "request_url": request_url,
        "request_headers": safe_headers,
        "request_fields": [{"key": key, "value": value} for key, value in request_fields],
        "status_code": response.status_code,
        "response_url": getattr(response, "url", request_url),
        "response_text": response_text[:4000],
        "response_json": payload or {},
    }


def auth_record_summary(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {
            "configured": False,
            "auth_status": "UNAUTHORIZED",
            "auth_status_label": AUTH_STATUS_LABELS["UNAUTHORIZED"],
            "has_access_token": False,
            "has_refresh_token": False,
            "pending_state": "",
            "pending_state_expires_at": None,
            "token_expires_at": None,
            "refresh_token_expires_at": None,
            "expires_in_seconds": None,
            "refresh_expires_in_seconds": None,
            "authorized_user": {},
            "last_error": "",
            "last_authorize_url": "",
        }
    token_expires_at = _parse_iso(record.get("token_expires_at"))
    refresh_expires_at = _parse_iso(record.get("refresh_token_expires_at"))
    now = _utc_now()
    expires_in_seconds = int((token_expires_at - now).total_seconds()) if token_expires_at else None
    refresh_expires_in_seconds = int((refresh_expires_at - now).total_seconds()) if refresh_expires_at else None
    auth_status = record.get("auth_status") or "UNAUTHORIZED"
    return {
        "configured": True,
        "auth_status": auth_status,
        "auth_status_label": AUTH_STATUS_LABELS.get(auth_status, auth_status),
        "has_access_token": bool(record.get("access_token")),
        "has_refresh_token": bool(record.get("refresh_token")),
        "pending_state": record.get("pending_state") or "",
        "pending_state_expires_at": record.get("pending_state_expires_at"),
        "token_expires_at": record.get("token_expires_at"),
        "refresh_token_expires_at": record.get("refresh_token_expires_at"),
        "expires_in_seconds": expires_in_seconds,
        "refresh_expires_in_seconds": refresh_expires_in_seconds,
        "authorized_user": record.get("authorized_user") or {},
        "last_error": record.get("last_error") or "",
        "last_authorize_url": record.get("last_authorize_url") or "",
    }


class OpenPlatformClient:
    def __init__(self, settings: AppConfig, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    def summarize(self) -> dict[str, Any]:
        return auth_record_summary(db.get_open_platform_auth(self.settings.database_path))

    def start_authorization(self) -> dict[str, Any]:
        authorize_url, state = build_authorize_url(self.settings)
        db.update_open_platform_auth(
            self.settings.database_path,
            auth_status="AUTHORIZING",
            pending_state=state,
            pending_state_expires_at=_expires_at(900),
            last_authorize_url=authorize_url,
            last_error="",
        )
        return {"authorize_url": authorize_url, "state": state, "redirect_uri": self.settings.dqd_open_redirect_uri}

    def handle_callback(self, code: str, state: str | None = None) -> dict[str, Any]:
        record = db.get_open_platform_auth(self.settings.database_path)
        if record is None:
            raise OpenPlatformAuthError("授权记录不存在", status="AUTH_REQUIRED")
        pending_state = str(record.get("pending_state") or "").strip()
        pending_state_expires_at = _parse_iso(record.get("pending_state_expires_at"))
        if not pending_state or not state or pending_state != state:
            db.update_open_platform_auth(self.settings.database_path, auth_status="ERROR", last_error="state 校验失败")
            raise OpenPlatformAuthError("state 校验失败，请重新发起授权", status="AUTH_REQUIRED")
        if pending_state_expires_at and pending_state_expires_at < _utc_now():
            db.update_open_platform_auth(self.settings.database_path, auth_status="ERROR", last_error="授权 state 已过期")
            raise OpenPlatformAuthError("授权 state 已过期，请重新发起授权", status="AUTH_REQUIRED")
        if not self.settings.dqd_open_appid or not self.settings.dqd_open_appsecret:
            raise OpenPlatformConfigError("DQD_OPEN_APPID / DQD_OPEN_APPSECRET 未配置")
        response = self.session.post(
            OPEN_TOKEN_URL,
            json={
                "appid": self.settings.dqd_open_appid,
                "app_secret": self.settings.dqd_open_appsecret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=self.settings.dqd_open_timeout,
        )
        payload, text = _response_payload(response)
        diagnostics = _response_diagnostics(
            request_url=OPEN_TOKEN_URL,
            request_headers={"Accept": "application/json"},
            request_fields=[
                ("appid", self.settings.dqd_open_appid),
                ("app_secret", "[redacted]"),
                ("code", code),
                ("grant_type", "authorization_code"),
            ],
            response=response,
            payload=payload,
            response_text=text,
        )
        if payload is None:
            raise OpenPlatformRequestError("换取 access_token 失败: 返回不是 JSON", status_code=response.status_code, diagnostics=diagnostics)
        if response.status_code >= 400:
            message = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
            raise OpenPlatformRequestError(f"换取 access_token 失败: {message}", payload=payload, status_code=response.status_code, diagnostics=diagnostics)
        if int(payload.get("code", 0)) != 0:
            message = payload.get("message") or payload.get("msg") or "换取 access_token 失败"
            db.update_open_platform_auth(self.settings.database_path, auth_status="ERROR", last_error=str(message))
            raise OpenPlatformAuthError(str(message), status="AUTH_REQUIRED", payload=payload, diagnostics=diagnostics)
        token = _normalise_token_response(payload)
        expires_at = _expires_at(token.get("expires_in") or 7200)
        refresh_expires_at = _expires_at(7 * 24 * 3600)
        db.update_open_platform_auth(
            self.settings.database_path,
            auth_status="AUTHORIZED",
            pending_state="",
            pending_state_expires_at=None,
            access_token=token["access_token"],
            refresh_token=token["refresh_token"],
            token_type=token["token_type"],
            token_expires_at=expires_at,
            refresh_token_expires_at=refresh_expires_at,
            authorized_user=token["user_info"],
            last_error="",
        )
        return db.get_open_platform_auth(self.settings.database_path) or {}

    def refresh_access_token(self) -> str:
        record = db.get_open_platform_auth(self.settings.database_path)
        if record is None:
            raise OpenPlatformAuthError("授权记录不存在", status="AUTH_REQUIRED")
        refresh_token = str(record.get("refresh_token") or "").strip()
        if not refresh_token:
            raise OpenPlatformAuthError(
                "尚未完成授权，请先授权",
                status="AUTH_REQUIRED",
                authorize_url=self.start_authorization()["authorize_url"],
            )
        if not self.settings.dqd_open_appid or not self.settings.dqd_open_appsecret:
            raise OpenPlatformConfigError("DQD_OPEN_APPID / DQD_OPEN_APPSECRET 未配置")
        db.update_open_platform_auth(self.settings.database_path, auth_status="REFRESHING", last_error="")
        response = self.session.post(
            OPEN_TOKEN_REFRESH_URL,
            json={"refresh_token": refresh_token},
            timeout=self.settings.dqd_open_timeout,
        )
        payload, text = _response_payload(response)
        diagnostics = _response_diagnostics(
            request_url=OPEN_TOKEN_REFRESH_URL,
            request_headers={"Accept": "application/json"},
            request_fields=[("refresh_token", "[redacted]")],
            response=response,
            payload=payload,
            response_text=text,
        )
        if payload is None:
            db.update_open_platform_auth(self.settings.database_path, auth_status="EXPIRED", last_error=f"HTTP {response.status_code}")
            raise OpenPlatformRequestError("刷新 access_token 失败: 返回不是 JSON", status_code=response.status_code, diagnostics=diagnostics)
        if response.status_code in {401, 403}:
            # 真正的鉴权失败，需要重新授权
            message = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
            db.update_open_platform_auth(self.settings.database_path, auth_status="EXPIRED", last_error=str(message))
            raise OpenPlatformAuthError(f"刷新 access_token 失败: {message}", status="AUTH_EXPIRED", payload=payload, diagnostics=diagnostics)
        if response.status_code >= 400:
            # 限流、服务错误等暂时性问题，不重置为 EXPIRED
            message = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
            db.update_open_platform_auth(self.settings.database_path, auth_status="ERROR", last_error=str(message))
            raise OpenPlatformRequestError(f"刷新 access_token 失败: {message}", status_code=response.status_code, payload=payload, diagnostics=diagnostics)
        if int(payload.get("code", 0)) != 0:
            message = payload.get("message") or payload.get("msg") or "刷新 access_token 失败"
            db.update_open_platform_auth(self.settings.database_path, auth_status="EXPIRED", last_error=str(message))
            raise OpenPlatformAuthError(
                str(message),
                status="AUTH_EXPIRED",
                payload=payload,
                authorize_url=self.start_authorization()["authorize_url"],
                diagnostics=diagnostics,
            )
        token = _normalise_token_response(payload)
        expires_at = _expires_at(token.get("expires_in") or 7200)
        refresh_expires_at = _expires_at(7 * 24 * 3600)
        db.update_open_platform_auth(
            self.settings.database_path,
            auth_status="AUTHORIZED",
            access_token=token["access_token"],
            refresh_token=token["refresh_token"] or refresh_token,
            token_type=token["token_type"],
            token_expires_at=expires_at,
            refresh_token_expires_at=refresh_expires_at,
            authorized_user=token["user_info"],
            last_error="",
        )
        return token["access_token"]

    def ensure_access_token(self, *, force_refresh: bool = False) -> str:
        record = db.get_open_platform_auth(self.settings.database_path)
        if record is None:
            raise OpenPlatformAuthError(
                "尚未授权，请先完成开放平台登录",
                status="AUTH_REQUIRED",
                authorize_url=self.start_authorization()["authorize_url"],
            )
        access_token = str(record.get("access_token") or "").strip()
        refresh_token = str(record.get("refresh_token") or "").strip()
        token_expires_at = _parse_iso(record.get("token_expires_at"))
        refresh_expires_at = _parse_iso(record.get("refresh_token_expires_at"))
        now = _utc_now()
        if access_token and not force_refresh and token_expires_at and token_expires_at - now > timedelta(minutes=5):
            return access_token
        if refresh_token and (refresh_expires_at is None or refresh_expires_at > now):
            return self.refresh_access_token()
        if access_token and token_expires_at and token_expires_at > now and not force_refresh:
            return access_token
        raise OpenPlatformAuthError(
            "开放平台授权已过期，请重新登录授权",
            status="AUTH_EXPIRED",
            authorize_url=self.start_authorization()["authorize_url"],
        )

    def post_signed(
        self,
        *,
        data: list[tuple[str, str]],
        require_login: bool = True,
        extra_query: dict[str, str] | None = None,
    ) -> tuple[requests.Response, dict[str, Any], str]:
        url = build_signed_request_url(self.settings, extra_query=extra_query)
        raw_headers = getattr(self.settings, "dqd_headers", {}) or {}
        if not isinstance(raw_headers, dict):
            raw_headers = {}
        headers = {str(key): str(value) for key, value in raw_headers.items()}
        if require_login:
            token = self.ensure_access_token()
            headers["Authorization"] = f"Bearer {token}"

        response = self.session.post(url, data=data, headers=headers, timeout=self.settings.dqd_open_timeout)
        payload, text = _response_payload(response)
        diagnostics = _response_diagnostics(
            request_url=url,
            request_headers=headers,
            request_fields=data,
            response=response,
            payload=payload,
            response_text=text,
        )
        if response.status_code in {401, 403} and require_login:
            logger.warning("Open platform returned %s for article create; retrying with refreshed token", response.status_code)
            try:
                token = self.ensure_access_token(force_refresh=True)
            except OpenPlatformError as exc:
                raise OpenPlatformAuthError(
                    f"创建文章接口返回 HTTP {response.status_code}，需要重新授权",
                    status="AUTH_EXPIRED",
                    authorize_url=getattr(exc, "authorize_url", None) or self.start_authorization()["authorize_url"],
                    payload=payload,
                    diagnostics=diagnostics,
                ) from exc
            headers["Authorization"] = f"Bearer {token}"
            response = self.session.post(url, data=data, headers=headers, timeout=self.settings.dqd_open_timeout)
            payload, text = _response_payload(response)
            diagnostics = _response_diagnostics(
                request_url=url,
                request_headers=headers,
                request_fields=data,
                response=response,
                payload=payload,
                response_text=text,
            )
            if response.status_code in {401, 403}:
                raise OpenPlatformAuthError(
                    f"创建文章接口返回 HTTP {response.status_code}，需要重新授权",
                    status="AUTH_EXPIRED",
                    authorize_url=self.start_authorization()["authorize_url"],
                    payload=payload,
                    diagnostics=diagnostics,
                )

        if payload is None:
            raise OpenPlatformRequestError(
                "创建文章接口返回不是 JSON",
                status_code=response.status_code,
                diagnostics=diagnostics,
            )
        if response.status_code >= 400:
            message = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
            raise OpenPlatformRequestError(
                f"创建文章接口返回 HTTP {response.status_code}: {message}",
                payload=payload,
                status_code=response.status_code,
                diagnostics=diagnostics,
            )
        if self._needs_login(response, payload) and require_login:
            token = self.ensure_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            response = self.session.post(url, data=data, headers=headers, timeout=self.settings.dqd_open_timeout)
            payload, text = _response_payload(response)
            diagnostics = _response_diagnostics(
                request_url=url,
                request_headers=headers,
                request_fields=data,
                response=response,
                payload=payload,
                response_text=text,
            )
            if payload is None:
                raise OpenPlatformRequestError(
                    "创建文章接口返回不是 JSON",
                    status_code=response.status_code,
                    diagnostics=diagnostics,
                )
            if self._needs_login(response, payload):
                raise OpenPlatformAuthError(
                    "接口仍然提示需要登录，请重新授权",
                    status="AUTH_EXPIRED",
                    payload=payload,
                    authorize_url=self.start_authorization()["authorize_url"],
                    diagnostics=diagnostics,
                )
            if response.status_code >= 400:
                message = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
                raise OpenPlatformRequestError(
                    f"创建文章接口返回 HTTP {response.status_code}: {message}",
                    payload=payload,
                    status_code=response.status_code,
                    diagnostics=diagnostics,
                )

        if not isinstance(payload, dict):
            raise OpenPlatformRequestError(
                "创建文章接口返回不是 JSON 对象",
                status_code=response.status_code,
                diagnostics=diagnostics,
            )
        return response, payload, url

    @staticmethod
    def _needs_login(response: requests.Response, payload: dict[str, Any]) -> bool:
        if response.status_code in {401, 403}:
            return True
        if not isinstance(payload, dict):
            return False
        code = payload.get("code")
        try:
            return int(code) == 10007
        except (TypeError, ValueError):
            return False
