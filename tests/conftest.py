from __future__ import annotations

import pytest

from app.web import create_app


@pytest.fixture()
def app(tmp_path):
    database = tmp_path / "test.sqlite3"
    app = create_app({"TESTING": True, "DATABASE": str(database)})
    app.config["DATABASE"] = str(database)
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()
