from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app import repository as repo
from app.config import AppConfig
from app.db import get_db
from app.services.dqd_open_client import (
    DqdOpenClientError,
    DqdOpenDraftResult,
    build_create_article_form,
)
from app.services.publisher import (
    confirm_due_draft_results,
    create_draft_for_article,
    publish_ready_articles,
)


def _ready_article():
    return {
        "source": "marca",
        "source_url": "https://example.com/publisher",
        "translate_title": "马卡：球队确认新赛季重要安排",
        "translate_body": "<p>球队确认了新赛季的重要安排，训练计划、热身赛和球迷活动都已经公布。</p>",
        "archive_id": 0,
        "dqd_litpic": "/fastdfs8/publisher.jpg",
        "channels": [11, 12],
    }


def _open_config(database: str) -> AppConfig:
    return AppConfig(
        database_path=database,
        scheduler_enabled=False,
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
        dqd_open_archive_level="B",
        dqd_open_status=0,
    )


def test_create_article_form_uses_backend_tab_and_draft_status(app):
    config = _open_config(":memory:")
    article = {
        "title_final": "完整标题",
        "body_html": "<p>完整正文，信息充分。</p>",
        "litpic": "/fastdfs8/cover.jpg",
        "channels": [11, 12],
    }
    form = build_create_article_form(article, {"backend_tab_id": 284}, config)
    assert ("dqd_enname", "hongsiqin") in form
    assert ("archive_level", "B") in form
    assert ("status", "0") in form
    assert ("no_roll_recommend", "1") in form
    assert ("tabs[]", "284") in form
    assert ("channels", "11,12") in form


def test_create_article_form_filters_blacklisted_channels_at_submission_boundary(app):
    config = _open_config(":memory:")
    article = {
        "title_final": "发布边界标签过滤",
        "body_html": "<p>完整正文，信息充分。</p>",
        "channels": [89, 700000001, 93, 189],
        "channelsnew": [90, 700000002],
    }

    form = build_create_article_form(article, {"backend_tab_id": 284}, config)

    assert ("channels", "700000001,189") in form
    assert ("channelsnew", "700000002") in form


def test_create_article_form_omits_channels_when_all_are_blacklisted(app):
    config = _open_config(":memory:")
    form = build_create_article_form(
        {
            "title_final": "全部标签过滤",
            "body_html": "<p>完整正文，信息充分。</p>",
            "channels": [89, 90, 93],
        },
        {"backend_tab_id": 284},
        config,
    )

    assert not any(key == "channels" for key, _ in form)


def test_create_article_form_includes_publish_account(app):
    config = _open_config(":memory:")
    form = build_create_article_form(
        {
            "title_final": "账号池文章",
            "body_html": "<p>完整正文，信息充分。</p>",
            "litpic": "/fastdfs8/cover.jpg",
        },
        {"backend_tab_id": 284},
        config,
        publish_account={"dqd_user_id": 13421038, "user_name": "足球实战技巧"},
    )

    assert ("user_id", "13421038") in form
    assert ("user_name", "足球实战技巧") in form


def test_create_article_form_omits_publish_account_by_default(app):
    config = _open_config(":memory:")
    form = build_create_article_form(
        {
            "title_final": "默认发布账号文章",
            "body_html": "<p>完整正文，信息充分。</p>",
            "litpic": "/fastdfs8/cover.jpg",
        },
        {"backend_tab_id": 284},
        config,
    )

    assert not any(key in {"user_id", "user_name"} for key, _ in form)


@pytest.mark.parametrize(
    ("publish_account", "error"),
    [
        ({"dqd_user_id": 0, "user_name": "无效账号"}, "dqd_user_id 必须为正整数"),
        ({"dqd_user_id": "invalid", "user_name": "无效账号"}, "dqd_user_id 必须为正整数"),
        ({"dqd_user_id": 13421038, "user_name": "  "}, "user_name 不能为空"),
    ],
)
def test_create_article_form_rejects_invalid_publish_account(app, publish_account, error):
    config = _open_config(":memory:")

    with pytest.raises(DqdOpenClientError, match=error):
        build_create_article_form(
            {
                "title_final": "非法账号文章",
                "body_html": "<p>完整正文，信息充分。</p>",
                "litpic": "/fastdfs8/cover.jpg",
            },
            {"backend_tab_id": 284},
            config,
            publish_account=publish_account,
        )


def test_create_article_form_submits_all_unique_backend_tabs(app):
    config = _open_config(":memory:")
    article = {
        "title_final": "多个栏目文章",
        "body_html": "<p>完整正文，信息充分。</p>",
        "litpic": "/fastdfs8/cover.jpg",
        "channels": [],
    }

    form = build_create_article_form(
        article,
        [
            {"backend_tab_id": 58},
            {"backend_tab_id": 349},
            {"backend_tab_id": 58},
        ],
        config,
    )

    assert [value for key, value in form if key == "tabs[]"] == ["58", "349"]


def test_publish_ready_article_passes_all_snapshot_tabs(app, monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            assert [tab["backend_tab_id"] for tab in tabs] == [348, 349]
            return DqdOpenDraftResult(
                archive_id=3805555,
                payload={"code": 0, "data": {"archive_id": 3805555}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("tabs[]", "348"), ("tabs[]", "349")],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tabs_by_backend_id = {
            tab["backend_tab_id"]: tab for tab in repo.list_tabs()
        }
        first = tabs_by_backend_id[348]
        second = tabs_by_backend_id[349]
        repo.update_source("marca", tab_ids=[first["id"], second["id"]], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())

    assert result["draft_created"] == 1


def test_publish_ready_article_never_submits_featured_tab(app, monkeypatch):
    """「精选」is legacy-only and must never reach the backend as a column."""

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            assert tabs["backend_tab_id"] == 349
            return DqdOpenDraftResult(
                archive_id=3806666,
                payload={"code": 0, "data": {"archive_id": 3806666}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("tabs[]", "349")],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tabs_by_backend_id = {
            tab["backend_tab_id"]: tab for tab in repo.list_tabs()
        }
        article = repo.upsert_material(_ready_article())["article"]
        repo.assign_article_tabs(
            article["id"],
            [tabs_by_backend_id[58]["id"], tabs_by_backend_id[349]["id"]],
        )
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())
        updated = repo.get_article(article["id"])
        resolved_mode = repo.get_article_publish_mode(article["id"])

    assert result["draft_created"] == 1
    assert updated["status"] == "DRAFT_CREATED"
    assert resolved_mode == 0


@pytest.mark.parametrize(
    ("backend_tab_id", "tab_name"), [(58, "精选"), (12, "法甲")]
)
def test_publish_ready_article_with_only_blocked_tab_is_mapping_blocked(
    app, backend_tab_id, tab_name
):
    with app.app_context():
        blocked = {
            tab["backend_tab_id"]: tab for tab in repo.list_tabs()
        }[backend_tab_id]
        article = repo.upsert_material(_ready_article())["article"]
        repo.assign_article_tabs(article["id"], [blocked["id"]])
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())
        updated = repo.get_article(article["id"])

    assert result["draft_created"] == 0
    assert result["mapping_blocked"] == 1
    assert updated["status"] == "MAPPING_BLOCKED"
    assert tab_name in str(updated["error"] or result["items"][0]["error"])


def test_publish_ready_article_creates_draft_and_saves_archive_id(app, monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, **kwargs):
            # 提交前文章已被 CAS 锁定为 PUBLISHING，提交用的快照取自锁定结果。
            assert article["status"] == "PUBLISHING"
            assert tab["backend_tab_id"]
            return DqdOpenDraftResult(
                archive_id=3801234,
                payload={"code": 0, "data": {"archive_id": 3801234}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("title", article["title_final"]), ("tabs[]", str(tab["backend_tab_id"]))],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())
        updated = repo.get_article(article["id"])

    assert result["draft_created"] == 1
    assert result["failed"] == 0
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3801234


def test_publish_retry_reuses_existing_archive_id_without_remote_call(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3802222)
        repo.transition_status(article["id"], "PUBLISH_FAILED")

        result = create_draft_for_article(_open_config(app.config["DATABASE"]), get_db(), article["id"])
        updated = repo.get_article(article["id"])

    assert result["reused_existing_archive"] is True
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3802222


def test_duplicate_request_recovers_existing_archive_without_overwriting(app, monkeypatch):
    """A concurrent 重复请求 must not overwrite a good DRAFT_CREATED with a failure."""

    class DuplicateAfterConcurrentSuccessClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            # Simulate the other concurrent worker having already persisted the
            # draft before this attempt reaches the backend.
            repo.update_article_backend_refs(article["id"], get_db(), dqd_archive_id=6347480)
            raise DqdOpenClientError(
                "懂球帝创建草稿失败：重复请求",
                status_code=200,
                payload={"code": 0, "data": {"code": 3, "message": "创建失败"}},
                duplicate_request=True,
            )

    monkeypatch.setattr(
        "app.services.publisher.DqdOpenClient", DuplicateAfterConcurrentSuccessClient
    )
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(_open_config(app.config["DATABASE"]), conn, article["id"])
        updated = repo.get_article(article["id"], conn)

    assert result["reused_existing_archive"] is True
    assert result["archive_id"] == 6347480
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 6347480


def test_duplicate_request_without_local_archive_enters_confirmation(app, monkeypatch):
    """Duplicate accepted upstream but no local archive_id yet -> confirm, not fail."""

    class DuplicateClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            raise DqdOpenClientError(
                "懂球帝创建草稿失败：重复请求",
                status_code=200,
                payload={"code": 0, "data": {"code": 3, "message": "创建失败"}},
                duplicate_request=True,
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", DuplicateClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(_open_config(app.config["DATABASE"]), conn, article["id"])
        pending = repo.get_article(article["id"], conn)

    assert pending["status"] != "PUBLISH_FAILED"
    assert pending["status"] == "DRAFT_CONFIRMING"


def test_publish_retry_blocks_legacy_kbs_duplicate_without_remote_call(app, monkeypatch):
    class ForbiddenClient:
        def __init__(self, config):  # pragma: no cover - must not be called
            raise AssertionError("exact source duplicate must be blocked locally")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", ForbiddenClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("kbs", tab_id=tab["id"], enabled=True)
        canonical = repo.upsert_material({
            **_ready_article(),
            "source": "kbs",
            "source_url": "https://news.kbs.co.kr/news/view.do?ncd=8635111",
        })["article"]
        repo.update_article_backend_refs(canonical["id"], dqd_archive_id=6161860)
        repo.transition_status(canonical["id"], "DRAFT_CREATED")
        conn = get_db()
        now = "2026-08-12T00:00:00.000Z"
        with conn:
            duplicate_id = conn.execute(
                """
                INSERT INTO articles
                (source,source_url,duplicate_of_article_id,status,tab_id,created_at,updated_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                ("kbs", "https://news.kbs.co.kr/news/pc/view/view.do?ncd=8635111",
                 canonical["id"], "PUBLISH_FAILED", tab["id"], now, now, now),
            ).lastrowid

        with pytest.raises(ValueError, match=r"#\d+.*archive_id=6161860"):
            create_draft_for_article(_open_config(app.config["DATABASE"]), conn, duplicate_id)
        assert repo.get_article(duplicate_id, conn)["status"] == "SOURCE_DUPLICATE"


def test_publish_worker_skips_legacy_kbs_duplicate_without_remote_call(app, monkeypatch):
    class ForbiddenClient:
        def __init__(self, config):  # pragma: no cover - must not be called
            raise AssertionError("exact source duplicate must be blocked locally")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", ForbiddenClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("kbs", tab_id=tab["id"], enabled=True)
        canonical = repo.upsert_material({
            **_ready_article(),
            "source": "kbs",
            "source_url": "https://news.kbs.co.kr/news/view.do?ncd=8635111",
        })["article"]
        repo.transition_status(canonical["id"], "DRAFT_CREATED")
        conn = get_db()
        now = "2026-08-12T00:00:00.000Z"
        with conn:
            duplicate_id = conn.execute(
                """
                INSERT INTO articles
                (source,source_url,duplicate_of_article_id,status,tab_id,created_at,updated_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                ("kbs", "https://news.kbs.co.kr/news/pc/view/view.do?ncd=8635111",
                 canonical["id"], "READY_TO_PUBLISH", tab["id"], now, now, now),
            ).lastrowid

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), conn)

    assert result["draft_created"] == 0
    assert result["skipped"] == 1
    assert result["duplicate_skipped"] == 1
    assert result["mapping_blocked"] == 0
    assert result["items"][0]["article_id"] == duplicate_id
    assert result["items"][0]["skipped"] is True


def test_publish_ready_articles_recovers_stale_publishing_with_archive_id(app, monkeypatch):
    class ForbiddenClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("stale publishing recovery should not call remote create")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", ForbiddenClient)
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.update_article_backend_refs(article["id"], dqd_archive_id=3803333)
        repo.transition_status(article["id"], "PUBLISHING")
        conn = get_db()
        with conn:
            conn.execute("UPDATE articles SET updated_at=? WHERE id=?", (stale_time, article["id"]))

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), get_db())
        updated = repo.get_article(article["id"])

    assert result["recovered"] == 1
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3803333


def test_publish_account_pool_assigns_account_and_sends_snapshot(app, monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, publish_account=None, **kwargs):
            captured["account"] = publish_account
            return DqdOpenDraftResult(
                archive_id=3804444,
                payload={"code": 0, "data": {"archive_id": 3804444}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[
                    ("user_id", str(publish_account["dqd_user_id"])),
                    ("user_name", publish_account["user_name"]),
                ],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        account = repo.create_publish_account(13421038, "足球实战技巧", connection=conn)
        repo.set_setting("publish_account_pool_enabled", True, conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)
        events = repo.list_article_events(article["id"], conn)

    expected = {"dqd_user_id": 13421038, "user_name": "足球实战技巧"}
    assert captured["account"] == expected
    assert result["publish_user_id"] == 13421038
    assert result["publish_user_name"] == "足球实战技巧"
    assert updated["publish_account_id"] == account["id"]
    assert updated["publish_user_id"] == 13421038
    assert updated["publish_user_name"] == "足球实战技巧"
    assert '"publish_user_id":13421038' in events[-1]["payload_json"]


def test_publish_worker_uses_publish_account_pool(app, monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, publish_account=None, **kwargs):
            captured["account"] = publish_account
            return DqdOpenDraftResult(
                archive_id=3804545,
                payload={"code": 0, "data": {"archive_id": 3804545}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("user_id", str(publish_account["dqd_user_id"]))],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        repo.create_publish_account(13421048, "自动任务账号", connection=conn)
        repo.set_publish_account_pool_enabled(True, conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = publish_ready_articles(
            _open_config(app.config["DATABASE"]), conn
        )
        updated = repo.get_article(article["id"], conn)

    assert result["draft_created"] == 1
    assert result["failed"] == 0
    assert captured["account"] == {
        "dqd_user_id": 13421048,
        "user_name": "自动任务账号",
    }
    assert updated["publish_user_id"] == 13421048


def test_publish_retry_reuses_original_account_after_pool_changes(app, monkeypatch):
    original = {"dqd_user_id": 13421038, "user_name": "首次分配账号"}

    class FailingClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, publish_account=None, **kwargs):
            assert publish_account == original
            raise DqdOpenClientError("模拟创建失败")

    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        account = repo.create_publish_account(
            original["dqd_user_id"], original["user_name"], connection=conn
        )
        repo.set_setting("publish_account_pool_enabled", True, conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        monkeypatch.setattr("app.services.publisher.DqdOpenClient", FailingClient)

        with pytest.raises(DqdOpenClientError, match="模拟创建失败"):
            create_draft_for_article(
                _open_config(app.config["DATABASE"]), conn, article["id"]
            )

        repo.set_publish_account_pool_enabled(False, conn)
        repo.update_publish_account(
            account["id"], conn, user_name="账号已改名", enabled=False
        )

        class SuccessfulClient:
            def __init__(self, config):
                self.config = config

            def create_article(self, article, tab, publish_account=None, **kwargs):
                assert publish_account == original
                return DqdOpenDraftResult(
                    archive_id=3805555,
                    payload={"code": 0, "data": {"archive_id": 3805555}},
                    request_url="https://platform.dongqiudi.com/open/v1/do",
                    form_fields=[("user_id", str(publish_account["dqd_user_id"]))],
                )

        monkeypatch.setattr("app.services.publisher.DqdOpenClient", SuccessfulClient)
        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert result["archive_id"] == 3805555
    assert updated["publish_user_name"] == original["user_name"]
    assert updated["status"] == "DRAFT_CREATED"


def test_enabled_publish_account_pool_blocks_empty_pool_before_remote_call(app, monkeypatch):
    class ForbiddenClient:
        def __init__(self, config):  # pragma: no cover - must not be called
            raise AssertionError("empty account pool must block before remote create")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", ForbiddenClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        repo.set_setting("publish_account_pool_enabled", True, conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        with pytest.raises(ValueError, match="没有可用的发布账号"):
            create_draft_for_article(
                _open_config(app.config["DATABASE"]), conn, article["id"]
            )
        updated = repo.get_article(article["id"], conn)

    assert updated["status"] == "PUBLISH_FAILED"
    assert updated["publish_account_id"] is None
    with app.app_context():
        events = repo.list_article_events(article["id"], get_db())
    assert events[-1]["event_type"] == "DRAFT_RETRY_BLOCKED"
    assert "publish_account_unavailable" in events[-1]["payload_json"]


def test_ambiguous_create_enters_confirmation_and_blocks_normal_retry(app, monkeypatch):
    class AmbiguousClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            raise DqdOpenClientError(
                "创建草稿请求失败: HTTP 502",
                status_code=502,
                diagnostics={"request_id": "upstream-502"},
                result_unknown=True,
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", AmbiguousClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = _open_config(app.config["DATABASE"])

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        pending = repo.get_article(article["id"], conn)
        assert pending["status"] == "DRAFT_CONFIRMING"
        assert pending["draft_next_confirm_at"] is not None
        assert pending["upstream_request_id"] == "upstream-502"
        assert pending["client_request_id"]

        with pytest.raises(ValueError, match="不能重新创建草稿"):
            create_draft_for_article(cfg, conn, article["id"])
        assert len(repo.list_due_draft_confirmations(conn)) == 0


def test_non_idempotent_502_is_retried_once_and_saves_archive_id(app, monkeypatch):
    calls = 0

    class RetriableClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DqdOpenClientError(
                    "创建草稿请求失败: HTTP 502",
                    status_code=502,
                    diagnostics={"request_id": "upstream-first-502"},
                    result_unknown=True,
                )
            return DqdOpenDraftResult(
                archive_id=3812001,
                payload={"code": 0, "data": {"archive_id": 3812001}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
                diagnostics={"request_id": "upstream-retry-success"},
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", RetriableClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = replace(
            _open_config(app.config["DATABASE"]),
            dqd_open_idempotency_enabled=False,
            dqd_open_502_retry_enabled=True,
        )

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        with conn:
            conn.execute(
                "UPDATE articles SET draft_next_confirm_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), article["id"]),
            )
        result = confirm_due_draft_results(cfg, conn)
        updated = repo.get_article(article["id"], conn)

    assert calls == 2
    assert result["confirmed"] == 1
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3812001


def test_non_idempotent_502_retry_stops_after_second_ambiguous_result(app, monkeypatch):
    calls = 0

    class AlwaysAmbiguousClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            nonlocal calls
            calls += 1
            raise DqdOpenClientError(
                "创建草稿请求失败: HTTP 502",
                status_code=502,
                result_unknown=True,
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", AlwaysAmbiguousClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = replace(
            _open_config(app.config["DATABASE"]),
            dqd_open_idempotency_enabled=False,
            dqd_open_502_retry_enabled=True,
        )

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        with conn:
            conn.execute(
                "UPDATE articles SET draft_next_confirm_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), article["id"]),
            )
        result = confirm_due_draft_results(cfg, conn)
        after = confirm_due_draft_results(cfg, conn)
        updated = repo.get_article(article["id"], conn)

    assert calls == 2
    assert result["exhausted"] == 1
    assert after["checked"] == 0
    assert updated["status"] == "DRAFT_CONFIRMING"
    assert updated["draft_next_confirm_at"] is None


def test_idempotent_confirmation_reuses_key_and_saves_archive_id(app, monkeypatch):
    calls = []

    class RetriableClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, client_request_id=None, **kwargs):
            calls.append(client_request_id)
            if len(calls) == 1:
                raise DqdOpenClientError(
                    "创建草稿请求失败: HTTP 504",
                    status_code=504,
                    diagnostics={"request_id": "upstream-timeout"},
                    result_unknown=True,
                )
            return DqdOpenDraftResult(
                archive_id=3811001,
                payload={"code": 0, "data": {"archive_id": 3811001}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
                diagnostics={"request_id": "upstream-confirmed"},
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", RetriableClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = replace(_open_config(app.config["DATABASE"]), dqd_open_idempotency_enabled=True)

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        with conn:
            conn.execute(
                "UPDATE articles SET draft_next_confirm_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), article["id"]),
            )
        result = confirm_due_draft_results(cfg, conn)
        updated = repo.get_article(article["id"], conn)

    assert result["confirmed"] == 1
    assert calls[0] == calls[1]
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3811001
    assert updated["upstream_request_id"] == "upstream-confirmed"


def test_confirmation_budget_stops_after_five_ambiguous_retries(app, monkeypatch):
    calls = 0

    class AlwaysAmbiguousClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, client_request_id=None, **kwargs):
            nonlocal calls
            calls += 1
            raise DqdOpenClientError(
                "创建草稿请求失败: HTTP 502",
                status_code=502,
                result_unknown=True,
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", AlwaysAmbiguousClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = replace(_open_config(app.config["DATABASE"]), dqd_open_idempotency_enabled=True)
        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])

        for _ in range(5):
            with conn:
                conn.execute(
                    "UPDATE articles SET draft_next_confirm_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), article["id"]),
                )
            confirm_due_draft_results(cfg, conn)

        exhausted = repo.get_article(article["id"], conn)
        after = confirm_due_draft_results(cfg, conn)

    assert calls == 6  # initial POST + five same-key confirmations
    assert exhausted["status"] == "DRAFT_CONFIRMING"
    assert exhausted["draft_next_confirm_at"] is None
    assert after["checked"] == 0


def test_source_publish_override_wins_over_tab_mode(app, monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured["status"] = kwargs.get("status")
            return DqdOpenDraftResult(
                archive_id=3813001,
                payload={"code": 0, "data": {"archive_id": 3813001}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=0)
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=1,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert captured["status"] == 1
    assert result["status"] == "PUBLISHED"
    assert updated["publish_mode"] == 1
    assert updated["status"] == "PUBLISHED"


def test_source_follow_tab_uses_tab_mode(app, monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured["status"] = kwargs.get("status")
            return DqdOpenDraftResult(
                archive_id=3813002,
                payload={"code": 0, "data": {"archive_id": 3813002}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=1)
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )

    assert captured["status"] == 1
    assert result["status"] == "PUBLISHED"


def test_tab_mode_switch_applies_before_first_submission(app, monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured.append(kwargs.get("status"))
            return DqdOpenDraftResult(
                archive_id=3813010,
                payload={"code": 0, "data": {"archive_id": 3813010}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=0)
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        repo.update_tab(tab["id"], conn, publish_mode=1)
        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )

    assert captured == [1]
    assert result["status"] == "PUBLISHED"


def test_conflicting_tab_modes_block_submission(app, monkeypatch):
    class MustNotSubmitClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):  # pragma: no cover
            raise AssertionError("conflicting modes must not submit")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", MustNotSubmitClient)
    with app.app_context():
        conn = get_db()
        tabs = repo.list_tabs(conn)[:2]
        repo.update_tab(tabs[0]["id"], conn, publish_mode=0)
        repo.update_tab(tabs[1]["id"], conn, publish_mode=1)
        repo.update_source(
            "marca", conn, tab_ids=[tabs[0]["id"], tabs[1]["id"]],
            enabled=True, publish_mode_override=None,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = publish_ready_articles(
            _open_config(app.config["DATABASE"]), conn
        )
        updated = repo.get_article(article["id"], conn)

    assert result["mapping_blocked"] == 1
    assert result["published"] == 0
    assert result["draft_created"] == 0
    assert updated["status"] == "MAPPING_BLOCKED"
    assert updated["publish_mode"] is None


def test_legacy_archive_recovery_uses_old_source_mode_not_current_tab(app):
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=1)
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode=0, publish_mode_override=None,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "PUBLISH_FAILED", conn)
        repo.update_article_backend_refs(
            article["id"], conn, dqd_archive_id=3813011
        )

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert result["reused_existing_archive"] is True
    assert result["status"] == "DRAFT_CREATED"
    assert updated["publish_mode"] == 0
    assert updated["status"] == "DRAFT_CREATED"


def test_article_snapshot_wins_after_source_override_changes(app, monkeypatch):
    captured = []

    class FailingThenSuccessfulClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured.append(kwargs.get("status"))
            if len(captured) == 1:
                raise DqdOpenClientError("模拟首次失败")
            return DqdOpenDraftResult(
                archive_id=3813003,
                payload={"code": 0, "data": {"archive_id": 3813003}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr(
        "app.services.publisher.DqdOpenClient", FailingThenSuccessfulClient
    )
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=1,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = _open_config(app.config["DATABASE"])

        with pytest.raises(DqdOpenClientError, match="模拟首次失败"):
            create_draft_for_article(cfg, conn, article["id"])
        assert repo.get_article(article["id"], conn)["publish_mode"] == 1

        repo.update_source("marca", conn, publish_mode_override=0)
        result = create_draft_for_article(cfg, conn, article["id"])

    assert captured == [1, 1]
    assert result["status"] == "PUBLISHED"


def test_non_chinese_direct_publish_is_downgraded_to_draft(app, monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured.append(kwargs.get("status"))
            return DqdOpenDraftResult(
                archive_id=3814001,
                payload={"code": 0, "data": {"archive_id": 3814001}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=1)
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        material = _ready_article()
        material["translate_body"] = (
            "<p>The visiting team controlled possession and scored twice "
            "before half time to secure an important league victory.</p>"
        )
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert captured == [0]
    assert result["status"] == "DRAFT_CREATED"
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["publish_mode"] == 0
    assert updated["quality"]["language_check"]["downgraded_to_draft"] is True
    assert updated["quality"]["language_check"]["exceeds_threshold"] is True


def test_exactly_sixty_percent_non_chinese_still_publishes(app, monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured.append(kwargs.get("status"))
            return DqdOpenDraftResult(
                archive_id=3814004,
                payload={"code": 0, "data": {"archive_id": 3814004}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=1,
        )
        material = _ready_article()
        material["translate_body"] = "<p>中文abc</p>"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert captured == [1]
    assert result["status"] == "PUBLISHED"
    assert updated["publish_mode"] == 1
    assert updated["quality"]["language_check"]["non_chinese_ratio"] == 0.6
    assert updated["quality"]["language_check"]["downgraded_to_draft"] is False


def test_non_chinese_body_keeps_configured_draft_mode(app, monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured.append(kwargs.get("status"))
            return DqdOpenDraftResult(
                archive_id=3814002,
                payload={"code": 0, "data": {"archive_id": 3814002}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=0,
        )
        material = _ready_article()
        material["translate_body"] = "<p>Foreign language article body.</p>"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert captured == [0]
    assert result["status"] == "DRAFT_CREATED"
    assert updated["publish_mode"] == 0
    assert updated["quality"]["language_check"]["downgraded_to_draft"] is False


class _GuardClient:
    def __init__(self, config):
        self.config = config

    def create_article(self, article, tabs, **kwargs):
        _GuardClient.captured.append(kwargs.get("status"))
        return DqdOpenDraftResult(
            archive_id=3814100,
            payload={"code": 0, "data": {"archive_id": 3814100}},
            request_url="https://platform.dongqiudi.com/open/v1/do",
            form_fields=[],
        )


def _enable_guard_llm(monkeypatch):
    monkeypatch.setattr(
        "app.services.publisher._make_league_guard_llm", lambda config: object()
    )


def test_ai_guard_upgrades_draft_to_publish_when_belongs(app, monkeypatch):
    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *args, **kwargs: {"belongs": True, "confidence": 0.95, "reason": "属于"},
    )
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(
            tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛",
        )
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert _GuardClient.captured == [1]
    assert result["status"] == "PUBLISHED"
    assert updated["publish_mode"] == 1
    guard = updated["quality"]["league_guard"]
    assert guard["upgraded_to_publish"] is True
    assert guard["effective_publish_mode"] == 1


def test_ai_guard_keeps_draft_when_not_belongs(app, monkeypatch):
    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *args, **kwargs: {"belongs": False, "confidence": 0.99, "reason": "不属于"},
    )
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(
            tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛",
        )
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert _GuardClient.captured == [0]
    assert result["status"] == "DRAFT_CREATED"
    assert updated["publish_mode"] == 0
    guard = updated["quality"]["league_guard"]
    assert guard["upgraded_to_publish"] is False
    assert guard["effective_publish_mode"] == 0


def test_ai_guard_runs_after_publish_claim(app, monkeypatch):
    """归属护栏必须在 CAS 抢占之后运行，且一次提交只调用一次。

    抢占若留在提交前一刻，抓取轮末尾的发布与独立发布 worker 会同时通过状态检查、
    各自调用一次大模型。两次结论可能相反：先落地的决定最终状态，后落地的只覆盖
    quality_json，于是列表会同时出现「已升级直发」与「草稿已创建」。
    """

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    observed = {"statuses": []}

    def _spy(*args, **kwargs):
        observed["statuses"].append(
            repo.get_article(observed["article_id"], observed["conn"])["status"]
        )
        return {"belongs": True, "confidence": 0.95, "reason": "属于"}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _spy)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(
            tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛",
        )
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=None,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        observed["article_id"] = article["id"]
        observed["conn"] = conn

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        event_types = [
            event["event_type"] for event in repo.list_article_events(article["id"], conn)
        ]

    assert observed["statuses"] == ["PUBLISHING"]
    assert event_types.index("PUBLISH_CLAIMED") < event_types.index("DRAFT_RETRY_STARTED")


def test_ai_guard_skips_check_for_source_direct_publish(app, monkeypatch):
    _GuardClient.captured = []
    calls = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)

    def _spy(*args, **kwargs):
        calls.append(args)
        return {"belongs": True, "confidence": 0.99, "reason": "属于"}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _spy)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(
            tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛",
        )
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=1,
        )
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert calls == []
    assert _GuardClient.captured == [1]
    assert result["status"] == "PUBLISHED"
    assert updated["publish_mode"] == 1


def test_ai_guard_cascade_to_j2_when_not_j1(app, monkeypatch):
    """Test cascade fallback: article not belonging to 日职联 but belonging to 日职乙."""
    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)

    call_count = [0]

    def _mock_check(*args, **kwargs):
        call_count[0] += 1
        tab_name = args[2] if len(args) > 2 else kwargs.get("tab_name", "")
        if tab_name == "日职联":
            return {"belongs": False, "confidence": 0.95, "reason": "不属于日职联"}
        elif tab_name == "日职乙":
            return {"belongs": True, "confidence": 0.92, "reason": "属于日职乙"}
        return {"belongs": False, "confidence": 0.99, "reason": "不属于"}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _mock_check)

    with app.app_context():
        conn = get_db()
        j1_tab = repo.get_tab_by_name("日职联", conn)
        j2_tab = repo.get_tab_by_name("日职乙", conn)

        repo.update_tab(
            j1_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛J1",
        )
        repo.update_tab(
            j2_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=False,
            ai_league_guard_definition="日本职业足球联赛J2",
        )
        repo.update_source(
            "marca", conn, tab_id=j1_tab["id"], enabled=True,
            publish_mode_override=None,
        )

        material = _ready_article()
        # The prefilter gates the second call, so the article must carry a J2
        # marker the way a real 日职乙 article would.
        material["translate_title"] = "官方：岐阜后卫平濑大加盟秋田蓝闪电"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert call_count[0] == 2  # Called twice: first for J1, then for J2
    assert _GuardClient.captured == [1]  # Should upgrade to publish
    assert result["status"] == "PUBLISHED"
    assert updated["publish_mode"] == 1
    # The article must be moved into the J2 column, not the J1 one.
    assert j2_tab["id"] in updated["tab_ids"]
    assert j1_tab["id"] not in updated["tab_ids"]
    # Content tags are unrelated to columns and must survive untouched.
    assert updated["channels"] == [11, 12]
    guard = updated["quality"]["league_guard"]
    assert guard["upgraded_to_publish"] is True
    assert guard["effective_publish_mode"] == 1
    assert guard["fallback_tab_name"] == "日职乙"
    assert "属于「日职乙」" in guard["reason"]


def test_ai_guard_cascade_keeps_draft_when_neither_j1_nor_j2(app, monkeypatch):
    """Test cascade fallback: article belongs to neither 日职联 nor 日职乙."""
    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)

    call_count = [0]

    def _mock_check(*args, **kwargs):
        call_count[0] += 1
        # Both return False
        return {"belongs": False, "confidence": 0.95, "reason": "不属于"}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _mock_check)

    with app.app_context():
        conn = get_db()
        j1_tab = repo.get_tab_by_name("日职联", conn)
        j2_tab = repo.get_tab_by_name("日职乙", conn)

        repo.update_tab(
            j1_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛J1",
        )
        repo.update_tab(
            j2_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=False,
            ai_league_guard_definition="日本职业足球联赛J2",
        )
        repo.update_source(
            "marca", conn, tab_id=j1_tab["id"], enabled=True,
            publish_mode_override=None,
        )

        material = _ready_article()
        material["translate_title"] = "官方：岐阜后卫平濑大加盟秋田蓝闪电"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert call_count[0] == 2  # Called twice: first for J1, then for J2
    assert _GuardClient.captured == [0]  # Should stay as draft
    assert result["status"] == "DRAFT_CREATED"
    assert updated["publish_mode"] == 0
    # A rejected cascade must not move the article out of its original column.
    assert j1_tab["id"] in updated["tab_ids"]
    guard = updated["quality"]["league_guard"]
    assert guard["upgraded_to_publish"] is False
    assert guard["effective_publish_mode"] == 0
    # No candidate won, so there is no landing column. Only 日职乙 is seeded
    # here: 亚冠精英 is not in DEFAULT_TABS, and the seed drops unresolvable
    # targets rather than storing a dangling id.
    assert guard["fallback_tab_name"] is None
    assert guard["fallback_candidates"] == ["日职乙"]
    assert "也不属于「日职乙」" in guard["reason"]


def test_ai_guard_cascade_skips_second_call_without_fallback_keywords(app, monkeypatch):
    """The keyword prefilter must save the second call for implausible articles."""
    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)

    call_count = [0]

    def _mock_check(*args, **kwargs):
        call_count[0] += 1
        return {"belongs": False, "confidence": 0.95, "reason": "不属于"}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _mock_check)

    with app.app_context():
        conn = get_db()
        j1_tab = repo.get_tab_by_name("日职联", conn)
        repo.update_tab(
            j1_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛J1",
        )
        repo.update_source(
            "marca", conn, tab_id=j1_tab["id"], enabled=True,
            publish_mode_override=None,
        )
        material = _ready_article()
        # No J2 team name anywhere: the fallback check is not worth a call.
        material["translate_title"] = "官方：曼联发布新赛季第三球衣"
        material["translate_body"] = "<p>曼联公布了新赛季第三球衣的设计与发售安排。</p>"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert call_count[0] == 1  # Only the primary column was checked
    assert _GuardClient.captured == [0]
    guard = updated["quality"]["league_guard"]
    assert guard["upgraded_to_publish"] is False
    assert "特征词" in guard["reason"]


def test_ai_guard_cascade_tries_candidates_in_order(app, monkeypatch):
    """A rejected first candidate must not stop the cascade.

    Mirrors the real 韩K → [亚冠精英, 韩K2联] config: the article is rejected by
    the primary column and by the first candidate, and is only absorbed by the
    second one, which must be the column it lands in.
    """

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)

    asked: list[str] = []

    def _mock_check(*args, **kwargs):
        tab_name = args[2] if len(args) > 2 else kwargs.get("tab_name", "")
        asked.append(tab_name)
        if tab_name == "日职乙":
            return {"belongs": True, "confidence": 0.93, "reason": "属于日职乙"}
        return {"belongs": False, "confidence": 0.95, "reason": f"不属于{tab_name}"}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _mock_check)

    with app.app_context():
        conn = get_db()
        k1_tab = repo.get_tab_by_name("韩K", conn)
        acl_tab = repo.get_tab_by_name("亚冠精英", conn)
        if acl_tab is None:
            acl_tab = repo.create_tab("亚冠精英", 365, True, conn)
        j2_tab = repo.get_tab_by_name("日职乙", conn)

        repo.update_tab(
            k1_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="韩国K联赛1",
            # 亚冠精英 first, 日职乙 second: only the latter accepts the article.
            ai_fallback_tab_ids=[acl_tab["id"], j2_tab["id"]],
        )
        repo.update_tab(
            acl_tab["id"], conn,
            ai_league_guard_definition="亚足联冠军联赛精英",
        )
        repo.update_tab(
            j2_tab["id"], conn,
            ai_league_guard_definition="日本职业足球联赛J2",
        )
        repo.update_source(
            "marca", conn, tab_id=k1_tab["id"], enabled=True,
            publish_mode_override=None,
        )

        material = _ready_article()
        # Carries both an 亚冠 marker and a J2 team, so neither candidate is
        # gated out by the prefilter and the try order is what decides.
        material["translate_title"] = "官方：秋田蓝闪电前锋亚冠首发"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert asked == ["韩K", "亚冠精英", "日职乙"]
    assert _GuardClient.captured == [1]
    guard = updated["quality"]["league_guard"]
    assert guard["fallback_tab_name"] == "日职乙"
    assert guard["fallback_candidates"] == ["亚冠精英", "日职乙"]
    # It must land in the accepting column, not the first candidate.
    assert j2_tab["id"] in updated["tab_ids"]
    assert acl_tab["id"] not in updated["tab_ids"]
    assert k1_tab["id"] not in updated["tab_ids"]
    assert updated["channels"] == [11, 12]


def test_title_dedup_does_not_overwrite_concurrently_published_article(app, monkeypatch):
    """标题查重的结果不得覆盖另一个轮次已经落定的终态。

    查重要调用大模型，期间另一个发布轮次可能已经抢占并发布成功。若用无条件
    写回落终态，线上已发布的文章会被改写成「已取消自动发布」，运营看到的状态
    与懂球帝实际情况相反。
    """

    def _late_dedup(config, connection, current):
        # 模拟另一个发布轮次在查重期间已抢占并发布成功。
        repo.transition_status(int(current["id"]), "PUBLISHED", connection)
        return {
            "outcome": "duplicate",
            "matched": {"id": 999999, "title": "撞车的另一篇"},
            "shared_channels": [11],
            "error": "",
        }

    monkeypatch.setattr("app.services.publisher._title_dedup_gate", _late_dedup)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", conn, tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), conn)
        updated = repo.get_article(article["id"], conn)

    assert updated["status"] == "PUBLISHED"
    assert result["title_duplicate_skipped"] == 0


def test_ai_guard_cascade_submits_reassigned_tab(app, monkeypatch):
    """级联改判后必须把改判后的栏目提交给开放平台。

    栏目列表在解析发布模式之前就取好了，而护栏的级联兜底是在解析过程中改写
    数据库的。若提交前不重新读取，AI 判到「日职乙」的稿子仍会被发到「日职联」。
    """

    submitted = []

    class _CapturingClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            submitted.append(tabs)
            return DqdOpenDraftResult(
                archive_id=3814200,
                payload={"code": 0, "data": {"archive_id": 3814200}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _CapturingClient)
    _enable_guard_llm(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *args, **kwargs: (
            {"belongs": True, "confidence": 0.93, "reason": "属于日职乙"}
            if (args[2] if len(args) > 2 else kwargs.get("tab_name", "")) == "日职乙"
            else {"belongs": False, "confidence": 0.95, "reason": "不属于"}
        ),
    )

    with app.app_context():
        conn = get_db()
        j1_tab = repo.get_tab_by_name("日职联", conn)
        j2_tab = repo.get_tab_by_name("日职乙", conn)
        repo.update_tab(
            j1_tab["id"], conn, publish_mode=0,
            ai_league_guard_enabled=True,
            ai_league_guard_definition="日本职业足球联赛J1",
            ai_fallback_tab_ids=[j2_tab["id"]],
        )
        repo.update_tab(
            j2_tab["id"], conn,
            ai_league_guard_definition="日本职业足球联赛J2",
        )
        repo.update_source(
            "marca", conn, tab_id=j1_tab["id"], enabled=True,
            publish_mode_override=None,
        )

        material = _ready_article()
        # 含 J2 球队名，兜底候选才能通过关键词前置过滤。
        material["translate_title"] = "官方：秋田蓝闪电前锋加盟新潟天鹅"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert len(submitted) == 1
    payload_tabs = submitted[0] if isinstance(submitted[0], list) else [submitted[0]]
    backend_ids = {int(tab["backend_tab_id"]) for tab in payload_tabs}
    assert int(j2_tab["backend_tab_id"]) in backend_ids
    assert int(j1_tab["backend_tab_id"]) not in backend_ids
    assert j2_tab["id"] in updated["tab_ids"]


def test_update_tab_rejects_bad_fallback_candidates(app):
    """The write path must reject self-cascade and unknown candidate ids."""

    with app.app_context():
        conn = get_db()
        j1_tab = repo.get_tab_by_name("日职联", conn)

        with pytest.raises(ValueError):
            repo.update_tab(
                j1_tab["id"], conn, ai_fallback_tab_ids=[j1_tab["id"]]
            )
        with pytest.raises(ValueError):
            repo.update_tab(
                j1_tab["id"], conn, ai_fallback_tab_ids=[999999]
            )


def test_non_chinese_downgrade_stays_draft_during_502_retry(app, monkeypatch):
    captured = []

    class RetriableClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            captured.append(kwargs.get("status"))
            if len(captured) == 1:
                raise DqdOpenClientError(
                    "创建草稿请求失败: HTTP 502",
                    status_code=502,
                    result_unknown=True,
                )
            return DqdOpenDraftResult(
                archive_id=3814003,
                payload={"code": 0, "data": {"archive_id": 3814003}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", RetriableClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True,
            publish_mode_override=1,
        )
        material = _ready_article()
        material["translate_body"] = (
            "<p>The original translation failed and the complete article "
            "remained in another language.</p>"
        )
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = replace(
            _open_config(app.config["DATABASE"]),
            dqd_open_idempotency_enabled=False,
            dqd_open_502_retry_enabled=True,
        )

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        pending = repo.get_article(article["id"], conn)
        assert pending["publish_mode"] == 0
        with conn:
            conn.execute(
                "UPDATE articles SET draft_next_confirm_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), article["id"]),
            )
        result = confirm_due_draft_results(cfg, conn)
        updated = repo.get_article(article["id"], conn)

    assert captured == [0, 0]
    assert result["confirmed"] == 1
    assert updated["publish_mode"] == 0
    assert updated["status"] == "DRAFT_CREATED"


def test_publish_controller_skips_when_publisher_disabled():
    from app.services.publisher import PublishController

    controller = PublishController(
        AppConfig(scheduler_enabled=False, publisher_enabled=False)
    )
    assert controller.start() is False
    assert controller.status()["running"] is False


def test_publish_controller_drains_ready_queue_in_background(app, monkeypatch):
    import time

    from app.services.publisher import PublishController

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tab, **kwargs):
            return DqdOpenDraftResult(
                archive_id=3809999,
                payload={"code": 0, "data": {"archive_id": 3809999}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[("title", article["title_final"])],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", FakeClient)
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_ready_article())["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")
        article_id = article["id"]

    controller = PublishController(_open_config(app.config["DATABASE"]))
    assert controller.start() is True

    deadline = time.monotonic() + 5
    while controller.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.02)

    with app.app_context():
        updated = repo.get_article(article_id)
    result = controller.status()["last_result"]
    assert result is not None
    assert result["draft_created"] == 1
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["dqd_archive_id"] == 3809999
