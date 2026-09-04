from __future__ import annotations

import threading

from app.config import AppConfig
from app.services.pipeline import RunController, _try_acquire_run_lock
from app.services.scheduler import Scheduler


def test_run_controller_rejects_overlapping_runs(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocking_run(config):
        entered.set()
        assert release.wait(2)
        return {"message": "done", "errors": 0}

    monkeypatch.setattr("app.services.pipeline.run_once", blocking_run)
    controller = RunController(AppConfig(scheduler_enabled=False))

    assert controller.start() is True
    assert entered.wait(1)
    assert controller.start() is False
    release.set()
    assert controller.wait(2) is True
    assert controller.status() == {
        "running": False,
        "last_result": {"message": "done", "errors": 0},
    }


def test_run_controller_recovers_after_unexpected_error(monkeypatch):
    calls = 0

    def failing_run(config):
        nonlocal calls
        calls += 1
        raise RuntimeError("unexpected")

    monkeypatch.setattr("app.services.pipeline.run_once", failing_run)
    controller = RunController(AppConfig(scheduler_enabled=False))

    assert controller.start() is True
    assert controller.wait(2) is True
    status = controller.status()
    assert status["running"] is False
    assert status["last_result"]["errors"] == 1
    assert status["last_result"]["message"] == "unexpected"

    assert controller.start() is True
    assert controller.wait(2) is True
    assert calls == 2


def test_run_lock_is_shared_by_separate_file_handles(tmp_path):
    database = str(tmp_path / "automatic.sqlite3")
    first = _try_acquire_run_lock(database)
    assert first is not None
    try:
        assert _try_acquire_run_lock(database) is None
    finally:
        import fcntl

        fcntl.flock(first.fileno(), fcntl.LOCK_UN)
        first.close()
    released = _try_acquire_run_lock(database)
    assert released is not None
    released.close()


class _FakeController:
    def __init__(self):
        self.called = threading.Event()
        self.calls = 0

    def start(self):
        self.calls += 1
        self.called.set()
        return True


class _FakeMaintenanceController:
    def __init__(self):
        self.called = threading.Event()
        self.calls = 0

    def start(self):
        self.calls += 1
        self.called.set()
        return True


def test_scheduler_uses_controller_and_can_pause_resume():
    controller = _FakeController()
    scheduler = Scheduler(controller, interval_seconds=60)
    try:
        scheduler.start()
        scheduler.stop()
        assert scheduler.trigger() is False
        assert not controller.called.wait(0.05)

        scheduler.start()
        assert scheduler.trigger() is True
        assert controller.called.wait(1)
        assert controller.calls == 1
    finally:
        assert scheduler.shutdown() is True


def test_scheduler_runs_maintenance_controller_on_short_interval():
    controller = _FakeController()
    maintenance = _FakeMaintenanceController()
    scheduler = Scheduler(
        controller,
        interval_seconds=60,
        maintenance_controller=maintenance,
        maintenance_interval_seconds=1,
    )
    try:
        scheduler.start()
        assert maintenance.called.wait(2)
        assert maintenance.calls == 1
        assert controller.calls == 0
    finally:
        assert scheduler.shutdown() is True
