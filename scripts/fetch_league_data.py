"""手动拉取联赛 tab 点击数据（平时由应用内的每日定时任务自动执行）

口径、性能约束、为什么要本地累积，全部写在 app/services/league_clicks.py 的
模块注释里，改之前先看那里。

用法：
    python3 scripts/fetch_league_data.py              # 补齐所有缺失的日期
    python3 scripts/fetch_league_data.py 2026-09-23   # 强制重拉指定日期
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.league_clicks import (
    credentials_configured,
    fetch_day,
    pending_dates,
    sync_due_days,
)


def _fetch_one(config: AppConfig, conn, day: date) -> None:
    print(f"拉取 {day}（约 1~2 分钟）...", flush=True)
    rows = fetch_day(config, day)
    repo.save_league_tab_clicks(day.isoformat(), rows, conn)
    repo.mark_league_tab_click_run(
        day.isoformat(), conn, status="SUCCESS",
        row_count=len(rows), total_cnt=sum(cnt for _, cnt in rows),
    )
    print(f"  {day} → {len(rows)} 个联赛tab，共 {sum(cnt for _, cnt in rows)} 次", flush=True)


def main() -> int:
    config = AppConfig()
    if not credentials_configured(config):
        print("未配置 StarRocks 账号，请检查 dqd-bigdata-sr 技能的 .env 或 STARROCKS_USER 环境变量")
        return 1

    conn = _connect(config.database_path)
    try:
        if len(sys.argv) > 1:
            _fetch_one(config, conn, date.fromisoformat(sys.argv[1]))
            return 0

        days = pending_dates(config, conn)
        if not days:
            print("已是最新，没有需要补的日期")
            return 0
        print(f"待补 {len(days)} 天: {', '.join(d.isoformat() for d in days)}")
        result = sync_due_days(config, conn)
        print(f"✓ 完成: 成功 {result.get('synced')} / 失败 {result.get('failed')}"
              f" / 仍待补 {result.get('remaining')} 天")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
