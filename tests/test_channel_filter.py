from __future__ import annotations

from app.services.channel_filter import BLOCKED_CHANNEL_IDS, filter_blocked_channels


def test_filter_blocked_channels_matches_exact_ids_and_preserves_order():
    # 700000001 is an arbitrary ID absent from every blacklist (team/backend/player).
    assert filter_blocked_channels([89, "700000001", 93, 122, 189, 122]) == [700000001, 122, 189]


def test_filter_blocked_channels_removes_player_tag_ids():
    # Player tag IDs exported from 五大联赛及中超球员标签.csv must be stripped too.
    assert filter_blocked_channels([2038889, 82309, 845616, 189]) == [189]


def test_filter_blocked_channels_allows_empty_input():
    assert filter_blocked_channels(None) == []
    assert filter_blocked_channels([]) == []


def test_filter_blocked_channels_removes_every_configured_blacklist_id():
    assert filter_blocked_channels(sorted(BLOCKED_CHANNEL_IDS)) == []
