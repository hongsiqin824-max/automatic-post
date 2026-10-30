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
# 详情页的直接发布按钮是否可点击取决于这两个开关，不钉死的话测试结果会随
# 本机 .env 漂移：服务器上没有 .env 时发布开关默认关闭，按钮变成禁用，
# 确认提示文案不会渲染，test_article_detail_warns_before_direct_publish 就失败。
os.environ["AUTOMATIC_POST_PUBLISHER"] = "true"
os.environ["DQD_OPEN_APPID"] = "test-appid"
os.environ["DQD_OPEN_APPSECRET"] = "test-appsecret"
os.environ["DQD_OPEN_ENNAME"] = "test-enname"

from app.web import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_china_political_check(monkeypatch):
    """质检最前面的涉华政治轮会真调模型；默认判未命中，需要的测试自行覆盖。"""

    monkeypatch.setattr(
        "app.services.pipeline.check_china_political",
        lambda *args, **kwargs: {"china_political": False, "reason": ""},
    )


@pytest.fixture()
def app(tmp_path):
    database = tmp_path / "test.sqlite3"
    app = create_app({"TESTING": True, "DATABASE": str(database)})
    app.config["DATABASE"] = str(database)
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()
