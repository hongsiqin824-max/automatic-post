"""回填 quality_json.league_guard.guard_tab_name（被 AI 护栏校验的原栏目）

``guard_tab_name`` / ``guard_tab_id`` 比 ``league_guard`` 本身晚引入，历史记录里
只有 ``tab_name``，而它记的是最终落点：改挂过的文章拿它当原栏目会拼出「不属于
「亚冠2」已改挂「亚冠2」」这种自相矛盾的列表文案。

回填来源：
  * 未改挂的记录：``tab_name`` 就是被校验的栏目，直接复制。
  * 改挂过的记录：从 ``reason``（护栏当时拼的自然语言）里解析「不属于「X」」。
    解析不出、或解析结果等于落点栏目时跳过，交给展示层的不点名兜底。

用法（默认只统计不写库）：
    python3 scripts/backfill_league_guard_tab_name.py
    python3 scripts/backfill_league_guard_tab_name.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "instance" / "automatic_post.sqlite3"
# 护栏文案统一是「AI 判断本篇不属于「日职联」，……」
GUARD_TAB_PATTERN = re.compile(r"不属于「([^」]+)」")


def _resolve_tab_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["name"]): int(row["id"])
        for row in conn.execute("SELECT id, name FROM tabs")
    }


def backfill(*, apply: bool = False) -> None:
    if not DB_PATH.exists():
        print(f"数据库文件不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        tab_ids = _resolve_tab_ids(conn)
        rows = conn.execute(
            "SELECT id, quality_json FROM articles WHERE quality_json LIKE '%league_guard%'"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        stats = {"已有": 0, "复制落点": 0, "解析理由": 0, "无法确定": 0}
        samples: list[str] = []
        for row in rows:
            try:
                quality = json.loads(row["quality_json"] or "{}")
            except (TypeError, ValueError):
                continue
            guard = quality.get("league_guard")
            if not isinstance(guard, dict):
                continue
            if guard.get("guard_tab_name"):
                stats["已有"] += 1
                continue

            reassigned = guard.get("reassigned_tab_id") is not None and bool(
                guard.get("fallback_tab_name")
            )
            if not reassigned:
                name = str(guard.get("tab_name") or "")
                kind = "复制落点"
            else:
                match = GUARD_TAB_PATTERN.search(str(guard.get("reason") or ""))
                name = match.group(1) if match else ""
                if name == str(guard.get("fallback_tab_name") or ""):
                    name = ""
                kind = "解析理由"

            if not name:
                stats["无法确定"] += 1
                continue

            stats[kind] += 1
            guard["guard_tab_name"] = name
            if name in tab_ids:
                guard["guard_tab_id"] = tab_ids[name]
            updates.append((
                json.dumps(quality, ensure_ascii=False, separators=(",", ":")),
                int(row["id"]),
                row["quality_json"],
            ))
            if kind == "解析理由" and len(samples) < 5:
                samples.append(
                    f"  #{row['id']}  原栏目「{name}」 → 落点「{guard.get('fallback_tab_name')}」"
                )

        print(f"扫描到 league_guard 记录: {sum(stats.values())}")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        if samples:
            print("改挂记录解析样例:")
            print("\n".join(samples))

        if not apply:
            print(f"\n（试运行）将更新 {len(updates)} 条，加 --apply 才写库")
            return

        # 服务在跑，读到写之间同一篇可能被流水线改过 quality_json。带上读到的原值
        # 做 CAS，被改过的那几条本轮跳过即可（下一轮再补，或由新代码直接写对）。
        with conn:
            cursor = conn.executemany(
                "UPDATE articles SET quality_json=? WHERE id=? AND quality_json=?",
                updates,
            )
        print(f"\n已更新 {cursor.rowcount} 条，跳过 {len(updates) - cursor.rowcount} 条（期间被流水线改写）")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写入数据库")
    backfill(apply=parser.parse_args().apply)
