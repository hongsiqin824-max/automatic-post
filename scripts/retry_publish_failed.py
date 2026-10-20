"""批量重试「提交懂球帝失败」的文章（一次性运维脚本）。

场景：上游 Token Service 短时间不可用，一批文章全部落 PUBLISH_FAILED；服务恢复后
用它把这批文章按原来已落好的发布模式/栏目快照重新提交。走的是页面「重试」按钮
同一条路径（create_draft_for_article），不重跑 AI 护栏，标题查重照常。

用法：
    python3 scripts/retry_publish_failed.py --match "Token Service" --limit 1   # 金丝雀
    python3 scripts/retry_publish_failed.py --match "Token Service"             # 全部
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.publisher import (
    DraftClaimSkipped,
    ManualReconcileRequired,
    TitleDuplicateBlocked,
    create_draft_for_article,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--match", required=True, help="articles.error 必须包含的子串")
    parser.add_argument("--limit", type=int, default=0, help="最多重试多少篇，0 = 全部")
    parser.add_argument("--sleep", type=float, default=1.0, help="每篇之间停顿秒数")
    args = parser.parse_args()

    config = AppConfig()
    conn = _connect(config.database_path)
    try:
        rows = conn.execute(
            "SELECT id, title_final, publish_mode FROM articles "
            "WHERE status='PUBLISH_FAILED' AND error LIKE ? "
            "AND (dqd_archive_id IS NULL OR dqd_archive_id=0) ORDER BY id",
            (f"%{args.match}%",),
        ).fetchall()
        if args.limit > 0:
            rows = rows[: args.limit]
        print(f"待重试 {len(rows)} 篇", flush=True)

        tally: dict[str, int] = {}
        for index, row in enumerate(rows, 1):
            article_id = int(row["id"])
            title = str(row["title_final"] or "")[:40]
            try:
                result = create_draft_for_article(config, conn, article_id)
                status = str(result.get("status") or "")
                outcome = status or "OK"
            except TitleDuplicateBlocked as exc:
                outcome = "TITLE_DUPLICATE"
                status = str(exc)[:60]
            except DraftClaimSkipped as exc:
                outcome = "SKIPPED"
                status = str(exc)[:60]
            except ManualReconcileRequired as exc:
                outcome = "NEEDS_RECONCILE"
                status = str(exc)[:60]
            except Exception as exc:  # noqa: BLE001 - 单篇失败不中断整批
                outcome = "FAILED"
                status = f"{type(exc).__name__}: {str(exc)[:80]}"
            tally[outcome] = tally.get(outcome, 0) + 1
            print(f"[{index}/{len(rows)}] #{article_id} {outcome:16s} {status}  | {title}", flush=True)
            if index < len(rows):
                time.sleep(args.sleep)

        print("\n汇总：", flush=True)
        for key, value in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"  {key:16s} {value}", flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
