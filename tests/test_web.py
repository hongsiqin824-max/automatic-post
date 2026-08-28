from __future__ import annotations

from dataclasses import replace

from app import repository as repo
from app.db import get_db
from app.services.dqd_open_client import DqdOpenClientError, DqdOpenDraftResult
from app.web import _event_view, format_beijing_time


def test_format_beijing_time_converts_utc_and_handles_invalid_values():
    assert format_beijing_time("2026-08-10T02:18:06.681Z") == "2026-08-10 10:18:06"
    assert format_beijing_time("2026-08-10T02:18:06+00:00") == "2026-08-10 10:18:06"
    assert format_beijing_time("") == ""
    assert format_beijing_time("not-a-timestamp") == "not-a-timestamp"


def test_core_pages_render(client):
    for path in ("/", "/articles", "/review", "/config", "/health"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_dashboard_renders_beijing_time(app, client):
    with app.app_context():
        repo.set_setting("last_run_at", "2026-08-10T02:18:06.681Z", get_db())

    html = client.get("/").get_data(as_text=True)
    assert "2026-08-10 10:18:06" in html
    assert "2026-08-10T02:18:06.681Z" not in html


def test_source_and_tab_configuration_api(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
    response = client.post(f"/api/sources/marca", json={"tab_id": tab["id"], "enabled": True})
    assert response.status_code == 200
    assert response.get_json()["source"]["enabled"] == 1


def test_source_publish_mode_override_can_follow_tabs_or_force_mode(app, client):
    forced = client.post("/api/sources/marca", json={"publish_mode_override": 1})
    assert forced.status_code == 200
    assert forced.get_json()["source"]["publish_mode_override"] == 1
    assert "来源强制" in forced.get_json()["source"]["publish_mode_label"]

    followed = client.post("/api/sources/marca", json={"publish_mode_override": None})
    assert followed.status_code == 200
    assert followed.get_json()["source"]["publish_mode_override"] is None

    for invalid in (True, False, "1", 2):
        response = client.post("/api/sources/marca", json={"publish_mode_override": invalid})
        assert response.status_code == 400
        assert "publish_mode_override" in response.get_json()["error"]

    html = client.get("/config").get_data(as_text=True)
    assert 'data-source-publish-mode-select' in html
    assert "跟随栏目" in html
    assert "强制直接发布" in html


def test_create_source_accepts_publish_mode_override(client):
    response = client.post(
        "/api/sources",
        json={
            "code": "override-source",
            "display_name": "覆盖来源",
            "publish_mode_override": 0,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["source"]["publish_mode_override"] == 0

    invalid = client.post(
        "/api/sources",
        json={"code": "invalid-override", "display_name": "非法覆盖", "publish_mode_override": True},
    )
    assert invalid.status_code == 400


def test_tab_publish_mode_configuration_api_and_page(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        assert tab["publish_mode"] == 0

    updated = client.post(f"/api/tabs/{tab['id']}", json={"publish_mode": 1})
    assert updated.status_code == 200
    assert updated.get_json()["tab"]["publish_mode"] == 1

    invalid = client.post(f"/api/tabs/{tab['id']}", json={"publish_mode": True})
    assert invalid.status_code == 400
    assert "publish_mode" in invalid.get_json()["error"]

    html = client.get("/config").get_data(as_text=True)
    assert 'data-toggle-tab-publish-mode' in html
    assert 'data-current-mode="1"' in html
    # The old source publish-mode hook is gone; source mode now uses the
    # explicit follow/override select rendered on each source row.
    assert 'data-toggle-publish-mode' not in html


def test_tab_publish_mode_api_returns_affected_source_views(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source(
            "marca", tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )

    response = client.post(f"/api/tabs/{tab['id']}", json={"publish_mode": 1})

    assert response.status_code == 200
    payload = response.get_json()
    affected = {source["code"]: source for source in payload["affected_sources"]}
    assert affected["marca"]["publish_mode_effective"] == 1
    assert affected["marca"]["publish_mode_label"] == "跟随栏目 · 直接发布"


def test_article_detail_warns_before_direct_publish(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_tab(tab["id"], publish_mode=1)
        repo.update_source(
            "marca", tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/direct-publish-warning",
            "translate_title": "直接发布操作必须明确提示",
            "translate_body": "<p>这是一段足够长的正文，用于验证直接发布操作的页面提示。</p>",
            "channels": [],
        })["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

    html = client.get(f"/articles/{article['id']}").get_data(as_text=True)

    assert "直接发布到懂球帝" in html
    assert "操作成功后文章会立即上线" in html
    assert "提交成功后文章会立即上线，不能再作为草稿审核" in html


def test_create_source_configuration_api(app, client):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:2]

    response = client.post(
        "/api/sources",
        json={
            "code": "new-league",
            "display_name": "新联赛来源",
            "tab_ids": [tabs[0]["id"], tabs[1]["id"]],
        },
    )

    assert response.status_code == 200
    source = response.get_json()["source"]
    assert source["code"] == "new-league"
    assert source["display_name"] == "新联赛来源"
    assert source["enabled"] == 0
    assert source["tab_ids"] == [tabs[0]["id"], tabs[1]["id"]]
    assert "new-league" in client.get("/config").get_data(as_text=True)


def test_create_enabled_source_without_tab_is_rejected(client):
    response = client.post(
        "/api/sources",
        json={"code": "new-league", "display_name": "新联赛来源", "enabled": True},
    )

    assert response.status_code == 400
    assert "at least one enabled tab" in response.get_json()["error"]


def test_create_source_api_rejects_duplicate_and_invalid_payload(client):
    payload = {"code": "new-league", "display_name": "新联赛来源"}
    assert client.post("/api/sources", json=payload).status_code == 200

    duplicate = client.post("/api/sources", json=payload)
    assert duplicate.status_code == 400
    assert duplicate.get_json()["error"] == "source code 已存在"

    invalid_tabs = client.post(
        "/api/sources",
        json={**payload, "code": "another-league", "tab_ids": "1"},
    )
    assert invalid_tabs.status_code == 400
    assert "JSON 数组" in invalid_tabs.get_json()["error"]

    invalid_body = client.post(
        "/api/sources", data="[]", content_type="application/json"
    )
    assert invalid_body.status_code == 400
    assert "JSON 对象" in invalid_body.get_json()["error"]


def test_source_configuration_api_and_page_support_multiple_tabs(app, client):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:2]

    response = client.post(
        "/api/sources/marca",
        json={"tab_ids": [tabs[0]["id"], tabs[1]["id"], tabs[0]["id"]], "enabled": True},
    )

    assert response.status_code == 200
    source = response.get_json()["source"]
    assert source["tab_ids"] == [tabs[0]["id"], tabs[1]["id"]]
    html = client.get("/config").get_data(as_text=True)
    assert "手动为每个来源选择一个或多个栏目" in html
    assert f"data-tab-ids='[{tabs[0]['id']}, {tabs[1]['id']}]'" in html
    assert tabs[0]["name"] in html
    assert tabs[1]["name"] in html


def test_enabled_source_cannot_clear_all_tabs_via_api(app, client):
    with app.app_context():
        tab = repo.list_tabs(include_disabled=False)[0]
        repo.update_source("marca", tab_ids=[tab["id"]], enabled=True)

    response = client.post("/api/sources/marca", json={"tab_ids": [], "enabled": True})

    assert response.status_code == 400
    assert "at least one enabled tab" in response.get_json()["error"]


def test_source_update_without_tab_field_preserves_mapping(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
    response = client.post("/api/sources/marca", json={"enabled": False})
    assert response.status_code == 200
    assert response.get_json()["source"]["tab_id"] == tab["id"]


def test_review_api_moves_article_to_ready_queue(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-review",
            "translate_title": "需要人工处理的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证人工审核接口是否可以把文章放入待发队列。</p>",
            "channels": [],
        })["article"]
        repo.transition_status(article["id"], "NEEDS_REVIEW")
    response = client.post(f"/api/articles/{article['id']}/review", json={"action": "pass"})
    assert response.status_code == 200
    assert response.get_json()["article"]["status"] == "READY_TO_PUBLISH"


def test_create_draft_retry_api_records_attempt_and_archive_id(app, client, monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, **kwargs):
            assert article["status"] == "PUBLISH_FAILED"
            assert tab["backend_tab_id"]
            return DqdOpenDraftResult(
                archive_id=3802222,
                payload={"code": 0, "data": {"archive_id": 3802222}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("title", article["title_final"]), ("tabs[]", str(tab["backend_tab_id"]))],
            )

    cfg = replace(
        app.extensions["app_config"],
        database_path=app.config["DATABASE"],
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    app.extensions["app_config"] = cfg
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-retry",
            "translate_title": "可重试创建草稿的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证单篇草稿重试接口是否可以写入时间线。</p>",
            "dqd_litpic": "/fastdfs8/web-draft.jpg",
            "channels": [11],
        })["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")
        repo.transition_status(article["id"], "PUBLISH_FAILED")

    response = client.post(f"/api/articles/{article['id']}/create-draft")
    assert response.status_code == 200
    body = response.get_json()
    assert body["article"]["status"] == "DRAFT_CREATED"
    assert body["result"]["archive_id"] == 3802222
    assert body["result"]["draft_url"] == "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"
    assert body["article"]["draft_url"] == "https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"

    with app.app_context():
        events = repo.list_article_events(article["id"], get_db())
    assert events[-1]["event_type"] == "DRAFT_RETRY_SUCCEEDED"
    assert "draft_url" in events[-1]["payload_json"]


def test_article_detail_renders_clickable_draft_link(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-link",
            "translate_title": "可点击草稿链接的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证详情页草稿链接展示。</p>",
            "dqd_litpic": "/fastdfs8/web-draft-link.jpg",
            "channels": [11],
        })["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3802222)

    response = client.get(f"/articles/{article['id']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'href="https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802222"' in html
    assert ">3802222<" in html


def test_article_detail_shows_recovered_draft_hint_when_failed_but_archive_exists(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-hint",
            "translate_title": "失败但已有草稿链接的文章",
            "translate_body": "<p>这是一段足够长的正文，用于验证失败态下的草稿提示。</p>",
            "dqd_litpic": "/fastdfs8/web-draft-hint.jpg",
            "channels": [11],
        })["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3802444)
        repo.transition_status(article["id"], "PUBLISH_FAILED")

    response = client.get(f"/articles/{article['id']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "这条文章其实已经创建过懂球帝草稿" in html
    assert 'href="https://dadmin.dongqiudi.com/admin/archives/articlePublish?articleId=3802444"' in html


def test_draft_confirming_page_shows_automatic_confirmation_details_and_blocks_retry(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-confirming",
            "translate_title": "结果不确定时自动核对",
            "translate_body": "<p>这是一段用于验证草稿结果确认中页面的完整正文。</p>",
            "channels": [11],
        })["article"]
        repo.transition_status(article["id"], "PUBLISHING")
        confirming = repo.mark_draft_result_unknown(
            article["id"],
            request_id="upstream-request-confirming",
            next_confirm_at="2026-08-10T03:18:06.681Z",
        )
        assert confirming["status"] == "DRAFT_CONFIRMING"
        assert confirming["client_request_id"]
        conn = get_db()
        with conn:
            conn.execute(
                """
                UPDATE articles
                SET draft_confirm_attempts=2,
                    draft_last_attempt_at='2026-08-10T02:18:06.681Z'
                WHERE id=?
                """,
                (article["id"],),
            )

    response = client.get(f"/articles/{article['id']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "草稿结果自动处理中" in html
    assert "系统已安排再次调用创建接口" in html
    assert "upstream-request-confirming" in html
    assert confirming["client_request_id"] in html
    assert "自动核对次数" in html
    assert "<dd>2</dd>" in html
    assert "2026-08-10 10:18:06" in html
    assert "2026-08-10 11:18:06" in html
    assert "data-create-draft" not in html

    blocked = client.post(f"/api/articles/{article['id']}/create-draft")
    assert blocked.status_code == 409
    payload = blocked.get_json()
    assert payload["success"] is False
    assert payload["result"] == {"skipped": True, "reason": "draft_confirming"}
    assert "自动处理中" in payload["error"]
    assert payload["article"]["status"] == "DRAFT_CONFIRMING"


def test_create_draft_api_returns_accepted_for_ambiguous_upstream_result(app, client, monkeypatch):
    class AmbiguousClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, **kwargs):
            raise DqdOpenClientError(
                "创建草稿请求失败: HTTP 502",
                status_code=502,
                diagnostics={"request_id": "api-upstream-502"},
                result_unknown=True,
            )

    cfg = replace(
        app.extensions["app_config"],
        database_path=app.config["DATABASE"],
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
    )
    app.extensions["app_config"] = cfg
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", AmbiguousClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-draft-502",
            "translate_title": "502 后进入结果核对",
            "translate_body": "<p>这是一段足够长的正文，用于验证 502 结果未知接口语义。</p>",
            "dqd_litpic": "/fastdfs8/web-draft-502.jpg",
            "channels": [11],
        })["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

    response = client.post(f"/api/articles/{article['id']}/create-draft")
    assert response.status_code == 202
    body = response.get_json()
    assert body["success"] is True
    assert body["operation_status"] == "DRAFT_CONFIRMING"
    assert body["result"]["request_id"] == "api-upstream-502"


def test_article_detail_preview_renders_sanitized_body_and_uses_publish_cover(app, client):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-preview",
            "translate_title": "发布预览保留标题和正文结构",
            "translate_body": (
                "<p>第一段正文</p>"
                '<p><img src="/fastdfs8/body-preview.jpg" onerror="window.__xss=1"></p>'
                "<h2>绿色小标题</h2>"
                "<script>window.__xss=1</script>"
            ),
            "dqd_litpic": "/fastdfs8/cover-preview.jpg",
            "channels": [11],
        })["article"]

    quality_html = client.get(f"/articles/{article['id']}").get_data(as_text=True)
    assert (
        f'class="detail-view-tab is-active" href="/articles/{article["id"]}?view=quality"'
        in quality_html
    )
    assert "发布预览保留标题和正文结构" in quality_html

    response = client.get(f"/articles/{article['id']}?view=preview")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'aria-current="page">' in html
    assert "懂球帝草稿预览" in html
    assert "<h2>绿色小标题</h2>" in html
    assert "https://img1.qunliao.info/fastdfs8/body-preview.jpg" in html
    assert "后台封面（将提交）" in html
    assert "<script>" not in html.lower()
    assert 'onerror="' not in html.lower()
    assert html.count('src="https://img1.qunliao.info/fastdfs8/body-preview.jpg"') == 2
    assert 'src="https://img1.qunliao.info/fastdfs8/cover-preview.jpg"' not in html
    assert 'src="https://img1.qunliao.info/fastdfs8/cover-preview.jpg"' in quality_html

    fallback_html = client.get(f"/articles/{article['id']}?view=unknown").get_data(as_text=True)
    assert (
        f'class="detail-view-tab is-active" href="/articles/{article["id"]}?view=quality"'
        in fallback_html
    )
    assert "懂球帝草稿预览" not in fallback_html


def test_article_detail_preview_inserts_cover_into_body_without_mutating_article(app, client):
    stored_body = "<p>第一段正文</p><p>第二段正文</p>"
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-preview-cover-fallback",
            "translate_title": "发布预览显示正文补图",
            "translate_body": stored_body,
            "dqd_litpic": "/fastdfs8/cover-fallback.jpg",
            "channels": [11],
        })["article"]

    response = client.get(f"/articles/{article['id']}?view=preview")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    image_url = "https://img1.qunliao.info/fastdfs8/cover-fallback.jpg"
    assert html.count(f'src="{image_url}"') == 2
    assert f'<p>第一段正文</p><p><img src="{image_url}"' in html
    with app.app_context():
        assert repo.get_article(article["id"])["body_html"] == stored_body


def test_article_detail_shows_all_snapshot_tabs(app, client):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:2]
        repo.update_source("marca", tab_ids=[tabs[0]["id"], tabs[1]["id"]], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/web-multiple-tabs",
            "translate_title": "多个栏目展示测试",
            "translate_body": "<p>这是一段用于验证文章栏目快照展示的完整正文。</p>",
            "dqd_litpic": "/fastdfs8/web-multiple-tabs.jpg",
            "channels": [],
        })["article"]

    html = client.get(f"/articles/{article['id']}").get_data(as_text=True)

    assert tabs[0]["name"] in html
    assert tabs[1]["name"] in html
    assert f"ID {tabs[0]['backend_tab_id']}" in html
    assert f"ID {tabs[1]['backend_tab_id']}" in html


def test_article_detail_renders_auto_repair_and_two_quality_rounds(app, client):
    """The timeline should explain repair statistics without hiding raw audit data."""
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material({
            "source": "marca",
            "source_url": "https://example.com/auto-repair-timeline",
            "translate_title": "自动优化时间线展示",
            "translate_body": "<p>这是一段足够长的正文，用于验证自动优化和二次质检的展示。</p>",
            "channels": [],
        })["article"]
        repo.add_article_event(
            article["id"], "QUALITY_RESULT", get_db(),
            message="首轮发现推广内容",
            payload={"quality_round": 1, "pass": False, "needs_review": True, "level": "B", "score": 62},
        )
        repo.add_article_event(
            article["id"], "AUTO_REPAIR_TRIGGERED", get_db(),
            payload={
                "quality_round": 1,
                "trigger_reason": "正文含高置信推广段落",
                "match_count": 2,
                "rules": ["standalone_call_to_action"],
                "matched_texts": ["点击官网查看详情", "扫码关注"],
                "first_quality": {"pass": False, "reason": "存在推广内容"},
            },
        )
        repo.add_article_event(
            article["id"], "AUTO_REPAIR_APPLIED", get_db(),
            payload={
                "removed_blocks": [{"rule": "standalone_call_to_action", "text": "点击官网查看详情"}],
                "removed_count": 1,
                "attribution_normalized_count": 1,
                "attribution_changes": [{
                    "rule": "photo_credit_marker",
                    "before": "球员庆祝进球 [照片]=Getty Images",
                    "after": "球员庆祝进球（图片来源：Getty Images）",
                }],
                "match_count": 2,
                "body_length_before": 1200,
                "body_length_after": 1178,
                "image_count_before": 3,
                "image_count_after": 3,
                "image_sources_unchanged": True,
                "title_unchanged": True,
            },
        )
        repo.add_article_event(
            article["id"], "QUALITY_RESULT", get_db(),
            message="二次质检通过",
            payload={"quality_round": 2, "pass": True, "needs_review": False, "level": "B", "score": 96},
        )
        repo.add_article_event(
            article["id"], "AUTO_REPAIR_FINISHED", get_db(),
            payload={
                "outcome": "passed",
                "final_status": "READY_TO_PUBLISH",
                "destination": "沿用来源原栏目 · 创建草稿",
                "second_quality": {"pass": True, "reason": "正文完整"},
            },
        )

    response = client.get(f"/articles/{article['id']}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "首轮自动质检完成" in html
    assert "二次自动质检完成" in html
    assert "已触发自动优化" in html
    assert "命中数量" in html and ">2<" in html
    assert "正文长度" in html and "1200 → 1178" in html
    assert "图片数量" in html and "3 → 3" in html
    assert "图片地址" in html and "未改变" in html
    assert "图片署名" in html and "已规范化 1 处（未删除图片说明）" in html
    assert "球员庆祝进球（图片来源：Getty Images）" in html
    assert "优化后质检通过" in html
    assert "沿用来源原栏目 · 创建草稿" in html
    assert "查看原始记录" in html


def test_event_view_tolerates_invalid_payload_and_does_not_invent_quality_result():
    event = _event_view({"event_type": "QUALITY_RESULT", "payload_json": "{not-json"})
    assert event["payload"] == {}
    assert event["summary_rows"] == []


def test_run_endpoint_is_non_blocking(client):
    response = client.post("/api/run", json={})
    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_publish_account_pool_configuration_flow(app, client):
    empty_toggle = client.post("/api/publish-accounts/toggle", json={"enabled": True})
    assert empty_toggle.status_code == 400
    assert "至少需要一个已启用" in empty_toggle.get_json()["error"]

    created = client.post(
        "/api/publish-accounts",
        json={"dqd_user_id": 13421038, "user_name": "足球实战技巧", "enabled": True},
    )
    assert created.status_code == 200
    account = created.get_json()["account"]
    assert account["dqd_user_id"] == 13421038
    assert account["user_name"] == "足球实战技巧"

    enabled = client.post("/api/publish-accounts/toggle", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.get_json()["enabled"] is True

    blocked = client.post(
        f"/api/publish-accounts/{account['id']}", json={"enabled": False}
    )
    assert blocked.status_code == 400
    assert "不能停用最后一个" in blocked.get_json()["error"]

    updated = client.post(
        f"/api/publish-accounts/{account['id']}",
        json={"dqd_user_id": 13421039, "user_name": "足球技巧号", "enabled": True},
    )
    assert updated.status_code == 200
    assert updated.get_json()["account"]["dqd_user_id"] == 13421039

    html = client.get("/config").get_data(as_text=True)
    assert "发布账号池" in html
    assert "足球技巧号" in html
    assert "13421039" in html
    assert "不保存密码、cookie 或 token" in html


def test_publish_account_api_rejects_invalid_types(client):
    response = client.post(
        "/api/publish-accounts",
        json={"dqd_user_id": "13421038", "user_name": "字符串 ID", "enabled": True},
    )
    assert response.status_code == 400
    assert "正整数" in response.get_json()["error"]

    response = client.post("/api/publish-accounts/toggle", json={"enabled": "true"})
    assert response.status_code == 400
    assert "JSON boolean" in response.get_json()["error"]
