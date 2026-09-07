from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from app import db
import app.services.dqd_open_client as dqd_open_client_module
from app.services.dqd_open_client import (
    DqdOpenClient,
    DqdOpenClientError,
    build_create_article_form,
)
from app.services.article_images import first_image_src
from app.services.dqd_publish_html import DqdPublishHtmlError
from app.services.open_platform import (
    OpenPlatformClient,
    OpenPlatformRequestError,
    build_draft_url,
)


def test_first_image_src_selects_first_safe_image():
    body = (
        '<p><img src="javascript:alert(1)"></p>'
        '<p><IMG alt="正文首图" src="/fastdfs8/body-cover.jpg"></p>'
        '<p><img src="/fastdfs8/second.jpg"></p>'
    )

    assert first_image_src(body) == "/fastdfs8/body-cover.jpg"


def test_first_image_src_rejects_empty_and_unsafe_values():
    assert first_image_src('<img src="data:image/png;base64,abc">') is None
    assert first_image_src('<img src="">') is None


def test_create_article_form_prefers_body_first_image_over_material_cover(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    article = {
        "title_final": "正文首图作为后台封面",
        "body_html": '<p><img src="/fastdfs8/body-cover.jpg"></p>',
        "litpic": "/fastdfs7/material-cover.jpg",
    }

    form = build_create_article_form(article, {"backend_tab_id": 284}, config)

    assert ("litpic", "/fastdfs8/body-cover.jpg") in form
    assert article["litpic"] == "/fastdfs7/material-cover.jpg"


def test_create_article_form_falls_back_to_material_cover_without_body_image(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )

    form = build_create_article_form(
        {
            "title_final": "回退素材封面",
            "body_html": "<p>没有图片的正文</p>",
            "litpic": "/fastdfs7/material-cover.jpg",
        },
        {"backend_tab_id": 284},
        config,
    )

    assert ("litpic", "/fastdfs7/material-cover.jpg") in form


@pytest.mark.parametrize("status", [0, 1])
def test_create_article_form_inserts_cover_into_body_for_both_publish_modes(app, status):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )

    form = build_create_article_form(
        {
            "title_final": "提交前补正文图片",
            "body_html": "<p>第一段</p><p>第二段</p>",
            "litpic": "/fastdfs7/material-cover.jpg",
        },
        {"backend_tab_id": 284},
        config,
        status=status,
    )

    fields = dict(form)
    assert fields["status"] == str(status)
    assert fields["body"] == (
        '<p>第一段</p><p><img src="/fastdfs7/material-cover.jpg"></p><p>第二段</p>'
    )
    assert fields["litpic"] == "/fastdfs7/material-cover.jpg"


@pytest.mark.parametrize("status", [True, False, -1, 2, "publish"])
def test_create_article_form_rejects_invalid_publish_status(app, status):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )

    with pytest.raises(DqdOpenClientError, match="status 必须是 0"):
        build_create_article_form(
            {
                "title_final": "非法发布模式",
                "body_html": "<p>用于验证 status 参数校验的正文。</p>",
            },
            {"backend_tab_id": 284},
            config,
            status=status,
        )


def test_create_article_form_keeps_existing_body_image_and_does_not_duplicate(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    body = ' \n<p>第一段</p><p><img src="/fastdfs8/body-cover.jpg"></p><p>末段</p>\n '
    article = {
        "title_final": "已有正文图片",
        "body_html": body,
        "litpic": "/fastdfs7/material-cover.jpg",
    }

    first_body = dict(
        build_create_article_form(article, {"backend_tab_id": 284}, config)
    )["body"]
    second_body = dict(
        build_create_article_form(
            {**article, "body_html": first_body},
            {"backend_tab_id": 284},
            config,
        )
    )["body"]

    assert first_body == body
    assert second_body == body
    assert second_body.count("<img") == 1


def test_create_article_form_removes_linked_text_but_preserves_linked_images(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    body = (
        '<p>正文内容，包含比赛信息和赛后采访。</p>'
        '<p><a href="https://example.com/related">澳超相关新闻</a></p>'
        '<p><a href="https://example.com/image"><img src="/body.jpg"></a></p>'
    )

    fields = dict(build_create_article_form(
        {"title_final": "去除可跳转内容", "body_html": body},
        {"backend_tab_id": 284},
        config,
    ))

    assert "href=" not in fields["body"]
    assert "澳超相关新闻" not in fields["body"]
    assert '<img src="/body.jpg">' in fields["body"]


def test_create_article_form_applies_final_quality_cleanup(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    body = (
        '<p data-href="https://example.com">正文内容，包含比赛信息和赛后采访。</p>'
        '<iframe src="https://video.example/player"></iframe>'
        '<p>[相关阅读][source]</p>\n'
        '[source]: https://example.com/news\n'
        '<p><img src="/body.jpg" alt="比赛图"></p>'
    )

    fields = dict(build_create_article_form(
        {"title_final": "最终提交前清理", "body_html": body},
        {"backend_tab_id": 284},
        config,
    ))

    assert "data-href=" not in fields["body"].lower()
    assert "<iframe" not in fields["body"].lower()
    assert "[相关阅读]" not in fields["body"]
    assert "[source]:" not in fields["body"]
    assert '<img src="/body.jpg" alt="比赛图">' in fields["body"]


def test_create_article_form_rejects_body_erased_by_final_cleanup(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )

    with pytest.raises(DqdOpenClientError, match="正文为空"):
        build_create_article_form(
            {"title_final": "清理后正文为空", "body_html": "<p>前文</p><p>正文</p>"},
            {"backend_tab_id": 284},
            config,
        )


def test_create_article_form_uses_shared_default_when_publish_image_is_missing(app):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )

    form = build_create_article_form(
        {
            "title_final": "没有可用图片",
            "body_html": "<p>纯文字正文</p>",
            "litpic": "javascript:alert(1)",
        },
        {"backend_tab_id": 284},
        config,
    )
    assert dict(form)["litpic"] == "/fastdfs7/M00/71/51/rBUC6Gh3f-uAJ3PaAABRUJ73Hek971.jpg"


@pytest.mark.parametrize("status", [0, 1])
def test_create_article_form_cleans_wordpress_metadata_for_both_modes(app, status):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    body = (
        '<p>澳超正文</p><img src="https://img.example/aleagues.jpg" '
        "data-image-meta='{" + '"camera":"Canon EOS R5"' + "}' "
        'data-orig-file="https://img.example/original.jpg" class="wp-image-1">'
    )

    fields = dict(
        build_create_article_form(
            {"title_final": "提交前清理", "body_html": body},
            {"backend_tab_id": 284},
            config,
            status=status,
        )
    )

    assert fields["status"] == str(status)
    assert "data-image-meta" not in fields["body"]
    assert "data-orig-file" not in fields["body"]
    assert 'src="https://img.example/aleagues.jpg"' in fields["body"]
    assert 'class="wp-image-1"' in fields["body"]


def test_html_validation_failure_stops_before_open_platform_request(app, monkeypatch):
    config = replace(
        app.extensions["app_config"],
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    client = DqdOpenClient(config)
    request_called = False

    def fail_cleanup(body):
        raise DqdPublishHtmlError("校验失败")

    def unexpected_request(**kwargs):
        nonlocal request_called
        request_called = True
        raise AssertionError("不应调用懂球帝接口")

    monkeypatch.setattr(dqd_open_client_module, "sanitize_dqd_publish_html", fail_cleanup)
    monkeypatch.setattr(client.open_platform, "post_signed", unexpected_request)

    with pytest.raises(DqdOpenClientError, match="懂球帝正文安全校验失败：校验失败"):
        client.create_article(
            {
                "title_final": "不应提交",
                "body_html": '<p>正文</p><img src="/one.jpg">',
            },
            {"backend_tab_id": 284},
        )

    assert request_called is False


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


def test_create_article_sends_no_roll_recommend_field(app, monkeypatch):
    class FakeResponse:
        status_code = 200

    submitted: dict[str, object] = {}

    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        client = DqdOpenClient(cfg)

        def post_signed(**kwargs):
            submitted.update(kwargs)
            return (
                FakeResponse(),
                {"code": 0, "data": {"archive_id": 6141667}},
                "https://platform.dongqiudi.com/open/v1/do",
            )

        monkeypatch.setattr(client.open_platform, "post_signed", post_signed)
        client.create_article(
            {"title_final": "关闭滚动推荐测试", "body_html": "<p>测试正文。</p>"},
            {"backend_tab_id": 1},
        )

    assert ("no_roll_recommend", "1") in submitted["data"]


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


@pytest.mark.parametrize(
    ("inner_data", "expected_message"),
    [
        (
            {
                "code": 3,
                "message": "创建失败",
                "data": {"result": {"success": False, "message": "重复请求"}},
            },
            "懂球帝创建草稿失败：重复请求",
        ),
        (
            {
                "code": 5,
                "message": "服务异常",
                "data": {"err": "internal database detail"},
            },
            "懂球帝创建草稿失败：服务异常",
        ),
    ],
)
def test_create_article_reports_nested_business_failure(
    app,
    monkeypatch,
    inner_data,
    expected_message,
):
    class FakeResponse:
        status_code = 200

    response_payload = {
        "code": 0,
        "message": "success",
        "data": inner_data,
        "request_id": "request-123",
    }
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
                response_payload,
                "https://platform.dongqiudi.com/open/v1/do",
            ),
        )
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>', "channels": []}

        with pytest.raises(DqdOpenClientError, match=expected_message) as raised:
            client.create_article(article, tab)

    assert raised.value.payload == response_payload
    assert raised.value.status_code == 200
    assert raised.value.diagnostics["request_id"] == "request-123"
    assert raised.value.result_unknown is (inner_data.get("code") == 5)
    assert raised.value.diagnostics["result_unknown"] is (inner_data.get("code") == 5)


def test_create_article_keeps_missing_archive_id_fallback_for_ambiguous_success(app, monkeypatch):
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
                {"code": 0, "message": "success", "data": {}},
                "https://platform.dongqiudi.com/open/v1/do",
            ),
        )
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>', "channels": []}

        with pytest.raises(
            DqdOpenClientError,
            match="创建草稿成功但没有返回 archive_id",
        ) as raised:
            client.create_article(article, tab)

    assert raised.value.result_unknown is True
    assert raised.value.diagnostics["result_unknown"] is True


def test_create_article_marks_http_502_as_result_unknown(app, monkeypatch):
    payload = {
        "code": 50001,
        "message": "内部接口调用失败",
        "request_id": "upstream-request-502",
    }
    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
            dqd_open_idempotency_enabled=True,
        )
        client = DqdOpenClient(cfg)

        def raise_502(**kwargs):
            raise OpenPlatformRequestError(
                "创建文章接口返回 HTTP 502: 内部接口调用失败",
                payload=payload,
                status_code=502,
                diagnostics={"request_url": "https://platform.dongqiudi.com/open/v1/do"},
            )

        monkeypatch.setattr(client.open_platform, "post_signed", raise_502)
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>'}

        with pytest.raises(DqdOpenClientError, match="HTTP 502") as raised:
            client.create_article(article, tab, client_request_id="draft-request-1")

    assert raised.value.status_code == 502
    assert raised.value.result_unknown is True
    assert raised.value.diagnostics["result_unknown"] is True
    assert raised.value.diagnostics["request_id"] == "upstream-request-502"
    assert raised.value.diagnostics["client_request_id"] == "draft-request-1"


@pytest.mark.parametrize(
    "transport_error",
    [requests.Timeout("request timed out"), requests.ConnectionError("connection reset")],
)
def test_create_article_marks_transport_interruption_as_result_unknown(
    app,
    monkeypatch,
    transport_error,
):
    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        client = DqdOpenClient(cfg)

        def raise_transport_error(**kwargs):
            raise transport_error

        monkeypatch.setattr(client.open_platform, "post_signed", raise_transport_error)
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>'}

        with pytest.raises(DqdOpenClientError, match="创建草稿请求失败") as raised:
            client.create_article(article, tab)

    assert raised.value.result_unknown is True
    assert raised.value.diagnostics["result_unknown"] is True
    assert raised.value.diagnostics["exception_type"] == type(transport_error).__name__


def test_create_article_marks_non_json_response_as_result_unknown(app, monkeypatch):
    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
        )
        client = DqdOpenClient(cfg)

        def raise_non_json(**kwargs):
            raise OpenPlatformRequestError(
                "创建文章接口返回不是 JSON",
                status_code=200,
                diagnostics={"response_text": "upstream gateway error"},
            )

        monkeypatch.setattr(client.open_platform, "post_signed", raise_non_json)
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>'}

        with pytest.raises(DqdOpenClientError, match="不是 JSON") as raised:
            client.create_article(article, tab)

    assert raised.value.result_unknown is True
    assert raised.value.diagnostics["result_unknown"] is True
    assert raised.value.diagnostics["response_text"] == "upstream gateway error"


def test_create_article_submits_configured_idempotency_field(app, monkeypatch):
    class FakeResponse:
        status_code = 200

    submitted = {}
    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
            dqd_open_idempotency_enabled=True,
            dqd_open_idempotency_field="external_request_id",
        )
        client = DqdOpenClient(cfg)

        def post_signed(**kwargs):
            submitted.update(kwargs)
            return (
                FakeResponse(),
                {"code": 0, "data": {"archive_id": 6141888}},
                "https://platform.dongqiudi.com/open/v1/do",
            )

        monkeypatch.setattr(client.open_platform, "post_signed", post_signed)
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>'}
        draft = client.create_article(
            article,
            tab,
            client_request_id="draft-request-2",
        )

    assert ("external_request_id", "draft-request-2") in submitted["data"]
    assert draft.diagnostics["client_request_id"] == "draft-request-2"
    assert draft.diagnostics["idempotency_field"] == "external_request_id"


def test_create_article_omits_idempotency_field_until_enabled(app, monkeypatch):
    class FakeResponse:
        status_code = 200

    submitted = {}
    with app.app_context():
        cfg = replace(
            app.extensions["app_config"],
            dqd_open_appid="appid-test",
            dqd_open_appsecret="secret-test",
            dqd_open_enname="hongsiqin",
            dqd_open_idempotency_enabled=False,
        )
        client = DqdOpenClient(cfg)

        def post_signed(**kwargs):
            submitted.update(kwargs)
            return (
                FakeResponse(),
                {"code": 0, "data": {"archive_id": 6141999}},
                "https://platform.dongqiudi.com/open/v1/do",
            )

        monkeypatch.setattr(client.open_platform, "post_signed", post_signed)
        tab = {"backend_tab_id": 1}
        article = {"title_final": "demo", "body_html": '<p><img src="x.jpg"></p>'}
        client.create_article(article, tab, client_request_id="draft-request-3")

    submitted_keys = {key for key, _ in submitted["data"]}
    assert "client_request_id" not in submitted_keys


def test_build_draft_url_returns_backend_article_page():
    assert build_draft_url(3802222) == "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"
    assert build_draft_url("0") == ""
    assert build_draft_url(None) == ""
