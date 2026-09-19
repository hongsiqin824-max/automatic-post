"""手动触发一次来源抓取量预警。

定时已经由 Web 进程内的 ``SourceReportScheduler`` 负责（见
``app/services/source_report.py``），这个脚本只留作手动补发/排查用：

  python scripts/run_daily_report.py

原先它是 crontab 的入口，但 macOS 的 cron 需要单独授予完全磁盘访问权限，
实际从未成功执行过——日志目录始终是空的。改到进程内调度后不再依赖 cron。
"""

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.config import AppConfig
from app.db import _connect, init_db
from app.services.source_report import send_due_source_report

if __name__ == "__main__":
    config = AppConfig()
    init_db(config.database_path)
    connection = _connect(config.database_path)
    try:
        result = send_due_source_report(config, connection)
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result.get("failed") else 0)
