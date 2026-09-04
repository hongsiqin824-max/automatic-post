from __future__ import annotations

import sqlite3
import threading

import pytest

from app import repository as repo
from app.db import _connect, get_db, init_db


def _material(**overrides):
    value = {
        "source": "marca",
        "source_url": "https://example.com/article/1",
        "translate_title": "马卡：主队在联赛中取得关键胜利",
        "translate_body": "<p>这是一篇完整的体育新闻正文，包含足够的信息用于自动质量检测和后续审核流程。</p>",
        "archive_id": 0,
        "dqd_litpic": "/fastdfs8/example.jpg",
        "channels": [11, 22, 11],
    }
    value.update(overrides)
    return value


def test_catalog_is_seeded_and_disabled_by_default(app):
    with app.app_context():
        assert len(repo.list_tabs()) == 35
        sources = repo.list_sources()
        assert len(sources) == 85
        assert not any(row["enabled"] for row in sources)


def test_source_must_have_tab_before_enable(app):
    with app.app_context():
        with pytest.raises(ValueError, match="assigned to a tab"):
            repo.update_source("marca", enabled=True)
        tab = repo.list_tabs(include_disabled=False)[0]
        source = repo.update_source("marca", tab_id=tab["id"], enabled=True)
        assert source["enabled"] == 1
        assert source["tab_id"] == tab["id"]


def test_source_publish_mode_defaults_to_draft(app):
    with app.app_context():
        sources = repo.list_sources()
        marca = [s for s in sources if s["code"] == "marca"][0]
        assert marca["publish_mode"] == 0


def test_update_source_publish_mode(app):
    with app.app_context():
        tab = repo.list_tabs(include_disabled=False)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)

        updated = repo.update_source("marca", publish_mode=1)
        assert updated["publish_mode"] == 1

        updated = repo.update_source("marca", publish_mode=0)
        assert updated["publish_mode"] == 0

        with pytest.raises(ValueError, match="publish_mode must be 0"):
            repo.update_source("marca", publish_mode=2)
        with pytest.raises(ValueError, match="publish_mode must be 0"):
            repo.update_source("marca", publish_mode=True)


def test_source_supports_ordered_multiple_tabs(app):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:3]
        selected = [tabs[1]["id"], tabs[0]["id"], tabs[2]["id"], tabs[1]["id"]]

        source = repo.update_source("marca", tab_ids=selected, enabled=True)

        assert source["tab_ids"] == [tabs[1]["id"], tabs[0]["id"], tabs[2]["id"]]
        assert source["backend_tab_ids"] == [
            tabs[1]["backend_tab_id"], tabs[0]["backend_tab_id"], tabs[2]["backend_tab_id"]
        ]
        assert source["tab_id"] == tabs[1]["id"]
        assert repo.get_source_tabs("marca") == source["tabs"]
        assert [row["code"] for row in repo.list_sources(tab_id=tabs[0]["id"])] == ["marca"]


def test_create_source_adds_code_name_and_tab_mappings(app):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:2]

        source = repo.create_source(
            "new-league",
            "新联赛来源",
            tab_ids=[tabs[1]["id"], tabs[0]["id"], tabs[1]["id"]],
        )

        assert source["code"] == "new-league"
        assert source["display_name"] == "新联赛来源"
        assert source["enabled"] == 0
        assert source["tab_ids"] == [tabs[1]["id"], tabs[0]["id"]]


def test_create_enabled_source_requires_enabled_tab(app):
    with app.app_context():
        with pytest.raises(ValueError, match="at least one enabled tab"):
            repo.create_source("new-league", "新联赛来源", enabled=True)


def test_enabled_source_requires_at_least_one_enabled_tab(app):
    with app.app_context():
        enabled_tab = repo.list_tabs(include_disabled=False)[0]
        disabled_tab = repo.create_tab("停用栏目", 987654, enabled=False)

        with pytest.raises(ValueError, match="enabled tab"):
            repo.update_source("marca", tab_ids=[enabled_tab["id"], disabled_tab["id"]], enabled=True)

        repo.update_source("marca", tab_ids=[enabled_tab["id"]], enabled=True)
        with pytest.raises(ValueError, match="at least one enabled tab"):
            repo.set_source_tabs("marca", [])


def test_material_unique_key_and_external_ids_are_separate(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        first = repo.upsert_material(_material())
        second = repo.upsert_material(_material(archive_id=77))
        assert first["created"] is True
        assert second["created"] is False
        assert first["article"]["id"] == second["article"]["id"]
        assert second["article"]["upstream_archive_id"] == 77
        assert second["article"]["dqd_archive_id"] is None
        assert repo.count_articles() == 1
        assert second["article"]["channels"] == [11, 22]


def test_list_articles_filters_by_created_time_newest_first(app):
    with app.app_context():
        tab = repo.list_tabs(include_disabled=False)[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        older = repo.upsert_material(_material(
            source_url="https://example.com/sort/older",
            translate_title="筛选排序测试旧文章",
        ))["article"]
        newer = repo.upsert_material(_material(
            source_url="https://example.com/sort/newer",
            translate_title="筛选排序测试新文章",
        ))["article"]
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE articles SET created_at=?, updated_at=? WHERE id=?",
                ("2026-08-25T00:00:00.000Z", "2026-08-25T03:00:00.000Z", older["id"]),
            )
            conn.execute(
                "UPDATE articles SET created_at=?, updated_at=? WHERE id=?",
                ("2026-08-25T02:00:00.000Z", "2026-08-25T01:00:00.000Z", newer["id"]),
            )

        rows = repo.list_articles(
            status=older["status"],
            source="marca",
            tab_id=tab["id"],
        )

        assert [row["id"] for row in rows] == [newer["id"], older["id"]]


def test_list_articles_searches_title_and_source_url(app):
    with app.app_context():
        title_article = repo.upsert_material(_material(
            source_url="https://example.com/search/title",
            translate_title="按标题搜索的测试文章",
        ))["article"]
        url_article = repo.upsert_material(_material(
            source_url="https://example.com/search/source-url",
            translate_title="另一篇搜索测试文章",
        ))["article"]

        title_rows = repo.list_articles(query="按标题搜索")
        url_rows = repo.list_articles(query="search/source-url")

        assert [row["id"] for row in title_rows] == [title_article["id"]]
        assert [row["id"] for row in url_rows] == [url_article["id"]]


@pytest.mark.parametrize("reverse", [False, True])
def test_kbs_ncd_deduplicates_pc_and_standard_urls(app, reverse):
    urls = [
        "https://news.kbs.co.kr/news/pc/view/view.do?ncd=8635111",
        "https://news.kbs.co.kr/news/view.do?ncd=8635111",
    ]
    if reverse:
        urls.reverse()
    with app.app_context():
        first = repo.upsert_material(_material(source="kbs", source_url=urls[0]))
        second = repo.upsert_material(_material(source="kbs", source_url=urls[1]))

        assert first["created"] is True
        assert second["created"] is False
        assert second["article"]["id"] == first["article"]["id"]
        assert second["article"]["origin_key"] == "kbs:ncd:8635111"
        assert repo.count_articles() == 1


def test_article_keeps_source_tab_snapshot_after_source_changes(app):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:3]
        initial_ids = [tabs[0]["id"], tabs[1]["id"]]
        repo.update_source("marca", tab_ids=initial_ids, enabled=True)
        first = repo.upsert_material(_material())["article"]

        repo.update_source("marca", tab_ids=[tabs[2]["id"]])
        repeated = repo.upsert_material(_material(title="重复获取后的标题"))["article"]
        second = repo.upsert_material(_material(
            source_url="https://example.com/article/2",
            translate_title="另一篇文章",
        ))["article"]

        assert first["tab_ids"] == initial_ids
        assert repeated["tab_ids"] == initial_ids
        assert repeated["tab_id"] == initial_ids[0]
        assert second["tab_ids"] == [tabs[2]["id"]]
        assert repo.count_articles(tab_id=tabs[1]["id"]) == 1
        assert [item["id"] for item in repo.list_articles(tab_id=tabs[1]["id"])] == [first["id"]]


def test_repeated_unmapped_article_does_not_gain_new_source_tabs(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        tab = repo.list_tabs(include_disabled=False)[0]
        repo.update_source("marca", tab_ids=[tab["id"]], enabled=True)

        repeated = repo.upsert_material(_material(translate_title="再次获取"))["article"]

        assert article["tab_ids"] == []
        assert repeated["tab_ids"] == []
        assert repeated["tab_id"] is None


def test_article_tabs_can_be_replaced_explicitly(app):
    with app.app_context():
        tabs = repo.list_tabs(include_disabled=False)[:3]
        repo.update_source("marca", tab_ids=[tabs[0]["id"]], enabled=True)
        article = repo.upsert_material(_material())["article"]

        updated = repo.assign_article_tabs(
            article["id"], [tabs[2]["id"], tabs[1]["id"], tabs[2]["id"]]
        )

        assert updated["tab_ids"] == [tabs[2]["id"], tabs[1]["id"]]
        assert updated["tab_id"] == tabs[2]["id"]
        assert repo.get_article_tabs(article["id"]) == updated["tabs"]


def test_init_db_backfills_legacy_tab_fields_idempotently(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    init_db(database)
    conn = _connect(database)
    try:
        now = "2026-08-12T00:00:00.000Z"
        tab_id = conn.execute(
            "INSERT INTO tabs (backend_tab_id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (58, "彩经", now, now),
        ).lastrowid
        source_id = conn.execute(
            "INSERT INTO sources (code,display_name,tab_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("legacy", "旧来源", tab_id, now, now),
        ).lastrowid
        article_id = conn.execute(
            "INSERT INTO articles (source,source_url,tab_id,created_at,updated_at,last_seen_at) VALUES (?,?,?,?,?,?)",
            ("legacy", "https://example.com/legacy", tab_id, now, now, now),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    init_db(database)
    init_db(database)
    conn = _connect(database)
    try:
        source_rows = conn.execute(
            "SELECT source_id,tab_id,sort_order FROM source_tabs WHERE source_id=?", (source_id,)
        ).fetchall()
        article_rows = conn.execute(
            "SELECT article_id,tab_id,sort_order FROM article_tabs WHERE article_id=?", (article_id,)
        ).fetchall()
        assert [tuple(row) for row in source_rows] == [(source_id, tab_id, 0)]
        assert [tuple(row) for row in article_rows] == [(article_id, tab_id, 0)]
    finally:
        conn.close()


def test_init_db_links_legacy_kbs_duplicates_to_drafted_canonical(tmp_path):
    database = tmp_path / "legacy-kbs.sqlite3"
    init_db(database)
    conn = _connect(database)
    try:
        now = "2026-08-12T00:00:00.000Z"
        failed_id = conn.execute(
            """
            INSERT INTO articles
            (source,source_url,status,created_at,updated_at,last_seen_at)
            VALUES (?,?,?,?,?,?)
            """,
            ("kbs", "https://news.kbs.co.kr/news/pc/view/view.do?ncd=8635111",
             "PUBLISH_FAILED", now, now, now),
        ).lastrowid
        drafted_id = conn.execute(
            """
            INSERT INTO articles
            (source,source_url,status,dqd_archive_id,created_at,updated_at,last_seen_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            ("kbs", "https://news.kbs.co.kr/news/view.do?ncd=8635111",
             "DRAFT_CREATED", 6161860, now, now, now),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    init_db(database)
    init_db(database)
    conn = _connect(database)
    try:
        failed = conn.execute(
            "SELECT origin_key,duplicate_of_article_id,status FROM articles WHERE id=?", (failed_id,)
        ).fetchone()
        drafted = conn.execute(
            "SELECT origin_key,duplicate_of_article_id FROM articles WHERE id=?", (drafted_id,)
        ).fetchone()
        assert failed["origin_key"] is None
        assert failed["duplicate_of_article_id"] == drafted_id
        assert failed["status"] == "SOURCE_DUPLICATE"
        assert drafted["origin_key"] == "kbs:ncd:8635111"
        assert drafted["duplicate_of_article_id"] is None
        events = conn.execute(
            "SELECT event_type FROM article_events WHERE article_id=?", (failed_id,)
        ).fetchall()
        assert [row["event_type"] for row in events] == ["SOURCE_DUPLICATE_DETECTED"]
    finally:
        conn.close()


def test_publish_account_crud_and_validation(app):
    with app.app_context():
        assert repo.list_publish_accounts() == []
        first = repo.create_publish_account(10001, "账号一")
        second = repo.create_publish_account(10002, "账号二", enabled=False)

        assert repo.get_publish_account(first["id"])["dqd_user_id"] == 10001
        assert [item["id"] for item in repo.list_publish_accounts(include_disabled=False)] == [
            first["id"]
        ]

        updated = repo.update_publish_account(
            second["id"], dqd_user_id=10003, user_name="账号二（更新）", enabled=True
        )
        assert updated["dqd_user_id"] == 10003
        assert updated["user_name"] == "账号二（更新）"
        assert updated["enabled"] == 1
        assert updated["assignment_count"] == 0

        with pytest.raises(ValueError, match="positive integer"):
            repo.create_publish_account(0, "无效账号")
        with pytest.raises(ValueError, match="user_name is required"):
            repo.create_publish_account(10004, "  ")
        with pytest.raises(ValueError, match="boolean"):
            repo.create_publish_account(10004, "无效开关", enabled="false")
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_publish_account(10001, "重复账号")


def test_assign_publish_account_requires_an_enabled_account(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        repo.create_publish_account(11001, "停用账号", enabled=False)

        with pytest.raises(ValueError, match="没有可用的发布账号"):
            repo.assign_publish_account(article["id"])

        unchanged = repo.get_article(article["id"])
        assert unchanged["publish_account_id"] is None
        assert unchanged["publish_user_id"] is None
        assert unchanged["publish_user_name"] is None
        assert unchanged["publish_account_assigned_at"] is None


def test_publish_account_assignment_is_sticky_and_snapshotted(app):
    with app.app_context():
        account = repo.create_publish_account(12001, "原账号名")
        article = repo.upsert_material(_material())["article"]

        first = repo.assign_publish_account(article["id"])
        repo.update_publish_account(account["id"], user_name="新账号名", enabled=False)
        second = repo.assign_publish_account(article["id"])

        assert first["publish_account_id"] == account["id"]
        assert second["publish_account_id"] == account["id"]
        assert second["publish_user_id"] == 12001
        assert second["publish_user_name"] == "原账号名"
        assert second["publish_account_assigned_at"] == first["publish_account_assigned_at"]
        assert repo.get_publish_account(account["id"])["assignment_count"] == 1


def test_publish_account_assignment_prefers_the_least_used_account(app):
    with app.app_context():
        first_account = repo.create_publish_account(13001, "先加入账号")
        first_article = repo.upsert_material(_material())["article"]
        first_assignment = repo.assign_publish_account(first_article["id"])
        assert first_assignment["publish_account_id"] == first_account["id"]

        second_account = repo.create_publish_account(13002, "后加入账号")
        second_article = repo.upsert_material(_material(
            source_url="https://example.com/article/least-used"
        ))["article"]
        second_assignment = repo.assign_publish_account(second_article["id"])

        assert second_assignment["publish_account_id"] == second_account["id"]
        counts = {
            item["id"]: item["assignment_count"]
            for item in repo.list_publish_accounts()
        }
        assert counts == {first_account["id"]: 1, second_account["id"]: 1}


def test_publish_account_pool_switch_preserves_an_enabled_account(app):
    with app.app_context():
        with pytest.raises(ValueError, match="至少需要一个已启用"):
            repo.set_publish_account_pool_enabled(True)

        account = repo.create_publish_account(13501, "池开关账号")
        assert repo.set_publish_account_pool_enabled(True) is True
        with pytest.raises(ValueError, match="不能停用最后一个"):
            repo.update_publish_account(account["id"], enabled=False)
        assert repo.get_publish_account(account["id"])["enabled"] == 1

        assert repo.set_publish_account_pool_enabled(False) is False
        assert repo.update_publish_account(account["id"], enabled=False)["enabled"] == 0


def test_concurrent_publish_account_disable_keeps_one_enabled(app):
    with app.app_context():
        accounts = [
            repo.create_publish_account(13601, "并发停用一"),
            repo.create_publish_account(13602, "并发停用二"),
        ]
        repo.set_publish_account_pool_enabled(True)
        database = app.config["DATABASE"]

    barrier = threading.Barrier(2)
    disabled = []
    errors = []

    def disable(account_id: int) -> None:
        conn = _connect(database)
        try:
            barrier.wait(timeout=2)
            repo.update_publish_account(account_id, conn, enabled=False)
            disabled.append(account_id)
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=disable, args=(account["id"],))
        for account in accounts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert len(disabled) == 1
    assert len(errors) == 1
    assert "不能停用最后一个" in str(errors[0])
    with app.app_context():
        enabled_accounts = repo.list_publish_accounts(include_disabled=False)
    assert len(enabled_accounts) == 1


def test_concurrent_publish_account_assignment_counts_once(app):
    with app.app_context():
        account = repo.create_publish_account(14001, "并发账号")
        article = repo.upsert_material(_material())["article"]
        database = app.config["DATABASE"]

    barrier = threading.Barrier(2)
    assignments = []
    errors = []

    def assign() -> None:
        conn = _connect(database)
        try:
            barrier.wait(timeout=2)
            assignments.append(repo.assign_publish_account(article["id"], conn))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=assign) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert len(assignments) == 2
    assert {item["publish_account_id"] for item in assignments} == {account["id"]}
    with app.app_context():
        assert repo.get_publish_account(account["id"])["assignment_count"] == 1


def test_init_db_migrates_publish_account_snapshot_columns(tmp_path):
    database = tmp_path / "legacy-publish-accounts.sqlite3"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_url TEXT NOT NULL,
                origin_key TEXT,
                duplicate_of_article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
                upstream_archive_id INTEGER NOT NULL DEFAULT 0,
                dqd_source_id INTEGER,
                dqd_archive_id INTEGER,
                title_original TEXT NOT NULL DEFAULT '',
                title_final TEXT NOT NULL DEFAULT '',
                body_html TEXT NOT NULL DEFAULT '',
                litpic TEXT NOT NULL DEFAULT '',
                channels_json TEXT NOT NULL DEFAULT '[]',
                tab_id INTEGER,
                level TEXT NOT NULL DEFAULT 'B',
                status TEXT NOT NULL DEFAULT 'RECEIVED',
                quality_json TEXT NOT NULL DEFAULT '{}',
                raw_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                review_note TEXT,
                reviewed_at TEXT,
                published_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE (source, source_url)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO articles
            (source,source_url,created_at,updated_at,last_seen_at)
            VALUES (?,?,?,?,?)
            """,
            (
                "legacy",
                "https://example.com/legacy-confirmation",
                "2026-08-01T00:00:00.000Z",
                "2026-08-01T00:00:00.000Z",
                "2026-08-01T00:00:00.000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    init_db(database)
    init_db(database)
    conn = _connect(database)
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()
        }
        assert {
            "publish_account_id",
            "publish_user_id",
            "publish_user_name",
            "publish_account_assigned_at",
        }.issubset(columns)
        assert {
            "client_request_id",
            "upstream_request_id",
            "draft_confirm_attempts",
            "draft_next_confirm_at",
            "draft_uncertain_since",
            "draft_last_attempt_at",
            "draft_confirm_claimed_at",
            "draft_confirm_claim_token",
        }.issubset(columns)
        assert {"publish_mode", "publish_mode_decided_at"}.issubset(columns)
        assert "publish_mode" in {
            row["name"] for row in conn.execute("PRAGMA table_info(tabs)").fetchall()
        }
        assert "publish_mode_override" in {
            row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()
        }
        migrated = conn.execute(
            "SELECT client_request_id,draft_confirm_attempts FROM articles"
        ).fetchone()
        assert migrated["client_request_id"]
        assert migrated["draft_confirm_attempts"] == 0
        assert conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_articles_client_request_id'
            """
        ).fetchone() is not None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='publish_accounts'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_disabling_referenced_tab_is_rejected(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        with pytest.raises(ValueError, match="referenced"):
            repo.disable_tab(tab["id"])


def test_manual_review_updates_content_and_status(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_material())["article"]
        repo.transition_status(article["id"], "NEEDS_REVIEW")
        updated = repo.manual_review_update(
            article["id"],
            "manual_fix_then_pass",
            title="修正后的完整标题",
            body_html="<p>修正后的完整正文，信息充分且没有广告内容，可以进入后续待发队列。</p>",
            note="人工已核对",
        )
        assert updated["status"] == "READY_TO_PUBLISH"
        assert updated["title_final"] == "修正后的完整标题"
        assert updated["quality"]["pass"] is True
        assert updated["quality"]["needs_review"] is False
        assert len(repo.list_article_events(article["id"])) >= 3


def test_transition_status_if_current_prevents_double_claim(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_source("marca", tab_id=tab["id"], enabled=True)
        article = repo.upsert_material(_material())["article"]
        repo.transition_status(article["id"], "READY_TO_PUBLISH")

        first = repo.transition_status_if_current(
            article["id"],
            "PUBLISHING",
            allowed_from={"READY_TO_PUBLISH"},
            event_type="DRAFT_CREATE_STARTED",
            message="开始创建草稿",
        )
        second = repo.transition_status_if_current(
            article["id"],
            "PUBLISHING",
            allowed_from={"READY_TO_PUBLISH"},
            event_type="DRAFT_CREATE_STARTED",
            message="开始创建草稿",
        )

        assert first["status"] == "PUBLISHING"
        assert second is None
        events = repo.list_article_events(article["id"])
        assert events[-1]["event_type"] == "DRAFT_CREATE_STARTED"


def test_manual_review_only_accepts_articles_waiting_for_review(app):
    with app.app_context():
        article = repo.upsert_material(_material())['article']
        with pytest.raises(ValueError, match="awaiting manual review"):
            repo.manual_review_update(article["id"], "pass")


def test_quality_recheck_claim_is_atomic_and_clears_previous_attempt_marker(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        repo.save_quality(
            article["id"],
            {
                "pass": False,
                "needs_review": True,
                "promotion_repair": {"attempted": True, "outcome": "failed"},
            },
            status="NEEDS_REVIEW",
        )

        claimed = repo.claim_quality_recheck(article["id"])
        assert claimed["status"] == "RECEIVED"
        assert "promotion_repair" not in claimed["quality"]
        assert repo.claim_quality_recheck(article["id"]) is None
        events = repo.list_article_events(article["id"])
        assert events[-1]["event_type"] == "QUALITY_RECHECK_REQUESTED"


def test_quality_article_claim_is_atomic_and_reclaims_expired_lease(app):
    with app.app_context():
        article = repo.upsert_material(_material())[
            "article"
        ]
        first = repo.claim_quality_article(article["id"], article["updated_at"])
        assert first is not None
        assert first["status"] == "QUALITY_CHECKING"
        assert first["quality_claim_token"]
        second = repo.claim_quality_article(article["id"], article["updated_at"])
        assert second is None

        expired = repo.claim_quality_article(
            article["id"], None, now="2099-09-01T00:20:00.000Z",
            claim_stale_after_seconds=1,
        )
        assert expired is not None
        assert expired["quality_claim_token"] != first["quality_claim_token"]


def test_expired_quality_worker_cannot_overwrite_new_lease(app):
    with app.app_context():
        article = repo.upsert_material(_material())[
            "article"
        ]
        first = repo.claim_quality_article(article["id"], article["updated_at"])
        assert first is not None
        second = repo.claim_quality_article(
            article["id"],
            None,
            now="2099-09-01T00:20:00.000Z",
            claim_stale_after_seconds=1,
        )
        assert second is not None
        original_body = second["body_html"]

        stale_token = first["quality_claim_token"]
        with pytest.raises(RuntimeError, match="租约已失效"):
            repo.save_quality(
                article["id"],
                {"pass": True, "needs_review": False},
                status="READY_TO_PUBLISH",
                quality_claim_token=stale_token,
                body_html="<p>过期 worker 的候选正文</p>",
            )
        assert repo.transition_status(
            article["id"], "ERROR", quality_claim_token=stale_token
        ) is None
        with pytest.raises(RuntimeError, match="租约已失效"):
            repo.add_article_event(
                article["id"],
                "STALE_WORKER_EVENT",
                quality_claim_token=stale_token,
            )

        current = repo.get_article(article["id"])
        assert current["status"] == "QUALITY_CHECKING"
        assert current["quality"] == {}
        assert current["body_html"] == original_body
        assert not any(
            event["event_type"] == "STALE_WORKER_EVENT"
            for event in repo.list_article_events(article["id"])
        )
        repo.release_quality_claim(article["id"], second["quality_claim_token"])


def test_save_quality_can_commit_candidate_body_and_result_together(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        claimed = repo.claim_quality_article(article["id"], article["updated_at"])
        candidate = "<p>二次质检通过后的候选正文。</p>"
        quality = {"pass": True, "needs_review": False, "reason": "内容正常"}

        saved = repo.save_quality(
            article["id"],
            quality,
            status="READY_TO_PUBLISH",
            quality_claim_token=claimed["quality_claim_token"],
            body_html=candidate,
        )

        assert saved["body_html"] == candidate
        assert saved["quality"] == quality
        assert saved["status"] == "READY_TO_PUBLISH"


def test_catalog_seed_survives_operator_tab_edits_and_restart(app):
    with app.app_context():
        tab = repo.list_tabs()[0]
        repo.update_tab(tab["id"], name="自定义栏目名称", backend_tab_id=900001)
    restarted = __import__("app.web", fromlist=["create_app"]).create_app({
        "TESTING": True,
        "DATABASE": app.config["DATABASE"],
    })
    with restarted.app_context():
        edited = repo.get_tab(tab["id"])
        assert edited["name"] == "自定义栏目名称"
        assert edited["backend_tab_id"] == 900001


def test_draft_confirmation_persists_stable_id_and_unknown_result(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        first_key = repo.ensure_client_request_id(article["id"])
        assert first_key
        assert repo.ensure_client_request_id(article["id"]) == first_key

        repo.transition_status(article["id"], "PUBLISHING")
        unknown = repo.mark_draft_result_unknown(
            article["id"],
            request_id="upstream-request-502",
            next_confirm_at="2020-01-01T00:00:00.000Z",
            message="上游返回 HTTP 502，结果待确认",
            payload={"status_code": 502},
        )

        assert unknown["status"] == "DRAFT_CONFIRMING"
        assert unknown["client_request_id"] == first_key
        assert unknown["upstream_request_id"] == "upstream-request-502"
        assert unknown["draft_confirm_attempts"] == 0
        assert unknown["draft_uncertain_since"]
        assert unknown["draft_last_attempt_at"]
        assert unknown["draft_next_confirm_at"] == "2020-01-01T00:00:00.000Z"

        due = repo.list_due_draft_confirmations(now="2020-01-02T00:00:00.000Z")
        assert [item["id"] for item in due] == [article["id"]]


def test_draft_confirmation_claim_is_cas_and_can_record_created(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        repo.transition_status(article["id"], "PUBLISHING")
        repo.mark_draft_result_unknown(
            article["id"],
            next_confirm_at="2020-01-01T00:00:00.000Z",
        )
        due = repo.list_due_draft_confirmations(now="2020-01-02T00:00:00.000Z")[0]

        claimed = repo.claim_due_draft_confirmation(
            article["id"],
            due["updated_at"],
            now="2020-01-02T00:00:00.000Z",
        )
        assert claimed is not None
        assert claimed["status"] == "DRAFT_CONFIRMING"
        assert claimed["draft_confirm_attempts"] == 1
        assert claimed["draft_confirm_claim_token"]
        assert repo.claim_due_draft_confirmation(
            article["id"],
            due["updated_at"],
            now="2020-01-02T00:00:01.000Z",
        ) is None

        completed = repo.record_draft_confirmation_result(
            article["id"],
            outcome="CREATED",
            dqd_archive_id=987654,
            request_id="upstream-request-200",
            expected_updated_at=claimed["updated_at"],
            claim_token=claimed["draft_confirm_claim_token"],
        )
        assert completed["status"] == "DRAFT_CREATED"
        assert completed["dqd_archive_id"] == 987654
        assert completed["upstream_request_id"] == "upstream-request-200"
        assert completed["draft_next_confirm_at"] is None
        assert completed["draft_uncertain_since"]
        assert completed["draft_confirm_claim_token"] is None
        assert repo.list_due_draft_confirmations() == []


def test_draft_confirmation_pending_releases_claim_and_reschedules(app):
    with app.app_context():
        article = repo.upsert_material(_material())["article"]
        repo.transition_status(article["id"], "PUBLISHING")
        repo.mark_draft_result_unknown(
            article["id"],
            next_confirm_at="2020-01-01T00:00:00.000Z",
        )
        due = repo.list_due_draft_confirmations(now="2020-01-02T00:00:00.000Z")[0]
        claimed = repo.claim_due_draft_confirmation(
            article["id"], due["updated_at"], now="2020-01-02T00:00:00.000Z"
        )
        pending = repo.record_draft_confirmation_result(
            article["id"],
            outcome="PENDING",
            next_confirm_at="2099-01-01T00:00:00.000Z",
            message="上游仍在处理",
            expected_updated_at=claimed["updated_at"],
            claim_token=claimed["draft_confirm_claim_token"],
        )
        assert pending["status"] == "DRAFT_CONFIRMING"
        assert pending["draft_next_confirm_at"] == "2099-01-01T00:00:00.000Z"
        assert pending["draft_confirm_claimed_at"] is None
        assert pending["draft_confirm_attempts"] == 1
        assert repo.list_due_draft_confirmations(now="2020-01-02T00:00:00.000Z") == []
