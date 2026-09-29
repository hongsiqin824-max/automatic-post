from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from app import repository as repo
from app.config import AppConfig
from app.db import get_db
from app.services import league_clicks


class _FakeCursor:
    def __init__(self, rows_by_window, calls):
        self.rows_by_window = rows_by_window
        self.calls = calls
        self.params = None

    def execute(self, _sql, params):
        self.params = params
        self.calls.append(params)

    def fetchall(self):
        return self.rows_by_window[self.params]


class _FakeConnection:
    def __init__(self, rows_by_window, calls):
        self.rows_by_window = rows_by_window
        self.calls = calls

    def cursor(self):
        return _FakeCursor(self.rows_by_window, self.calls)

    def close(self):
        pass


def _window(start_hour: int, end_hour: int) -> tuple[str, str]:
    end_day = "2026-09-26" if end_hour == 24 else "2026-09-25"
    normalized_end_hour = 0 if end_hour == 24 else end_hour
    return (
        f"2026-09-25 {start_hour:02d}:00:00",
        f"{end_day} {normalized_end_hour:02d}:00:00",
    )


def test_fetch_day_splits_into_windows_and_merges_counts(monkeypatch):
    calls = []
    rows = {
        _window(0, 6): [("日职联", 3)],
        _window(6, 12): [("英超", 5), ("日职联", 2)],
        _window(12, 18): [],
        _window(18, 24): [("英超", 1)],
    }
    monkeypatch.setattr(
        league_clicks,
        "_connect_starrocks",
        lambda _config: _FakeConnection(rows, calls),
    )

    result = league_clicks.fetch_day(AppConfig(), date(2026, 9, 25))

    assert result == [("英超", 6), ("日职联", 5)]
    assert calls == list(rows)


def test_fetch_day_falls_back_to_smaller_window(monkeypatch):
    calls = []
    rows = {
        _window(0, 3): [("日职联", 3)],
        _window(3, 6): [("日职联", 2)],
        _window(6, 12): [],
        _window(12, 18): [],
        _window(18, 24): [],
    }

    def connect(_config):
        connection = _FakeConnection(rows, calls)

        original_cursor = connection.cursor

        def cursor():
            result = original_cursor()
            original_execute = result.execute

            def execute(sql, params):
                if params == _window(0, 6):
                    calls.append(params)
                    raise RuntimeError("exceed big query cpu limit")
                original_execute(sql, params)

            result.execute = execute
            return result

        connection.cursor = cursor
        return connection

    monkeypatch.setattr(league_clicks, "_connect_starrocks", connect)

    result = league_clicks.fetch_day(AppConfig(), date(2026, 9, 25))

    assert result == [("日职联", 5)]
    assert calls[:3] == [_window(0, 6), _window(0, 3), _window(3, 6)]


def test_failed_day_is_not_saved_and_is_backed_off(app, monkeypatch):
    config = replace(
        AppConfig(database_path=app.config["DATABASE"]),
        starrocks_user="test-user",
        league_clicks_backfill_days=30,
    )
    failed_day = date(2026, 9, 25)
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

    def fail_fetch(_config, _day):
        raise RuntimeError("exceed big query cpu limit")

    monkeypatch.setattr(league_clicks, "fetch_day", fail_fetch)
    monkeypatch.setattr(repo, "_now", lambda: now.isoformat().replace("+00:00", "Z"))

    with app.app_context():
        connection = get_db()
        result = league_clicks.sync_due_days(config, connection, now=now)
        assert failed_day.isoformat() in result["failed"]
        assert connection.execute(
            "SELECT COUNT(*) FROM league_tab_clicks WHERE stat_date=?",
            (failed_day.isoformat(),),
        ).fetchone()[0] == 0
        run = connection.execute(
            "SELECT status, total_cnt, error FROM league_tab_click_runs WHERE stat_date=?",
            (failed_day.isoformat(),),
        ).fetchone()
        assert tuple(run) == ("FAILED", 0, "exceed big query cpu limit")

        # mark_league_tab_click_run uses the current persisted timestamp. A retry
        # immediately after failure must therefore be deferred.
        assert failed_day not in league_clicks.pending_dates(config, connection, now=now)

        connection.execute(
            "UPDATE league_tab_click_runs SET fetched_at=? WHERE stat_date=?",
            ((now - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"), failed_day.isoformat()),
        )
        assert failed_day in league_clicks.pending_dates(config, connection, now=now)


def test_league_clicks_page_does_not_show_failed_count_as_zero(app, client):
    with app.app_context():
        repo.mark_league_tab_click_run(
            "2026-09-27",
            get_db(),
            status="FAILED",
            error="exceed big query cpu limit",
        )

    response = client.get("/league-data/clicks")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "未取得" in html
    assert "exceed big query cpu limit" in html
