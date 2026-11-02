"""飞书 OAuth 登录：拼授权地址、用 code 换用户信息。

文档:
- 授权码: https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code
- 换 token(v3): https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token-v3
- 用户信息: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


class FeishuLoginError(Exception):
    """飞书登录失败，message 可直接展示给用户。"""


def build_authorize_url(app_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode({"client_id": app_id, "redirect_uri": redirect_uri, "state": state})
    return f"{AUTHORIZE_URL}?{query}"


def fetch_login_user(
    app_id: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
    timeout: int = 10,
) -> dict[str, str]:
    """用授权码换 user_access_token，再取用户基本信息。"""

    try:
        token_resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=timeout,
        )
        token_data: dict[str, Any] = token_resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise FeishuLoginError(f"请求飞书失败：{exc}") from exc
    access_token = token_data.get("access_token")
    if token_data.get("code") != 0 or not access_token:
        detail = token_data.get("error_description") or token_data.get("error") or ""
        raise FeishuLoginError(f"飞书授权失败（{token_data.get('code')}）：{detail}")

    try:
        info_resp = requests.get(
            USER_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        info_data: dict[str, Any] = info_resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise FeishuLoginError(f"获取飞书用户信息失败：{exc}") from exc
    user = info_data.get("data") or {}
    if info_data.get("code") != 0 or not user.get("open_id"):
        raise FeishuLoginError(f"获取飞书用户信息失败（{info_data.get('code')}）：{info_data.get('msg', '')}")

    return {
        "open_id": str(user.get("open_id", "")),
        "name": str(user.get("name", "")),
        "tenant_key": str(user.get("tenant_key", "")),
    }
