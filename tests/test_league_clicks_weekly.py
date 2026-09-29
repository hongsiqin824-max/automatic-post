"""测试联赛点击数据周报功能。"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app import repository as repo
from app.services.league_clicks_weekly import (
    FeishuAPIError,
    LeagueClicksWeeklyController,
    LeagueClicksWeeklyScheduler,
    build_weekly_report_data,
    create_sheet,
    get_tenant_access_token,
    latest_due_saturday,
    send_weekly_report,
    write_sheet_data,
)


BEIJING = ZoneInfo("Asia/Shanghai")


def _json_response(payload):
    """构造一个 requests 风格的假响应。"""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


@pytest.fixture
def mock_config():
    """创建一个模拟的配置对象。"""
    config = MagicMock()
    config.database_path = ":memory:"
    config.league_clicks_weekly_enabled = True
    config.league_clicks_weekly_hour = 19
    config.league_clicks_weekly_minute = 0
    config.league_clicks_weekly_check_interval_seconds = 60
    config.feishu_app_id = "test_app_id"
    config.feishu_app_secret = "test_app_secret"
    config.feishu_spreadsheet_token = "test_token"
    config.feishu_api_timeout_seconds = 10
    config.feishu_spreadsheet_configured = True
    return config


@pytest.fixture
def sample_db(app):
    """插入已直接发布的文章，覆盖周报统计窗口。

    周报按「前一天 19:00 → 当天 19:00（北京）」逐日统计，所以这里把
    published_at 设在每天北京 12:00（UTC 04:00），落在对应那天的窗口里。
    只有 status=PUBLISHED 且 publish_mode=1 才会被 direct_publish_report 计入。
    """
    with app.app_context():
        from app.db import get_db
        conn = get_db()

        tab_ids = {}
        for name in ("日职联", "巴甲", "德乙"):
            # 这些栏目由 init_db 的 seed 预置，取现成的即可
            row = conn.execute("SELECT id FROM tabs WHERE name=?", (name,)).fetchone()
            tab_ids[name] = int(row["id"])

        # {栏目: 每天发布篇数}，日期从 2026-09-20 起共 7 天
        plan = {
            "日职联": [3, 2, 4, 1, 0, 2, 5],
            "巴甲": [1, 1, 0, 2, 3, 0, 1],
            "德乙": [0, 2, 1, 0, 1, 1, 0],
        }
        base_date = datetime(2026, 9, 20).date()
        seq = 0
        for name, counts in plan.items():
            for day_idx, count in enumerate(counts):
                day = base_date + timedelta(days=day_idx)
                published_at = f"{day.isoformat()}T04:00:00.000Z"  # 北京 12:00
                for _ in range(count):
                    seq += 1
                    conn.execute(
                        """
                        INSERT INTO articles
                        (source, source_url, status, publish_mode, published_at,
                         published_tab_names_json, tab_id)
                        VALUES (?,?,'PUBLISHED',1,?,?,?)
                        """,
                        (
                            "test",
                            f"https://example.com/{seq}",
                            published_at,
                            json.dumps([name], ensure_ascii=False),
                            tab_ids[name],
                        ),
                    )
        conn.commit()
        yield conn


def test_get_tenant_access_token_success():
    """测试成功获取 tenant_access_token。"""
    mock_response = MagicMock()
    mock_response.json.return_value = {"code": 0, "tenant_access_token": "test_token_123"}
    mock_response.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_response) as mock_post:
        token = get_tenant_access_token("app_id", "app_secret", timeout=10)
        assert token == "test_token_123"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"] == {"app_id": "app_id", "app_secret": "app_secret"}


def test_get_tenant_access_token_api_error():
    """测试飞书 API 返回错误。"""
    mock_response = MagicMock()
    mock_response.json.return_value = {"code": 99, "msg": "invalid app_id"}
    mock_response.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_response):
        with pytest.raises(FeishuAPIError, match="获取 token 失败"):
            get_tenant_access_token("bad_id", "bad_secret")


def test_create_sheet_success():
    """测试成功创建 sheet。"""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "code": 0,
        "data": {
            "replies": [
                {
                    "addSheet": {
                        "properties": {
                            "sheetId": "new_sheet_id_123",
                            "title": "2026-09-20 ~ 2026-09-26",
                        }
                    }
                }
            ]
        },
    }
    mock_response.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_response) as mock_post:
        sheet_id = create_sheet("token", "2026-09-20 ~ 2026-09-26", "access_token")
        assert sheet_id == "new_sheet_id_123"
        mock_post.assert_called_once()


def test_write_sheet_data_success():
    """测试成功写入数据。"""
    mock_response = MagicMock()
    mock_response.json.return_value = {"code": 0}
    mock_response.raise_for_status = MagicMock()

    values = [
        ["栏目", "2026-09-20", "2026-09-21", "2026-09-22", "合计"],
        ["日职联", 3, 2, 4, 9],
        ["总计", 4, 5, 5, 14],
    ]

    with patch("requests.post", return_value=mock_response) as mock_post:
        write_sheet_data("token", "sheet_id", values, "access_token")
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert "sheet_id!A1:E3" in payload["valueRanges"][0]["range"]


EXPECTED_HEADER = [
    "栏目",
    "2026-09-20",
    "2026-09-21",
    "2026-09-22",
    "2026-09-23",
    "2026-09-24",
    "2026-09-25",
    "2026-09-26",
    "合计",
]


def test_build_weekly_report_data_empty(app):
    """没有任何直接发布时，栏目行仍按 REPORT_TAB_NAMES 全列出并填 0。"""
    with app.app_context():
        from app.db import get_db
        from app.repository import REPORT_TAB_NAMES
        conn = get_db()
        data = build_weekly_report_data("2026-09-20", "2026-09-26", conn)

        assert data[0] == EXPECTED_HEADER
        # 标题行 + 固定栏目行 + 总计行
        assert len(data) == 1 + len(REPORT_TAB_NAMES) + 1
        assert [row[0] for row in data[1:-1]] == list(REPORT_TAB_NAMES)
        assert all(row[1:] == [0] * 8 for row in data[1:])


def test_build_weekly_report_data_with_data(app, sample_db):
    """每天一列，数值来自 direct_publish_report（与群消息同源）。"""
    from app.repository import REPORT_TAB_NAMES

    data = build_weekly_report_data("2026-09-20", "2026-09-26", sample_db)

    assert data[0] == EXPECTED_HEADER
    # 栏目行顺序固定，不随数据变化
    assert [row[0] for row in data[1:-1]] == list(REPORT_TAB_NAMES)

    rows = {row[0]: row for row in data}
    assert rows["日职联"] == ["日职联", 3, 2, 4, 1, 0, 2, 5, 17]
    assert rows["巴甲"] == ["巴甲", 1, 1, 0, 2, 3, 0, 1, 8]
    assert rows["德乙"] == ["德乙", 0, 2, 1, 0, 1, 1, 0, 5]
    # 没有发布的栏目整行为 0，但仍然保留
    assert rows["欧联"] == ["欧联", 0, 0, 0, 0, 0, 0, 0, 0]

    # 总计用 article_count（按文章去重），这里每篇只进一个栏目，所以等于纵向和
    assert rows["总计"] == ["总计", 4, 5, 5, 3, 4, 3, 6, 30]


def test_build_weekly_report_data_respects_boundary_hour(app, sample_db):
    """统计窗口按 boundary_hour 切分：文章发布于北京 12:00。

    把边界调到 11:00 后，北京 12:00 的文章就落到了「次日」的窗口里，
    因此整体数据相对 19:00 边界会右移一天。
    """
    data = build_weekly_report_data(
        "2026-09-20", "2026-09-26", sample_db, boundary_hour=11
    )

    rows = {row[0]: row for row in data}
    # 19:00 边界下日职联是 [3,2,4,1,0,2,5]，11:00 边界下右移一天
    assert rows["日职联"] == ["日职联", 0, 3, 2, 4, 1, 0, 2, 12]


def test_build_weekly_report_data_missing_day(app, sample_db):
    """某天完全没有直接发布时该列填 0，不会挤掉后面的日期列。"""
    # 删掉落在 2026-09-22 窗口内的文章（北京 09-22 12:00 = UTC 04:00）
    sample_db.execute(
        "DELETE FROM articles WHERE published_at LIKE '2026-09-22%'"
    )
    sample_db.commit()

    data = build_weekly_report_data("2026-09-20", "2026-09-26", sample_db)

    assert data[0][3] == "2026-09-22"
    rows = {row[0]: row for row in data}
    assert rows["日职联"] == ["日职联", 3, 2, 0, 1, 0, 2, 5, 13]
    assert rows["总计"] == ["总计", 4, 5, 0, 3, 4, 3, 6, 25]


def test_send_weekly_report_not_configured(app, mock_config):
    """测试飞书表格未配置时不发送。"""
    mock_config.feishu_spreadsheet_configured = False
    result = send_weekly_report(mock_config)
    assert result["sent"] is False
    assert result["reason"] == "feishu_spreadsheet_not_configured"


@pytest.mark.parametrize(
    "now, expected",
    [
        # 周六到点后：就是当天
        (datetime(2026, 9, 26, 19, 0, tzinfo=BEIJING), date(2026, 9, 26)),
        (datetime(2026, 9, 26, 23, 59, tzinfo=BEIJING), date(2026, 9, 26)),
        # 周六还没到点：回退到上一个周六
        (datetime(2026, 9, 26, 18, 59, tzinfo=BEIJING), date(2026, 9, 19)),
        # 周日/周一开机：仍指向刚过去的那个周六，所以能补发
        (datetime(2026, 9, 27, 0, 1, tzinfo=BEIJING), date(2026, 9, 26)),
        (datetime(2026, 9, 28, 10, 0, tzinfo=BEIJING), date(2026, 9, 26)),
        (datetime(2026, 10, 2, 23, 0, tzinfo=BEIJING), date(2026, 9, 26)),
    ],
)
def test_latest_due_saturday(now, expected):
    """最近一个已到点的周六，用于避免错过窗口后整周漏发。"""
    assert latest_due_saturday(now, hour=19, minute=0) == expected


def test_send_weekly_report_catches_up_after_missed_window(app, mock_config, sample_db):
    """周六 19:00 没开机，周一开机时仍然补发那一期，而不是整周跳过。"""
    now = datetime(2026, 9, 28, 10, 0, 0, tzinfo=BEIJING)  # 周一

    responses = [
        _json_response({"code": 0, "tenant_access_token": "t"}),
        _json_response(
            {"code": 0, "data": {"replies": [{"addSheet": {"properties": {"sheetId": "s1"}}}]}}
        ),
        _json_response({"code": 0}),
    ]
    with patch("requests.post", side_effect=responses):
        result = send_weekly_report(mock_config, sample_db, now=now)

    assert result["sent"] is True
    # 补发的仍是 09-26 那一期，区间以周六收尾
    assert result["period"] == "2026-09-26"
    assert result["sheet_title"] == "2026-09-20 ~ 2026-09-26"


def test_send_weekly_report_dedupes_by_period_not_today(app, mock_config, sample_db):
    """去重按「哪一期」而非「哪一天」：周六发过后，周日不会再发同一期。"""
    repo.set_setting("league_clicks_weekly_last_sent", "2026-09-26", sample_db)

    for now in (
        datetime(2026, 9, 26, 20, 0, tzinfo=BEIJING),  # 当天稍后
        datetime(2026, 9, 27, 9, 0, tzinfo=BEIJING),   # 次日
        datetime(2026, 9, 29, 9, 0, tzinfo=BEIJING),   # 下周二
    ):
        result = send_weekly_report(mock_config, sample_db, now=now)
        assert result["sent"] is False, now
        assert result["reason"] == "already_sent"


def test_send_weekly_report_before_time_targets_previous_week(app, mock_config, sample_db):
    """周六尚未到点时，指向的是上一个周六那一期。"""
    now = datetime(2026, 9, 26, 18, 0, 0, tzinfo=BEIJING)

    responses = [
        _json_response({"code": 0, "tenant_access_token": "t"}),
        _json_response(
            {"code": 0, "data": {"replies": [{"addSheet": {"properties": {"sheetId": "s1"}}}]}}
        ),
        _json_response({"code": 0}),
    ]
    with patch("requests.post", side_effect=responses):
        result = send_weekly_report(mock_config, sample_db, now=now)

    assert result["period"] == "2026-09-19"
    assert result["sheet_title"] == "2026-09-13 ~ 2026-09-19"


def test_send_weekly_report_success(app, mock_config, sample_db):
    """测试成功发送周报。"""
    # 2026-09-26 是周六，时间是 19:30
    now = datetime(2026, 9, 26, 19, 30, 0, tzinfo=BEIJING)

    mock_token_response = MagicMock()
    mock_token_response.json.return_value = {"code": 0, "tenant_access_token": "token_123"}
    mock_token_response.raise_for_status = MagicMock()

    mock_create_response = MagicMock()
    mock_create_response.json.return_value = {
        "code": 0,
        "data": {"replies": [{"addSheet": {"properties": {"sheetId": "sheet_123"}}}]},
    }
    mock_create_response.raise_for_status = MagicMock()

    mock_write_response = MagicMock()
    mock_write_response.json.return_value = {"code": 0}
    mock_write_response.raise_for_status = MagicMock()

    with patch("requests.post") as mock_post:
        mock_post.side_effect = [mock_token_response, mock_create_response, mock_write_response]
        result = send_weekly_report(mock_config, sample_db, now=now)

    assert result["sent"] is True
    assert result["sheet_id"] == "sheet_123"
    # 区间以发送当天（周六）收尾，最后一列正是群消息此刻发出的那一期
    assert result["sheet_title"] == "2026-09-20 ~ 2026-09-26"
    assert result["start_date"] == "2026-09-20"
    assert result["end_date"] == "2026-09-26"


def test_run_opens_own_connection_without_app_context(tmp_path, mock_config):
    """后台线程没有 Flask app context，_run 必须自己开连接并传下去。

    这里故意不用 app fixture：一旦 _run 回落到依赖 g 的 get_db()，
    send_weekly_report 拿到的 connection 就是 None，随后会抛
    RuntimeError: Working outside of application context。
    """
    from app.db import init_db

    db_path = tmp_path / "weekly.sqlite3"
    init_db(db_path)
    mock_config.database_path = str(db_path)

    controller = LeagueClicksWeeklyController(mock_config)
    captured: dict[str, object] = {}

    def fake_send(config, connection=None, now=None):
        captured["connection"] = connection
        # 连接不仅要非空，还得真的能查（即不依赖 app context）
        captured["settings_readable"] = (
            connection is not None
            and connection.execute("SELECT count(*) FROM settings").fetchone() is not None
        )
        controller._stop_event.set()  # 只跑一轮
        return {"sent": False, "reason": "stub"}

    with patch("app.services.league_clicks_weekly.send_weekly_report", side_effect=fake_send):
        controller._run()

    assert captured["connection"] is not None
    assert captured["settings_readable"] is True


def test_controller_start(mock_config):
    """测试控制器启动。"""
    controller = LeagueClicksWeeklyController(mock_config)

    # Mock _run 方法让它保持运行
    def mock_run():
        while not controller._stop_event.is_set():
            controller._stop_event.wait(0.1)

    with patch.object(controller, "_run", side_effect=mock_run):
        started = controller.start()
        assert started is True
        # 等待线程真正启动
        time.sleep(0.1)
        # 第二次启动应该返回 False（已在运行）
        started_again = controller.start()
        assert started_again is False
        controller.shutdown(timeout=1.0)


def test_scheduler_lifecycle(mock_config):
    """测试调度器的启动和关闭。"""
    controller = LeagueClicksWeeklyController(mock_config)
    scheduler = LeagueClicksWeeklyScheduler(controller, interval_seconds=1)

    with patch.object(controller, "_run"):
        started = scheduler.start()
        assert started is True
        time.sleep(0.1)

        # 第二次启动应该返回 False
        started_again = scheduler.start()
        assert started_again is False

        # 关闭
        shutdown_ok = scheduler.shutdown(timeout=0.5)
        assert shutdown_ok is True
        assert shutdown_ok is True
