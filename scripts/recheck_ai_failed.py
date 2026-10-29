"""批量重新质检「AI 服务调用失败」而落待审的文章（一次性运维脚本）。

场景：LLM 账户欠费 / 网络中断期间，质检全部失败落 NEEDS_REVIEW，自动瞬时重试
（AUTOMATIC_POST_TRANSIENT_RECHECK_*）也在故障期内耗尽了次数。服务恢复后用它把
这批文章按批放回 RECEIVED，由后台 worker 的历史遗留扫描（每 10 分钟、最多 200 篇）
重新跑完整质检。走的是页面「重新质检」按钮同一条路径（repo.claim_quality_recheck），
会写 QUALITY_RECHECK_REQUESTED 事件。

用法：
    python3 scripts/recheck_ai_failed.py --since 2026-10-25T18:00            # 预览
    python3 scripts/recheck_ai_failed.py --since 2026-10-25T18:00 --execute  # 一批
    python3 scripts/recheck_ai_failed.py --since 2026-10-25T18:00 --execute --wait  # 逐批跑完
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import repository as repo
from app.config import AppConfig
from app.db import _connect


def _pending_ids(conn, since: str, limit: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM articles
        WHERE status='NEEDS_REVIEW'
          AND created_at >= ?
          AND json_extract(quality_json, '$.reason') LIKE '%AI 服务%'
        ORDER BY id
        LIMIT ?
        """,
        (since, limit),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _received_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM articles WHERE status='RECEIVED'").fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="created_at 下限（UTC ISO，如 2026-10-25T18:00）")
    parser.add_argument("--batch", type=int, default=30, help="每批重置多少篇")
    parser.add_argument("--execute", action="store_true", help="不加则只预览")
    parser.add_argument("--wait", action="store_true", help="等 worker 消化完再放下一批，直到全部跑完")
    parser.add_argument("--idle-threshold", type=int, default=10, help="RECEIVED 少于多少篇视为消化完")
    args = parser.parse_args()

    config = AppConfig()
    conn = _connect(config.database_path)
    try:
        total = len(_pending_ids(conn, args.since, 100000))
        print(f"待重跑 {total} 篇（created_at >= {args.since}）", flush=True)
        if total == 0:
            return 0
        if not args.execute:
            for article_id in _pending_ids(conn, args.since, 5):
                row = repo.get_article(article_id, conn)
                print(f"  #{article_id} {row['source']} {row['title_final'][:40]}")
            print("预览模式，加 --execute 执行", flush=True)
            return 0

        done = 0
        batch_no = 0
        while True:
            ids = _pending_ids(conn, args.since, args.batch)
            if not ids:
                break
            batch_no += 1
            ok = 0
            for article_id in ids:
                if repo.claim_quality_recheck(article_id, conn) is not None:
                    ok += 1
            done += ok
            print(
                f"[{time.strftime('%H:%M:%S')}] 第 {batch_no} 批：重置 {ok}/{len(ids)} 篇 → RECEIVED，"
                f"累计 {done}/{total}",
                flush=True,
            )
            if not args.wait:
                break
            # 等 worker 把这批消化掉（历史遗留扫描每 10 分钟一次，只收 updated_at 超 10 分钟的）
            deadline = time.time() + 40 * 60
            while time.time() < deadline:
                time.sleep(60)
                remaining = _received_count(conn)
                print(f"[{time.strftime('%H:%M:%S')}]   RECEIVED 还剩 {remaining}", flush=True)
                if remaining <= args.idle_threshold:
                    break
            else:
                print("等待超时，停止放下一批；可稍后再跑一次", flush=True)
                return 1
        print(f"完成：共重置 {done} 篇", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
