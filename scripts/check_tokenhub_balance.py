"""读取 TokenHub / AIGW 钱包余额，可选立即跑一次告警检查。

定时检查由 Web 进程内的 ``BalanceAlertScheduler`` 负责（见
``app/services/balance_alert.py``），这个脚本用于手动查看/排查：

  python scripts/check_tokenhub_balance.py                 # 只打印 TokenHub 余额
  python scripts/check_tokenhub_balance.py --wallet aigw   # 只打印 AIGW 余额
  python scripts/check_tokenhub_balance.py --alert         # 低于阈值时推飞书（遵守冷却）
"""

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.config import AppConfig
from app.db import _connect, init_db
from app.services.balance_alert import (
    TokenHubError,
    aigw_wallet,
    check_and_alert,
    fetch_balance,
    tokenhub_wallet,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wallet", choices=["tokenhub", "aigw"], default="tokenhub")
    parser.add_argument("--alert", action="store_true", help="余额不足时推送飞书告警")
    args = parser.parse_args()
    config = AppConfig()
    wallet = aigw_wallet(config) if args.wallet == "aigw" else tokenhub_wallet(config)

    if not args.alert:
        try:
            balance = fetch_balance(config, wallet)
        except TokenHubError as exc:
            print(f"读取失败: {exc}", file=sys.stderr)
            raise SystemExit(1)
        balance["threshold"] = wallet.threshold
        balance["low"] = balance["balance"] < wallet.threshold
        print(json.dumps(balance, ensure_ascii=False, indent=2))
        raise SystemExit(2 if balance["low"] else 0)

    init_db(config.database_path)
    connection = _connect(config.database_path)
    try:
        result = check_and_alert(config, connection, wallet=wallet)
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result.get("failed") else 0)
