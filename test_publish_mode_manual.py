#!/usr/bin/env python3
"""Read-only diagnostic for the configured DQD publish modes."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def main() -> int:
    database = Path(__file__).parent / "instance" / "automatic_post.sqlite3"
    if not database.exists():
        print(f"数据库不存在: {database}")
        return 1

    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        tab_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(tabs)").fetchall()
        }
        article_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(articles)").fetchall()
        }
        required_tab_columns = {"publish_mode"}
        required_article_columns = {"publish_mode", "publish_mode_decided_at"}
        if not required_tab_columns.issubset(tab_columns):
            print("tabs 表缺少 publish_mode，请先启动一次新版本服务完成迁移。")
            return 1
        if not required_article_columns.issubset(article_columns):
            print("articles 表缺少发布模式快照字段，请先启动一次新版本服务完成迁移。")
            return 1

        rows = connection.execute(
            """
            SELECT id, name, backend_tab_id, enabled, publish_mode
            FROM tabs
            ORDER BY name COLLATE NOCASE, id
            """
        ).fetchall()
        print(f"栏目发布模式（{len(rows)} 个）")
        for row in rows:
            mode = "直接发布" if int(row["publish_mode"]) == 1 else "创建草稿"
            enabled = "启用" if int(row["enabled"]) == 1 else "停用"
            print(
                f"{row['id']:>3}  {row['name']:<16} "
                f"后台 ID {row['backend_tab_id']:<6}  {enabled}  {mode}"
            )

        overrides = connection.execute(
            """
            SELECT code, display_name, publish_mode_override
            FROM sources
            WHERE publish_mode_override IS NOT NULL
            ORDER BY code
            """
        ).fetchall()
        print(f"\n来源模式例外（{len(overrides)} 个）")
        for row in overrides:
            mode = "强制直接发布" if int(row["publish_mode_override"]) == 1 else "强制创建草稿"
            print(f"{row['code']:<20} {row['display_name']:<20} {mode}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
