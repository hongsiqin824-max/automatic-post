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
    ManualReconcileRequired,
    confirm_due_draft_results,
    create_draft_for_article,
    publish_ready_articles,
    recover_stale_publishing_articles,
)
from app.services.quality import LLMCallError


def _future_now(seconds: int = 120) -> str:
    """A timestamp far enough ahead that a fresh confirmation schedule is due."""

    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _open_config(database: str, **overrides: object) -> AppConfig:
    return AppConfig(
        database_path=database,
        scheduler_enabled=False,
        publisher_enabled=True,
        dqd_open_appid="appid-test",
        dqd_open_appsecret="secret-test",
        dqd_open_enname="hongsiqin",
        dqd_open_archive_level="B",
        dqd_open_status=0,
        **overrides,
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


def test_publish_ready_article_with_only_disabled_tab_creates_draft(app, monkeypatch):
    """栏目停用后，挂在它下面的文章只归档不发布。

    ``article_tabs`` 是入库时的快照，栏目之后被停用不会把映射摘掉。停用的语义是
    「这个栏目不再对外更新」，所以文章仍然要提交到该栏目、但只能是草稿——线上
    「NBA」栏目停用后曾经照旧直发过 9 篇。
    """

    submitted: list[int] = []

    class _Client:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            submitted.append(kwargs.get("status"))
            return DqdOpenDraftResult(
                archive_id=3814102,
                payload={"code": 0, "data": {"archive_id": 3814102}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _Client)
    with app.app_context():
        conn = get_db()
        tab = repo.get_tab_by_name("日职联", conn)
        # 栏目本身配的是直接发布，停用必须把它压回草稿。
        repo.update_tab(tab["id"], conn, publish_mode=1, ai_league_guard_enabled=False)
        repo.update_source("marca", conn, tab_id=tab["id"], enabled=True,
                           publish_mode_override=None)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.assign_article_tabs(article["id"], [tab["id"]], conn)
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        # 先建立映射再停用，复现运营的真实操作顺序。
        repo.update_source("marca", conn, tab_id=tab["id"], enabled=False)
        repo.update_tab(tab["id"], conn, enabled=False)

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), conn)
        updated = repo.get_article(article["id"], conn)

    assert result["mapping_blocked"] == 0
    assert result["draft_created"] == 1
    assert submitted == [0]  # 草稿，不是直发
    assert updated["status"] == "DRAFT_CREATED"
    assert updated["publish_mode"] == 0
    # 栏目映射保持不动，方便之后栏目恢复。
    assert tab["id"] in updated["tab_ids"]


def test_disabled_tab_forces_draft_even_when_another_tab_wants_direct(app, monkeypatch):
    """混挂启用+停用栏目时两个栏目都提交，但整篇降级为草稿。

    不能让一个已经关掉的栏目收到直发内容，所以停用是压过栏目模式的硬闸门。
    """

    submitted: list[tuple[int, list[int]]] = []

    class _Client:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            rows = tabs if isinstance(tabs, list) else [tabs]
            submitted.append((
                kwargs.get("status"),
                sorted(int(row["backend_tab_id"]) for row in rows),
            ))
            return DqdOpenDraftResult(
                archive_id=3814101,
                payload={"code": 0, "data": {"archive_id": 3814101}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _Client)
    with app.app_context():
        conn = get_db()
        live = repo.get_tab_by_name("日职联", conn)
        dead = repo.get_tab_by_name("日职乙", conn)
        repo.update_tab(live["id"], conn, publish_mode=1, ai_league_guard_enabled=False)
        repo.update_tab(dead["id"], conn, publish_mode=1, ai_league_guard_enabled=False)
        repo.update_source("marca", conn, tab_id=live["id"], enabled=True,
                           publish_mode_override=None)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.assign_article_tabs(article["id"], [live["id"], dead["id"]], conn)
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        repo.update_tab(dead["id"], conn, enabled=False)

        result = publish_ready_articles(_open_config(app.config["DATABASE"]), conn)

    assert result["mapping_blocked"] == 0
    expected_tabs = sorted(
        [int(live["backend_tab_id"]), int(dead["backend_tab_id"])]
    )
    assert submitted == [(0, expected_tabs)]


def test_disabled_tab_publish_mode_is_ignored_when_resolving(app):
    """停用栏目的 publish_mode 不得参与发布模式决策。

    否则一个停用栏目配成直发就能把文章推成直接发布，或者与启用栏目模式不一致
    时造成 MAPPING_BLOCKED 误拦。
    """

    with app.app_context():
        conn = get_db()
        live = repo.get_tab_by_name("日职联", conn)
        dead = repo.get_tab_by_name("日职乙", conn)
        repo.update_tab(live["id"], conn, publish_mode=0)
        repo.update_tab(dead["id"], conn, publish_mode=1)
        repo.update_source("marca", conn, tab_id=live["id"], enabled=True,
                           publish_mode_override=None)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.assign_article_tabs(article["id"], [live["id"], dead["id"]], conn)
        repo.update_tab(dead["id"], conn, enabled=False)

        resolved = repo.resolve_article_publish_mode(article["id"], conn)

    # 只剩启用栏目的草稿模式，两栏目模式不一致也不再算冲突。
    assert resolved["publish_mode"] == 0
    assert resolved["tab_conflict"] is False


def test_cascade_files_into_disabled_candidate_as_draft(app, monkeypatch):
    """级联判到停用栏目时照样改挂，但只创建草稿。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    asked: list[str] = []

    def _mock_check(*args, **kwargs):
        tab_name = args[2] if len(args) > 2 else kwargs.get("tab_name", "")
        asked.append(tab_name)
        return {"belongs": tab_name == "日职乙", "confidence": 0.95, "reason": tab_name}

    monkeypatch.setattr("app.services.publisher.check_league_membership", _mock_check)

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
        repo.update_tab(j1_tab["id"], conn, ai_fallback_tab_ids=[j2_tab["id"]])
        material = _ready_article()
        material["translate_title"] = "官方：岐阜后卫平濑大加盟秋田蓝闪电"
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        repo.update_tab(j2_tab["id"], conn, enabled=False)

        create_draft_for_article(
            _cascade_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    # 停用不再让候选被跳过——AI 要有机会说出它属于哪儿。
    assert asked == ["日职联", "日职乙"]
    # 栏目改对了，但因为目标栏目停用，只创建草稿。
    assert j2_tab["id"] in updated["tab_ids"]
    assert j1_tab["id"] not in updated["tab_ids"]
    assert _GuardClient.captured == [0]
    assert updated["publish_mode"] == 0
    guard = updated["quality"]["league_guard"]
    assert guard["reassigned_tab_id"] == j2_tab["id"]
    assert guard["reassigned_tab_active"] is False
    assert "该栏目已停用，只创建草稿" in guard["reason"]


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


def test_duplicate_request_without_local_archive_needs_manual_reconcile(app, monkeypatch):
    """Upstream says duplicate but we hold no archive_id: fail visibly, never resend.

    上游没有幂等键也没有查询接口，重发可能被当成新文章，因此只能转人工核对。
    """

    calls = 0

    class DuplicateClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            nonlocal calls
            calls += 1
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
        cfg = _open_config(app.config["DATABASE"])

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        pending = repo.get_article(article["id"], conn)
        # 不排期、不重发：确认流程不得认领它再发一次创建请求。
        after = confirm_due_draft_results(cfg, conn)
        events = repo.list_article_events(article["id"], conn)

    assert calls == 1
    assert after["checked"] == 0
    assert pending["status"] == "PUBLISH_FAILED"
    assert pending["draft_next_confirm_at"] is None
    assert "请到懂球帝后台按标题核对" in str(events[-1]["message"])


def test_unschedulable_result_unknown_fails_instead_of_stalling(app, monkeypatch):
    """No confirmation schedule possible -> land in PUBLISH_FAILED, never stall."""

    class AmbiguousClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            raise DqdOpenClientError(
                "创建草稿请求失败: HTTP 500",
                status_code=500,
                result_unknown=True,
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", AmbiguousClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(_open_config(app.config["DATABASE"]), conn, article["id"])
        updated = repo.get_article(article["id"], conn)

    assert updated["status"] == "PUBLISH_FAILED"
    assert updated["draft_next_confirm_at"] is None


def test_concurrent_claim_counts_as_skipped_not_failed(app, monkeypatch):
    """A rival round already advanced the article: skip quietly, never report failure."""

    class ForbiddenClient:
        def __init__(self, config):  # pragma: no cover - must not be called
            raise AssertionError("already claimed article must not be submitted again")

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", ForbiddenClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        # 标题查重要调大模型，期间另一个发布轮次把同一篇推进到已发布。
        def _rival_round_wins(config, connection, current):
            repo.transition_status(int(current["id"]), "PUBLISHED", connection)
            return None

        monkeypatch.setattr("app.services.publisher._title_dedup_gate", _rival_round_wins)
        result = publish_ready_articles(_open_config(app.config["DATABASE"]), conn)

    assert result["failed"] == 0
    assert result["skipped"] == 1
    assert result["items"][0]["skipped"] is True
    assert "已由其他发布轮次处理" in result["items"][0]["error"]


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
    # 放弃自动确认后不能继续假装「确认中」，否则无排期的文章永远无人认领。
    assert updated["status"] == "PUBLISH_FAILED"
    assert updated["draft_next_confirm_at"] is None


def test_direct_publish_502_is_never_retried(app, monkeypatch):
    """A duplicate direct publish is visible to readers, so 502 must not be retried.

    草稿重复只留在后台且能删，直接发布重复读者直接看到。上游没有幂等键，无法判断
    第一次到底成没成，因此直发遇到 502 一律转人工核对。
    """

    calls = 0

    class AlwaysBadGatewayClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            nonlocal calls
            calls += 1
            raise DqdOpenClientError(
                "直接发布请求失败: HTTP 502",
                status_code=502,
                diagnostics={"request_id": "upstream-direct-502"},
                result_unknown=True,
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", AlwaysBadGatewayClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_tab(tab["id"], conn, publish_mode=1)
        repo.update_source(
            "marca", conn, tab_id=tab["id"], enabled=True, publish_mode_override=None,
        )
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = replace(_open_config(app.config["DATABASE"]), dqd_open_502_retry_enabled=True)

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        updated = repo.get_article(article["id"], conn)
        after = confirm_due_draft_results(cfg, conn)
        events = repo.list_article_events(article["id"], conn)

    assert calls == 1  # 只提交一次，绝不重发
    assert after["checked"] == 0
    assert updated["status"] == "PUBLISH_FAILED"
    assert updated["draft_next_confirm_at"] is None
    assert "避免读者看到重复文章" in str(events[-1]["message"])
    assert events[-1]["payload"]["needs_manual_reconcile"] is True


def test_publishing_timeout_lands_in_manual_state(app):
    """A stuck PUBLISHING article must not park in an unscheduled DRAFT_CONFIRMING."""

    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "PUBLISHING", conn)
        stale = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with conn:
            conn.execute("UPDATE articles SET updated_at=? WHERE id=?", (stale, article["id"]))

        result = recover_stale_publishing_articles(conn)
        updated = repo.get_article(article["id"], conn)
        events = repo.list_article_events(article["id"], conn)

    assert result["timed_out"] == 1
    assert updated["status"] == "PUBLISH_FAILED"
    assert updated["draft_next_confirm_at"] is None
    assert events[-1]["payload"]["needs_manual_reconcile"] is True


def test_repeated_upstream_fetch_must_not_hide_a_stuck_article(app):
    """上游重复推送同一条素材，不得把卡住的稿件藏过回收窗口。

    线上真实故障：拉取周期与 ``PUBLISHING_STALE_SECONDS`` 都是 600 秒，而
    ``upsert_material`` 当时会无条件刷新 ``updated_at``。于是每轮先把卡住稿件的
    ``updated_at`` 刷成「现在」，同一轮的回收再判断「停滞是否超过 600 秒」，永远
    不成立——两篇稿件因此在 PUBLISHING 卡了三个多小时。``updated_at`` 表示本地
    状态停滞多久，只有 last_seen_at 该跟着上游走。
    """

    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "PUBLISHING", conn)
        stale = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with conn:
            conn.execute("UPDATE articles SET updated_at=? WHERE id=?", (stale, article["id"]))

        # 上游又推了一遍同一条素材，和线上每 600 秒一轮的行为一致。
        refetched = repo.upsert_material(material, conn)["article"]
        result = recover_stale_publishing_articles(conn)
        updated = repo.get_article(article["id"], conn)

    assert refetched["updated_at"] == stale
    assert refetched["last_seen_at"] > stale
    assert result["timed_out"] == 1
    assert updated["status"] == "PUBLISH_FAILED"


def test_upsert_still_refreshes_updated_at_before_processing_starts(app):
    """还没开始处理的稿件被重新拉取时，``updated_at`` 仍要跟着内容一起走。

    上一个测试锁的是「在途稿件不受上游刷新影响」，这里锁住边界的另一侧：
    RECEIVED 阶段内容本来就会被覆盖，此时 ``updated_at`` 必须照常更新，否则
    「最近更新」列表会停在入库那一刻。
    """

    with app.app_context():
        conn = get_db()
        material = _ready_article()
        created = repo.upsert_material(material, conn)["article"]
        stale = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with conn:
            conn.execute("UPDATE articles SET updated_at=? WHERE id=?", (stale, created["id"]))

        material["translate_title"] = "马卡：球队改口，安排全部推迟"
        refetched = repo.upsert_material(material, conn)["article"]

    assert refetched["status"] == "RECEIVED"
    assert refetched["title_final"] == "马卡：球队改口，安排全部推迟"
    assert refetched["updated_at"] > stale


def test_manual_retry_blocked_until_reconciled_then_allowed_with_force(app, monkeypatch):
    """Retrying an unconfirmed submission needs an explicit force flag."""

    calls = 0

    class CountingClient:
        def __init__(self, config):
            self.config = config

        def create_article(self, article, tabs, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DqdOpenClientError(
                    "懂球帝创建草稿失败：服务异常",
                    status_code=200,
                    payload={"code": 0, "data": {"code": 5, "message": "服务异常"}},
                    result_unknown=True,
                )
            return DqdOpenDraftResult(
                archive_id=3899001,
                payload={"code": 0, "data": {"archive_id": 3899001}},
                request_url="https://platform.dongqiudi.com/open/v1/do",
                form_fields=[],
                diagnostics={"request_id": "upstream-forced"},
            )

    monkeypatch.setattr("app.services.publisher.DqdOpenClient", CountingClient)
    with app.app_context():
        conn = get_db()
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True, connection=conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        cfg = _open_config(app.config["DATABASE"])

        with pytest.raises(DqdOpenClientError):
            create_draft_for_article(cfg, conn, article["id"])
        assert repo.get_article(article["id"], conn)["status"] == "PUBLISH_FAILED"
        assert repo.draft_needs_manual_reconcile(article["id"], conn) is True

        # 不带 force 的普通重试必须被拦下，避免误点造成重复发布。
        with pytest.raises(ManualReconcileRequired):
            create_draft_for_article(cfg, conn, article["id"])
        assert calls == 1

        # 人工核对完毕后显式强制重试才放行。
        result = create_draft_for_article(cfg, conn, article["id"], force=True)
        updated = repo.get_article(article["id"], conn)

    assert calls == 2
    assert result["archive_id"] == 3899001
    assert updated["status"] == "DRAFT_CREATED"


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


def _cascade_config(database: str) -> AppConfig:
    """Config for the legacy cascade path, which classifier mode supersedes.

    The cascade only runs with the classifier switched off, so these tests pin
    it explicitly rather than relying on the default.
    """

    return _open_config(database, league_guard_classifier_enabled=False)


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
            _cascade_config(app.config["DATABASE"]), conn, article["id"]
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
            _cascade_config(app.config["DATABASE"]), conn, article["id"]
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
            _cascade_config(app.config["DATABASE"]), conn, article["id"]
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
            _cascade_config(app.config["DATABASE"]), conn, article["id"]
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
            _cascade_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert len(submitted) == 1
    payload_tabs = submitted[0] if isinstance(submitted[0], list) else [submitted[0]]
    backend_ids = {int(tab["backend_tab_id"]) for tab in payload_tabs}
    assert int(j2_tab["backend_tab_id"]) in backend_ids
    assert int(j1_tab["backend_tab_id"]) not in backend_ids
    assert j2_tab["id"] in updated["tab_ids"]


def _setup_classifier_tabs(conn, *, fallback_ids: list[int] | None = None):
    """Guard column with an empty cascade list plus a defined target column."""

    j1_tab = repo.get_tab_by_name("日职联", conn)
    j2_tab = repo.get_tab_by_name("日职乙", conn)
    repo.update_tab(
        j1_tab["id"], conn, publish_mode=0,
        ai_league_guard_enabled=True,
        ai_league_guard_definition="日本职业足球联赛J1",
        ai_fallback_tab_ids=fallback_ids if fallback_ids is not None else [],
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
    return j1_tab, j2_tab


def _reject_guard_column(monkeypatch):
    """The guard column always says "does not belong", so the classifier runs."""

    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *args, **kwargs: {
            "belongs": False, "confidence": 0.99, "reason": "不属于日职联",
        },
    )


def test_classifier_reassigns_without_configured_fallback_candidates(app, monkeypatch):
    """分类模式的核心收益：护栏栏目没配候选也能改挂。

    线上近半数开了护栏的栏目 ``ai_fallback_tab_ids`` 为空，级联因此无处可去——
    AI 正确判出「不属于」之后文章仍然滞留草稿。分类模式一次调用就在全部候选栏目
    里选，覆盖面与人工有没有配过候选无关。
    """

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    seen: dict[str, object] = {}

    def _classify(title, body, candidates, llm):
        seen["names"] = [str(row.get("name") or "") for row in candidates]
        return {"tab_id": seen["target_id"], "confidence": 0.95, "reason": "属于日职乙"}

    monkeypatch.setattr("app.services.publisher.classify_article_tab", _classify)

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
        seen["target_id"] = j2_tab["id"]
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        result = create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)
        j1_after = repo.get_tab(j1_tab["id"], conn)

    # 前提成立：护栏栏目确实一个级联候选都没配。
    assert repo.tab_fallback_tab_ids(j1_after) == []
    # 候选集里不该出现护栏栏目自己（已经问过了），也不该出现通用「精选」。
    assert "日职联" not in seen["names"]
    assert "精选" not in seen["names"]
    assert "日职乙" in seen["names"]
    assert _GuardClient.captured == [1]
    assert result["status"] == "PUBLISHED"
    assert updated["publish_mode"] == 1
    assert j2_tab["id"] in updated["tab_ids"]
    assert j1_tab["id"] not in updated["tab_ids"]
    # Content tags are unrelated to columns and must survive untouched.
    assert updated["channels"] == [11, 12]
    guard = updated["quality"]["league_guard"]
    assert guard["classifier_used"] is True
    assert guard["reassigned_tab_id"] == j2_tab["id"]
    assert guard["fallback_tab_name"] == "日职乙"
    # 改挂成功后 tab_name 指向落点，被校验的原栏目单独留一列，否则按栏目统计
    # 护栏效果时会把改挂走的文章算到目标栏目名下。
    assert guard["tab_name"] == "日职乙"
    assert guard["guard_tab_name"] == "日职联"
    assert guard["guard_tab_id"] == j1_tab["id"]


def test_classifier_files_into_disabled_column_as_draft(app, monkeypatch):
    """AI 判到停用栏目时改挂过去，但只创建草稿。

    这是 enabled 的统一语义：停用 = 该栏目不再对外更新，所以文章要归到正确的
    栏目下等栏目恢复，而不是留在错误的栏目里、也不是无家可归。
    """

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    seen: dict[str, object] = {}

    def _classify(title, body, candidates, llm):
        seen["names"] = [str(row.get("name") or "") for row in candidates]
        return {
            "tab_id": seen["target_id"], "confidence": 0.95,
            "reason": "属于日职乙", "actual_competition": "日本J2联赛",
        }

    monkeypatch.setattr("app.services.publisher.classify_article_tab", _classify)

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
        seen["target_id"] = j2_tab["id"]
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        repo.update_tab(j2_tab["id"], conn, enabled=False)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    # 停用栏目必须留在候选里，否则 AI 根本没机会判到它。
    assert "日职乙" in seen["names"]
    # 栏目改对了……
    assert j2_tab["id"] in updated["tab_ids"]
    assert j1_tab["id"] not in updated["tab_ids"]
    # ……但只创建草稿，即使 reassign 模式是 always_direct。
    assert _GuardClient.captured == [0]
    assert updated["publish_mode"] == 0
    guard = updated["quality"]["league_guard"]
    assert guard["reassigned_tab_id"] == j2_tab["id"]
    assert guard["reassigned_tab_active"] is False
    assert guard["actual_competition"] == "日本J2联赛"
    assert "该栏目已停用，只创建草稿" in guard["reason"]


def test_classifier_records_competition_when_no_column_matches(app, monkeypatch):
    """判 null 时也要把 AI 认为的真实赛事记下来，用于决定该建哪些栏目。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.classify_article_tab",
        lambda *a, **k: {
            "tab_id": None, "confidence": 0.96,
            "reason": "亚运会女足，现有栏目均不匹配",
            "actual_competition": "亚运会",
        },
    )

    with app.app_context():
        conn = get_db()
        j1_tab, _ = _setup_classifier_tabs(conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    # 判不出归属：保持原栏目 + 创建草稿。
    assert _GuardClient.captured == [0]
    assert updated["publish_mode"] == 0
    assert j1_tab["id"] in updated["tab_ids"]
    guard = updated["quality"]["league_guard"]
    assert guard["reassigned_tab_id"] is None
    # 赛事名称照样落库，一条 SQL 就能聚合出「亚运会 N 篇」。
    assert guard["actual_competition"] == "亚运会"


def test_guard_stores_normalized_competition_and_keeps_the_raw_value(app, monkeypatch):
    """同一赛事的多种写法要在落库时收敛，否则聚合会把它拆成好几行。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.classify_article_tab",
        lambda *a, **k: {
            "tab_id": None, "confidence": 0.96,
            "reason": "解放者杯，现有栏目均不匹配",
            "actual_competition": "2026赛季南美解放者杯半决赛",
        },
    )

    with app.app_context():
        conn = get_db()
        _setup_classifier_tabs(conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        guard = repo.get_article(article["id"], conn)["quality"]["league_guard"]

    assert guard["actual_competition"] == "解放者杯"
    # 原文另存：用来回溯模型实际写了什么，也是补别名表的依据。
    assert guard["actual_competition_raw"] == "2026赛季南美解放者杯半决赛"


def test_guard_does_not_publish_into_its_own_disabled_column(app, monkeypatch):
    """AI 确认属于当前栏目，但该栏目已停用时也不能升级为直发。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *a, **k: {"belongs": True, "confidence": 0.99, "reason": "确实属于本栏目"},
    )

    with app.app_context():
        conn = get_db()
        j1_tab, _ = _setup_classifier_tabs(conn)
        article = repo.upsert_material(_ready_article(), conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
        # 栏目被启用来源引用时不允许停用，先按运营的真实顺序解绑来源。
        repo.update_source("marca", conn, tab_id=j1_tab["id"], enabled=False)
        repo.update_tab(j1_tab["id"], conn, enabled=False)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert _GuardClient.captured == [0]
    assert updated["publish_mode"] == 0


def test_classifier_keeps_draft_when_no_column_matches(app, monkeypatch):
    """判 null（非赛事内容）时维持草稿，且不得改动栏目。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.classify_article_tab",
        lambda *args, **kwargs: {
            "tab_id": None, "confidence": 0.97, "reason": "转会新闻，不属于任何赛事栏目",
        },
    )

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
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
    assert j1_tab["id"] in updated["tab_ids"]
    assert j2_tab["id"] not in updated["tab_ids"]
    guard = updated["quality"]["league_guard"]
    assert guard["upgraded_to_publish"] is False
    assert guard["reassigned_tab_id"] is None


def test_classifier_below_confidence_must_not_reassign(app, monkeypatch):
    """置信度不足时既不能发布，也不能改挂——错挂比留草稿更糟。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    target: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.publisher.classify_article_tab",
        lambda *args, **kwargs: {
            "tab_id": target["id"], "confidence": 0.5, "reason": "可能是日职乙",
        },
    )

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
        target["id"] = j2_tab["id"]
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert _GuardClient.captured == [0]
    assert updated["publish_mode"] == 0
    assert j1_tab["id"] in updated["tab_ids"]
    assert j2_tab["id"] not in updated["tab_ids"]
    assert "置信度不足" in updated["quality"]["league_guard"]["reason"]


def test_classifier_failure_keeps_draft(app, monkeypatch):
    """分类调用失败按 fail closed 处理：维持草稿，栏目不动。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)

    def _boom(*args, **kwargs):
        raise LLMCallError("timeout", category="timeout", retryable=True)

    monkeypatch.setattr("app.services.publisher.classify_article_tab", _boom)

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
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
    assert j1_tab["id"] in updated["tab_ids"]
    assert j2_tab["id"] not in updated["tab_ids"]
    assert "分类调用失败" in updated["quality"]["league_guard"]["reason"]


def test_classifier_target_mode_reassigns_but_keeps_draft(app, monkeypatch):
    """``target`` 模式下栏目纠正到位，但发布模式沿用目标栏目（这里是草稿）。

    改挂与升级在实现上是解耦的：目标栏目本身配成草稿时，文章要挂对栏目却不该
    被发出去。
    """

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    _reject_guard_column(monkeypatch)
    target: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.publisher.classify_article_tab",
        lambda *args, **kwargs: {
            "tab_id": target["id"], "confidence": 0.95, "reason": "属于日职乙",
        },
    )

    with app.app_context():
        conn = get_db()
        j1_tab, j2_tab = _setup_classifier_tabs(conn)
        target["id"] = j2_tab["id"]
        material = _ready_article()
        article = repo.upsert_material(material, conn)["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)

        create_draft_for_article(
            _open_config(
                app.config["DATABASE"],
                league_guard_reassign_publish_mode="target",
            ),
            conn,
            article["id"],
        )
        updated = repo.get_article(article["id"], conn)

    # 栏目改对了……
    assert j2_tab["id"] in updated["tab_ids"]
    assert j1_tab["id"] not in updated["tab_ids"]
    # ……但目标栏目是草稿配置，所以没有发布。
    assert _GuardClient.captured == [0]
    assert updated["publish_mode"] == 0
    guard = updated["quality"]["league_guard"]
    assert guard["reassigned_tab_id"] == j2_tab["id"]
    assert guard["effective_publish_mode"] == 0


def test_guard_candidate_tabs_keep_disabled_but_drop_generic_and_undefined(app):
    """候选集收所有写了判定说明的赛事栏目，包括已停用的。

    停用栏目留在候选里是有意的：AI 该有机会说出「这是 CBA 的稿子」，即使 CBA
    暂停了——文章随后按 CBA 归档为草稿，而不是无家可归。
    """

    with app.app_context():
        conn = get_db()
        # 通用「精选」不是赛事栏目：每篇文章都挂着它，放进候选等于允许「改挂到
        # 原地」。它平时没有判定说明，所以这里先给它填一份，确保排除它靠的是
        # backend_tab_id 而不是「恰好没定义」。
        generic = repo.get_tab_by_backend_id(repo.GENERIC_SOURCE_BACKEND_TAB_ID, conn)
        repo.update_tab(
            generic["id"], conn,
            ai_league_guard_definition="精选栏目收录全部内容",
        )

        repo.create_tab("无定义栏目", 9901, True, conn)
        repo.create_tab(
            "停用栏目", 9902, False, conn,
            ai_league_guard_definition="停用栏目的判定说明",
        )
        names = {row["name"] for row in repo.list_guard_candidate_tabs(conn)}

    assert generic["name"] not in names
    # 没有判定说明，模型除了栏目名无据可依。
    assert "无定义栏目" not in names
    # 停用栏目仍是合法的归档目标。
    assert "停用栏目" in names


def _routed_article(conn, marker_code: str, tab_id: int, *, ai_guard_enabled: bool):
    """Article carrying a league short code resolved through its own rule."""

    rule = repo.create_event_tab_rule(
        "league", marker_code, tab_id, True, conn,
        ai_guard_enabled=ai_guard_enabled,
    )
    material = _ready_article()
    material["source_url"] = f"https://example.com/routed/{marker_code}"
    material["user_name"] = f"nikkan:fb.{marker_code}.someteam"
    article = repo.upsert_material(material, conn)["article"]
    repo.transition_status(article["id"], "READY_TO_PUBLISH", conn)
    return rule, repo.get_article(article["id"], conn)


def test_routed_league_skips_guard_until_its_rule_opts_in(app, monkeypatch):
    """有 league 短码默认跳过 AI 校验——短码本身就代表栏目已知。"""

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *a, **k: (calls.append("membership"), {
            "belongs": False, "confidence": 0.99, "reason": "x",
        })[1],
    )

    with app.app_context():
        conn = get_db()
        guard_tab, _ = _setup_classifier_tabs(conn)
        rule, article = _routed_article(
            conn, "catchall", guard_tab["id"], ai_guard_enabled=False
        )
        assert article["route_league"] == "catchall"
        assert article["route_rule_id"] == rule["id"]

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )

    assert calls == []
    assert _GuardClient.captured == [0]


def test_routed_league_runs_guard_when_rule_opts_in(app, monkeypatch):
    """开了规则上的 AI 归属校验后，兜底短码的文章也会被重新判栏目并改挂。

    intl 这类「其他国际」短码的目标栏目只是猜测，值得再判一次；开关放在规则上，
    所以给一个兜底短码开启不会让其他所有已路由文章都开始花调用。
    """

    _GuardClient.captured = []
    monkeypatch.setattr("app.services.publisher.DqdOpenClient", _GuardClient)
    _enable_guard_llm(monkeypatch)
    monkeypatch.setattr(
        "app.services.publisher.check_league_membership",
        lambda *a, **k: {"belongs": False, "confidence": 0.99, "reason": "不属于兜底栏目"},
    )
    target: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.publisher.classify_article_tab",
        lambda *a, **k: {
            "tab_id": target["id"], "confidence": 0.95, "reason": "属于日职乙",
        },
    )

    with app.app_context():
        conn = get_db()
        guard_tab, real_tab = _setup_classifier_tabs(conn)
        target["id"] = real_tab["id"]
        _, article = _routed_article(
            conn, "catchall", guard_tab["id"], ai_guard_enabled=True
        )

        create_draft_for_article(
            _open_config(app.config["DATABASE"]), conn, article["id"]
        )
        updated = repo.get_article(article["id"], conn)

    assert _GuardClient.captured == [1]
    assert updated["publish_mode"] == 1
    assert real_tab["id"] in updated["tab_ids"]
    assert guard_tab["id"] not in updated["tab_ids"]
    guard = updated["quality"]["league_guard"]
    assert guard["classifier_used"] is True
    assert guard["reassigned_tab_id"] == real_tab["id"]


def test_event_tab_rule_ai_guard_defaults_off(app):
    """新规则默认不开 AI 校验，升级存量库也不会突然开始花调用。"""

    with app.app_context():
        conn = get_db()
        tab = repo.get_tab_by_name("日职联", conn)
        rule = repo.create_event_tab_rule("league", "freshcode", tab["id"], True, conn)

        assert int(rule["ai_guard_enabled"]) == 0
        assert repo.event_tab_rule_allows_ai_guard(rule["id"], conn) is False

        toggled = repo.update_event_tab_rule(rule["id"], conn, ai_guard_enabled=True)
        assert int(toggled["ai_guard_enabled"]) == 1
        assert repo.event_tab_rule_allows_ai_guard(rule["id"], conn) is True

        # 只改别的字段时开关必须保持原值。
        kept = repo.update_event_tab_rule(rule["id"], conn, enabled=False)
        assert int(kept["ai_guard_enabled"]) == 1

        # 缺失或未知的规则 id 一律按未开启处理。
        assert repo.event_tab_rule_allows_ai_guard(None, conn) is False
        assert repo.event_tab_rule_allows_ai_guard("", conn) is False
        assert repo.event_tab_rule_allows_ai_guard(999999, conn) is False


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
