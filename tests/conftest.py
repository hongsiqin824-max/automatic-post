from __future__ import annotations

import os

import pytest

# AppConfig 的默认值在 import 时就从环境（含 .env）读定，测试结果不该随本机 .env
# 漂移。在导入 app 之前钉死会改变发布决策的开关；load_dotenv 不覆盖已有变量，
# 所以这里的值优先于 .env。需要别的取值的测试自行通过 AppConfig(...) 覆盖。
os.environ["AUTOMATIC_POST_RELEVANCE_GUARD_ENABLED"] = "true"
os.environ["AUTOMATIC_POST_RELEVANCE_GUARD_SCOPE"] = "intl"
os.environ["AUTOMATIC_POST_RELEVANCE_GUARD_MIN_CONFIDENCE"] = "0.9"
os.environ["AUTOMATIC_POST_RELEVANCE_GUARD_KEEP_UPGRADE"] = "true"

from app.web import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    database = tmp_path / "test.sqlite3"
    app = create_app({"TESTING": True, "DATABASE": str(database)})
    app.config["DATABASE"] = str(database)
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()
