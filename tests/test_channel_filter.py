from __future__ import annotations

from app.services.channel_filter import BLOCKED_CHANNEL_IDS, filter_blocked_channels


def test_filter_blocked_channels_matches_exact_ids_and_preserves_order():
    # 700000001/700000002 are arbitrary IDs absent from every blacklist
    # (team/backend/player); 89、93 are blacklisted national-team tags.
    assert filter_blocked_channels(
        [89, "700000001", 93, 700000002, 189, 700000002]
    ) == [700000001, 700000002, 189]


def test_filter_blocked_channels_removes_player_tag_ids():
    # Player tag IDs exported from 五大联赛及中超球员标签.csv must be stripped too.
    assert filter_blocked_channels([2038889, 82309, 845616, 189]) == [189]


def test_filter_blocked_channels_removes_extra_eight_team_tags():
    # 8 team tags from 额外8队球员标签.csv (Al Nassr/Al Hilal/Sporting/Benfica/
    # Ajax/West Ham/Wolves/Southampton) plus one of their player tags.
    extra_teams = [2547, 1573, 2128, 91, 1099, 1818, 533, 1807]
    assert filter_blocked_channels(extra_teams + [11962998, 189]) == [189]


def test_filter_blocked_channels_removes_extra_source_tag_ids():
    # 球队/球员标签 IDs exported from 国内所有赛事/中甲中乙/名宿退役/欧冠
    # 的导出表, plus the standalone补充 ID 14774, must all be stripped.
    extra = [148, 152, 153, 172, 370, 382, 14774]
    assert filter_blocked_channels(extra + [189]) == [189]


def test_filter_blocked_channels_allows_empty_input():
    assert filter_blocked_channels(None) == []
    assert filter_blocked_channels([]) == []


def test_filter_blocked_channels_removes_every_configured_blacklist_id():
    assert filter_blocked_channels(sorted(BLOCKED_CHANNEL_IDS)) == []
