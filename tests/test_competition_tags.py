from __future__ import annotations

import dataclasses

import pytest

from app import repository as repo
from app.config import AppConfig
from app.db import _connect, get_db, init_db
from app.services import channel_filter
from app.services import competition_tags as ct


def _cfg(**overrides):
    base = AppConfig()
    defaults = {
        "competition_tags_enabled": True,
        "competition_tags_max_drop_ratio": 0.5,
    }
    defaults.update(overrides)
    return dataclasses.replace(base, **defaults)


def test_validate_rejects_empty_and_missing_season():
    cfg = _cfg()
    assert ct._validate(cfg, {"season_id": None, "team_ids": [], "player_ids": []}, None)[0] is False
    ok, reason = ct._validate(
        cfg, {"season_id": 1, "team_ids": [], "player_ids": [1]}, None
    )
    assert ok is False and "为空" in reason


def test_validate_first_time_without_threshold():
    cfg = _cfg()
    # 无历史基线时，只要不空就通过（不再设下限）。
    few = {"season_id": 1, "team_ids": [1000], "player_ids": [2000]}
    assert ct._validate(cfg, few, None)[0] is True
    many = {"season_id": 1, "team_ids": list(range(20)), "player_ids": list(range(300))}
    assert ct._validate(cfg, many, None)[0] is True


def test_validate_drop_protection():
    cfg = _cfg(competition_tags_max_drop_ratio=0.5)
    previous = {"status": "SUCCESS", "team_count": 20, "player_count": 500}
    crashed = {"season_id": 2, "team_ids": list(range(8)), "player_ids": list(range(100))}
    ok, reason = ct._validate(cfg, crashed, previous)
    assert ok is False and "骤降" in reason
    steady = {"season_id": 2, "team_ids": list(range(18)), "player_ids": list(range(480))}
    assert ct._validate(cfg, steady, previous)[0] is True


def test_sync_competition_success_then_rejected_keeps_old(monkeypatch, tmp_path):
    database = str(tmp_path / "ct.sqlite3")
    init_db(database)
    conn = _connect(database)
    cfg = _cfg()

    monkeypatch.setattr(
        ct, "fetch_competition",
        lambda config, cid: {
            "season_id": 100,
            "team_ids": list(range(1000, 1020)),       # 20 teams
            "player_ids": list(range(2000, 2300)),     # 300 players
        },
    )
    result = ct.sync_competition(cfg, 257, conn)
    assert result["status"] == "SUCCESS"
    tags = repo.load_competition_channel_tags(conn)
    assert len(tags[257]) == 320

    # A collapsed pull must be rejected and must not overwrite good data.
    monkeypatch.setattr(
        ct, "fetch_competition",
        lambda config, cid: {"season_id": 101, "team_ids": [1000], "player_ids": [2000]},
    )
    result = ct.sync_competition(cfg, 257, conn)
    assert result["status"] == "REJECTED"
    tags = repo.load_competition_channel_tags(conn)
    assert len(tags[257]) == 320  # unchanged
    run = repo.get_competition_tag_run(257, conn)
    assert run["status"] == "REJECTED"
    conn.close()


def test_reload_tag_caches_builds_tab_whitelist(app):
    with app.app_context():
        conn = get_db()
        # 澳超 → competition 257 → backend_tab_id 348（种子栏目）
        repo.replace_competition_tags(257, 100, [111, 222], [333], conn)
        caches = ct.reload_tag_caches(conn)
        tab = repo.get_tab_by_name("澳超", conn)
        backend_id = int(tab["backend_tab_id"])
        assert caches["tabs"].get(backend_id) == 3
        assert channel_filter.filter_to_tab_whitelist([111, 999, 333], [backend_id]) == [111, 333]
    channel_filter.set_tab_whitelist(None)


def test_reload_builds_dynamic_blacklist(app):
    with app.app_context():
        conn = get_db()
        # competition 4（英超）属于 BLACKLIST_COMPETITIONS，其标签应进动态黑名单。
        repo.replace_competition_tags(4, 24646, [700001, 700002], [700003], conn)
        caches = ct.reload_tag_caches(conn)
        assert caches["blocked"] == 3
        # 动态黑名单叠加进 filter_blocked_channels：700001 被删，189 保留。
        assert channel_filter.filter_blocked_channels([700001, 700003, 189]) == [189]
    channel_filter.set_dynamic_blocked(None)
    channel_filter.set_tab_whitelist(None)


def test_sync_national_teams_dispatches_and_stores(monkeypatch, tmp_path):
    database = str(tmp_path / "ct.sqlite3")
    init_db(database)
    conn = _connect(database)
    cfg = _cfg()

    # 国家队哨兵 id 必须走 fetch_national_teams，而不是走赛季的 fetch_competition。
    monkeypatch.setattr(
        ct, "fetch_competition",
        lambda config, cid: pytest.fail("国家队不应走 fetch_competition"),
    )
    monkeypatch.setattr(
        ct, "fetch_national_teams",
        lambda config: {
            "season_id": ct._NATIONAL_SEASON_SENTINEL,
            "season_ids": [ct._NATIONAL_SEASON_SENTINEL],
            "team_ids": [500, 501],       # 国家队球队标签
            "player_ids": [600, 601, 602],  # 国家队球员标签
        },
    )
    result = ct.sync_competition(cfg, ct.NATIONAL_TEAMS_COMPETITION_ID, conn)
    assert result["status"] == "SUCCESS"
    tags = repo.load_competition_channel_tags(conn)
    assert tags[ct.NATIONAL_TEAMS_COMPETITION_ID] == {500, 501, 600, 601, 602}
    conn.close()


def test_national_column_has_no_whitelist_clubs_kept(app):
    """国家队栏目已退出白名单收敛：俱乐部标签保留，仅黑名单生效。

    历史背景：曾给「国家队」栏目配 type='national' 白名单做收敛，但对生产库全量
    6782 篇国家队文章评估后发现——国家队新闻天然牵涉球员所属俱乐部，白名单会把这些
    合理俱乐部标签误删（49% 文章被波及、删掉的 88% 是俱乐部），故停用（见
    competition_tags._WHITELIST_ROUTING）。本测试守护「国家队栏目不再做白名单收敛」，
    防止日后有人又把国家队加回白名单。
    """
    with app.app_context():
        conn = get_db()
        # 99 是主流国家队球队标签，在静态黑名单里（用于验证黑名单仍生效）。
        assert 99 in channel_filter.BLOCKED_CHANNEL_IDS
        # 即使库里残留国家队哨兵标签，reload 也不会再给国家队栏目建白名单。
        repo.replace_competition_tags(
            ct.NATIONAL_TEAMS_COMPETITION_ID, ct._NATIONAL_SEASON_SENTINEL,
            [99, 500], [600], conn,
        )
        caches = ct.reload_tag_caches(conn)
        tab = repo.get_tab_by_name("国家队", conn)
        backend_id = int(tab["backend_tab_id"])
        # 国家队栏目没有白名单集合。
        assert caches["tabs"].get(backend_id) is None

        # 国家队栏目文章：无白名单 → 俱乐部标签 1568（首尔，国脚所属俱乐部、不在
        # 黑名单）被保留（放行，不收敛）。1568 正是评估中被旧白名单误删最多的俱乐部。
        article = [99, 500, 600, 1568]
        assert 1568 not in channel_filter.BLOCKED_CHANNEL_IDS
        passed = channel_filter.filter_to_tab_whitelist(article, [backend_id])
        assert passed == [99, 500, 600, 1568]
        # 黑名单仍照常生效：99 是主流国家队标签，永久剔除；俱乐部 1568 等保留。
        published = channel_filter.filter_blocked_channels(passed)
        assert published == [500, 600, 1568]
    channel_filter.set_tab_whitelist(None)
    channel_filter.set_dynamic_blocked(None)
