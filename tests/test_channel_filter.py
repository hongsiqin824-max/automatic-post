from __future__ import annotations

from app.services.channel_filter import BLOCKED_CHANNEL_IDS, filter_blocked_channels


def test_filter_blocked_channels_matches_exact_ids_and_preserves_order():
    assert filter_blocked_channels([89, "845616", 93, 122, 189, 122]) == [845616, 122, 189]


def test_filter_blocked_channels_allows_empty_input():
    assert filter_blocked_channels(None) == []
    assert filter_blocked_channels([]) == []


def test_filter_blocked_channels_removes_every_configured_blacklist_id():
    assert filter_blocked_channels(sorted(BLOCKED_CHANNEL_IDS)) == []
