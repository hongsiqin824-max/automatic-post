#!/usr/bin/env python3
"""测试飞书 API 连接和权限"""

import os
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

from app.services.league_clicks_weekly import (
    get_tenant_access_token,
    create_sheet,
    write_sheet_data,
)


def main():
    """测试飞书 API 连接"""
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    spreadsheet_token = os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip()

    if not app_id or not app_secret or not spreadsheet_token:
        print("错误：缺少必要的环境变量")
        print(f"FEISHU_APP_ID: {'已设置' if app_id else '未设置'}")
        print(f"FEISHU_APP_SECRET: {'已设置' if app_secret else '未设置'}")
        print(f"FEISHU_SPREADSHEET_TOKEN: {'已设置' if spreadsheet_token else '未设置'}")
        return 1

    print(f"App ID: {app_id}")
    print(f"Spreadsheet Token: {spreadsheet_token}")
    print()

    # 步骤 1: 获取 tenant_access_token
    print("步骤 1: 获取 tenant_access_token...")
    try:
        tenant_token = get_tenant_access_token(app_id, app_secret)
        print(f"✅ 成功获取 token: {tenant_token[:20]}...")
        print()
    except Exception as e:
        print(f"❌ 获取 token 失败: {e}")
        return 1

    # 步骤 2: 创建测试工作表
    print("步骤 2: 创建测试工作表...")
    try:
        sheet_id = create_sheet(
            spreadsheet_token,
            "API测试工作表",
            tenant_token,
        )
        print(f"✅ 成功创建工作表，sheet_id: {sheet_id}")
        print()
    except Exception as e:
        print(f"❌ 创建工作表失败: {e}")
        return 1

    # 步骤 3: 写入测试数据
    print("步骤 3: 写入测试数据...")
    test_data = [
        ["标题1", "标题2", "标题3"],
        ["数据1", "数据2", "数据3"],
        ["测试A", "测试B", "测试C"],
    ]
    try:
        write_sheet_data(
            spreadsheet_token,
            sheet_id,
            test_data,
            tenant_token,
        )
        print("✅ 成功写入测试数据")
        print()
    except Exception as e:
        print(f"❌ 写入数据失败: {e}")
        return 1

    print("=" * 60)
    print("🎉 所有测试通过！飞书 API 连接正常。")
    print("=" * 60)
    print()
    print("请在飞书中打开以下链接查看测试结果：")
    print(f"https://feishu.cn/sheets/{spreadsheet_token}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
