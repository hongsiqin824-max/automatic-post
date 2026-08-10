from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app import db
from app.services.dqd_open_client import DqdOpenClient
from app.services.open_platform import OpenPlatformClient, build_draft_url


def test_start_authorization_persists_state(app):
    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        app.extensions["app_config"] = cfg
        result = OpenPlatformClient(cfg).start_authorization()
        auth = db.get_open_platform_auth(cfg.database_path)

    assert result["authorize_url"].startswith("https://platform.dongqiudi.com/open/oauth/authorize?")
    assert result["redirect_uri"].endswith("/api/open/auth/callback")
    assert auth["auth_status"] == "AUTHORIZING"
    assert auth["pending_state"] == result["state"]


def test_handle_callback_saves_tokens(app, monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "{\"code\":0}"

        def json(self):
            return {
                "code": 0,
                "data": {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 7200,
                    "user_info": {"name": "tester"},
                },
            }

    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        app.extensions["app_config"] = cfg
        db.update_open_platform_auth(
            cfg.database_path,
            auth_status="AUTHORIZING",
            pending_state="state-123",
            pending_state_expires_at="2099-01-01T00:00:00+00:00",
        )
        client = OpenPlatformClient(cfg)
        monkeypatch.setattr(client.session, "post", lambda *args, **kwargs: FakeResponse())
        client.handle_callback("code-123", "state-123")
        auth = db.get_open_platform_auth(cfg.database_path)

    assert auth["auth_status"] == "AUTHORIZED"
    assert auth["access_token"] == "access-token"
    assert auth["refresh_token"] == "refresh-token"
    assert auth["authorized_user"]["name"] == "tester"


def test_post_signed_accepts_missing_dqd_headers(app, monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "{\"code\":0,\"data\":{}}"
        url = "https://platform.dongqiudi.com/open/v1/do?api_name=admin-archive-createarticle"

        def json(self):
            return {"code": 0, "data": {}}

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, data=None, headers=None, timeout=None):
            self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
            return FakeResponse()

    config = SimpleNamespace(
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_api_name="admin-archive-createarticle",
        dqd_open_base_url="https://platform.dongqiudi.com/open/v1/do",
        dqd_open_timeout=30,
        dqd_open_redirect_uri="http://127.0.0.1:8890/api/open/auth/callback",
    )
    session = FakeSession()
    client = OpenPlatformClient(config, session)
    monkeypatch.setattr(client, "ensure_access_token", lambda **kwargs: "access-token")

    response, payload, request_url = client.post_signed(data=[("title", "demo")])

    assert response.status_code == 200
    assert payload["code"] == 0
    assert request_url.startswith("https://platform.dongqiudi.com/open/v1/do?")
    assert session.calls[0]["headers"]["Authorization"] == "Bearer access-token"


def test_create_article_extracts_nested_archive_id(app, monkeypatch):
    class FakeResponse:
        status_code = 200

    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        client = DqdOpenClient(cfg)
        monkeypatch.setattr(
            client.open_platform,
            "post_signed",
            lambda **kwargs: (
                FakeResponse(),
                {
                    "code": 0,
                    "data": {
                        "code": 0,
                        "data": {
                            "archive_id": 6141666,
                            "result": {"archive_id": 6141666},
                        },
                    },
                },
                "https://platform.dongqiudi.com/open/v1/do",
            ),
        )
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": "<p><img src=\"x.jpg\"></p>", "channels": []}
        draft = client.create_article(article, tab)

    assert draft.archive_id == 6141666


def test_create_article_extracts_nested_article_id_alias(app, monkeypatch):
    class FakeResponse:
        status_code = 200

    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        client = DqdOpenClient(cfg)
        monkeypatch.setattr(
            client.open_platform,
            "post_signed",
            lambda **kwargs: (
                FakeResponse(),
                {
                    "code": 0,
                    "data": {
                        "result": {
                            "articleId": "6141777",
                        },
                    },
                },
                "https://platform.dongqiudi.com/open/v1/do",
            ),
        )
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": "<p><img src=\"x.jpg\"></p>", "channels": []}
        draft = client.create_article(article, tab)

    assert draft.archive_id == 6141777


def test_build_draft_url_returns_backend_article_page():
    assert build_draft_url(3802222) == "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"
    assert build_draft_url("0") == ""
    assert build_draft_url(None) == ""
