"""从StarRocks获取彩经联赛tab点击数据"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 添加dqd-bigdata-sr技能路径
SKILL_PATH = Path.home() / ".claude/skills/dqd-bigdata-sr"
sys.path.insert(0, str(SKILL_PATH / "scripts"))

from _lib import load_config, connect

# 目标联赛配置（联赛ID -> 中文名）
TARGET_LEAGUES = {
    "lotzc": "彩经",  # 彩经总tab
    "18": "亚冠精英",
    "83": "韩K联赛",
    "17": "瑞典超",
    "53": "挪超",
    "21": "巴甲",
    "16": "美职联",
    "35": "德乙",
    "15": "日职乙",
    "8": "日职联",
    "34": "澳超",
}

def fetch_league_clicks(days: int = 30) -> dict:
    """获取最近N天的联赛tab点击数据

    Args:
        days: 查询最近多少天的数据

    Returns:
        {
            "update_time": "2026-09-22 14:30:00",
            "date_range": {"start": "2026-08-23", "end": "2026-09-22"},
            "leagues": [...],
            "daily_data": [...]
        }
    """
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)

    # 构建competition_id列表
    league_ids = list(TARGET_LEAGUES.keys())

    sql = f"""
    WITH league_clicks AS (
      SELECT
        day,
        CASE
          WHEN page = '/live_tab/lotzc' THEN 'lotzc'
          ELSE REPLACE(page, '/live_tab/league_', '')
        END as competition_id_str,
        COUNT(*) as click_cnt,
        COUNT(DISTINCT device_id) as device_cnt
      FROM hive.dwd.dwd_pb_zucai_event_sensor_src_ph
      WHERE day >= '{start_date.strftime('%Y%m%d')}'
        AND day <= '{end_date.strftime('%Y%m%d')}'
        AND (
          page = '/live_tab/lotzc'
          OR page LIKE '/live_tab/league_%'
        )
      GROUP BY day, CASE
        WHEN page = '/live_tab/lotzc' THEN 'lotzc'
        ELSE REPLACE(page, '/live_tab/league_', '')
      END
    )
    SELECT
      day,
      competition_id_str,
      click_cnt,
      device_cnt
    FROM league_clicks
    WHERE competition_id_str IN ({','.join(f"'{lid}'" for lid in league_ids)})
    ORDER BY day DESC, click_cnt DESC
    """

    conn = None
    try:
        cfg = load_config()
        conn = connect(cfg, query_timeout=300)
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()

        # 转换数据格式
        daily_data = []
        for row in rows:
            day_str = row[0]
            formatted_date = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:]}"
            daily_data.append({
                "date": formatted_date,
                "league_id": row[1],
                "league_name": TARGET_LEAGUES.get(row[1], f"联赛{row[1]}"),
                "clicks": row[2],
                "devices": row[3],
            })

        # 汇总每个联赛的总数
        league_summary = {}
        for item in daily_data:
            lid = item["league_id"]
            if lid not in league_summary:
                league_summary[lid] = {
                    "league_id": lid,
                    "league_name": item["league_name"],
                    "total_clicks": 0,
                    "total_devices": 0,
                    "active_days": 0,
                }
            league_summary[lid]["total_clicks"] += item["clicks"]
            league_summary[lid]["total_devices"] += item["devices"]
            league_summary[lid]["active_days"] += 1

        return {
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date_range": {
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d"),
            },
            "leagues": sorted(league_summary.values(), key=lambda x: x["total_clicks"], reverse=True),
            "daily_data": daily_data,
        }
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    data = fetch_league_clicks(days)

    # 保存到instance目录
    output_dir = Path(__file__).parent.parent / "instance"
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / "league_data.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✓ 数据已保存到 {output_file}")
    print(f"  查询范围: {data['date_range']['start']} ~ {data['date_range']['end']}")
    print(f"  联赛数量: {len(data['leagues'])}")
    print(f"  每日记录: {len(data['daily_data'])} 条")
