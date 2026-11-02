from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest

from app.services import feishu_login

REDIRECT_URI = "https://automatic.example.com/auth/feishu/callback"


@pytest.fixture()
def login_app(app):
    cfg = app.extensions["app_config"]
    app.extensions["app_config"] = replace(
        cfg,
        feishu_login_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
        feishu_login_redirect_uri=REDIRECT_URI,
        feishu_login_tenant_key="tenant_ours",
    )
    return app


@pytest.fixture()
def login_client(login_app):
    return login_app.test_client()


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _stub_feishu(monkeypatch, tenant_key="tenant_ours", token_code=0):
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["token"] = (url, data)
        if token_code != 0:
            return _Resp({"code": token_code, "error": "invalid_grant", "error_description": "code expired"})
        return _Resp({"code": 0, "access_token": "u-token"})

    def fake_get(url, headers=None, timeout=None):
        calls["user_info"] = (url, headers)
        return _Resp({"code": 0, "data": {"open_id": "ou_1", "name": "洪思琴", "tenant_key": tenant_key}})

    monkeypatch.setattr(feishu_login.requests, "post", fake_post)
    monkeypatch.setattr(feishu_login.requests, "get", fake_get)
    return calls


def _start_login(client, next_path="/review"):
    resp = client.get(f"/login?next={next_path}")
    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["Location"]).query)
    return query["state"][0], query


def test_login_disabled_keeps_pages_open(client):
    assert client.get("/").status_code == 200


def test_page_redirects_to_login_when_not_logged_in(login_client):
    resp = login_client.get("/review?page=2")
    assert resp.status_code == 302
    location = urlparse(resp.headers["Location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["next"] == ["/review?page=2"]


def test_api_returns_401_when_not_logged_in(login_client):
    resp = login_client.post("/api/run", json={})
    assert resp.status_code == 401
    assert resp.get_json()["login_required"] is True


def test_health_stays_public(login_client):
    assert login_client.get("/health").status_code == 200


def test_login_redirects_to_feishu_authorize(login_client):
    resp = login_client.get("/login")
    location = urlparse(resp.headers["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == feishu_login.AUTHORIZE_URL
    query = parse_qs(location.query)
    assert query["client_id"] == ["cli_test"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["state"][0]


def test_callback_logs_in_and_returns_to_next(login_client, monkeypatch):
    calls = _stub_feishu(monkeypatch)
    state, _ = _start_login(login_client)
    resp = login_client.get(f"/auth/feishu/callback?code=abc&state={state}")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/review"
    assert calls["token"][1]["code"] == "abc"
    assert calls["token"][1]["redirect_uri"] == REDIRECT_URI
    assert calls["user_info"][1]["Authorization"] == "Bearer u-token"

    page = login_client.get("/")
    assert page.status_code == 200
    assert "洪思琴" in page.get_data(as_text=True)


def test_callback_rejects_wrong_state(login_client, monkeypatch):
    calls = _stub_feishu(monkeypatch)
    _start_login(login_client)
    resp = login_client.get("/auth/feishu/callback?code=abc&state=forged")
    assert resp.status_code == 400
    assert "token" not in calls
    assert login_client.get("/").status_code == 302


def test_callback_rejects_other_tenant(login_client, monkeypatch):
    _stub_feishu(monkeypatch, tenant_key="tenant_other")
    state, _ = _start_login(login_client)
    resp = login_client.get(f"/auth/feishu/callback?code=abc&state={state}")
    assert resp.status_code == 403
    assert login_client.get("/").status_code == 302


def test_callback_shows_feishu_error(login_client, monkeypatch):
    _stub_feishu(monkeypatch, token_code=20003)
    state, _ = _start_login(login_client)
    resp = login_client.get(f"/auth/feishu/callback?code=abc&state={state}")
    assert resp.status_code == 502
    assert "20003" in resp.get_data(as_text=True)


def test_callback_without_code_means_cancelled(login_client):
    state, _ = _start_login(login_client)
    resp = login_client.get(f"/auth/feishu/callback?error=access_denied&state={state}")
    assert resp.status_code == 400


def test_next_rejects_external_url(login_client, monkeypatch):
    _stub_feishu(monkeypatch)
    state, _ = _start_login(login_client, next_path="//evil.example.com")
    resp = login_client.get(f"/auth/feishu/callback?code=abc&state={state}")
    assert resp.headers["Location"] == "/"


def test_logout_clears_session(login_client, monkeypatch):
    _stub_feishu(monkeypatch)
    state, _ = _start_login(login_client)
    login_client.get(f"/auth/feishu/callback?code=abc&state={state}")
    assert login_client.get("/").status_code == 200
    assert login_client.get("/logout").status_code == 200
    assert login_client.get("/").status_code == 302
