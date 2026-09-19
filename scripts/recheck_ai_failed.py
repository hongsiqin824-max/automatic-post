"""重新质检因 AI 服务失败而待审的文章（分批安全执行）"""
import sqlite3
from pathlib import Path
from datetime import datetime


def recheck_ai_failed_articles(batch_size: int = 30, dry_run: bool = True):
    """
    重新质检因 AI 服务失败的文章

    Args:
        batch_size: 每批处理的文章数量（默认 30）
        dry_run: 是否只查看不执行（默认 True，安全起见）
    """
    db_path = Path(__file__).parent.parent / "instance" / "automatic_post.sqlite3"

    if not db_path.exists():
        print(f"❌ 数据库文件不存在: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))

    try:
        # 1. 统计总数
        cursor = conn.execute("""
            SELECT COUNT(*)
            FROM articles
            WHERE status = 'NEEDS_REVIEW'
              AND quality_json LIKE '%AI 服务%'
        """)
        total = cursor.fetchone()[0]
        print(f"📊 总共有 {total} 篇因 AI 服务失败的文章")

        if total == 0:
            print("✅ 没有需要重新质检的文章")
            return

        # 2. 查看本批文章详情
        cursor = conn.execute(f"""
            SELECT id, source, title_final, created_at
            FROM articles
            WHERE status = 'NEEDS_REVIEW'
              AND quality_json LIKE '%AI 服务%'
            ORDER BY created_at
            LIMIT {batch_size}
        """)
        articles = cursor.fetchall()

        print(f"\n本批将处理 {len(articles)} 篇文章：")
        print(f"{'ID':<8} {'来源':<15} {'创建时间':<20} 标题")
        print("-" * 80)
        for article_id, source, title, created_at in articles[:5]:
            title_display = (title or "")[:30]
            print(f"{article_id:<8} {source:<15} {created_at:<20} {title_display}...")
        if len(articles) > 5:
            print(f"... 还有 {len(articles) - 5} 篇")

        # 3. 执行重置
        if dry_run:
            print(f"\n⚠️  DRY RUN 模式 - 未实际修改数据")
            print(f"执行命令：python scripts/recheck_ai_failed.py --execute")
            return

        article_ids = [row[0] for row in articles]
        placeholders = ",".join("?" * len(article_ids))

        cursor = conn.execute(f"""
            UPDATE articles
            SET status = 'RECEIVED',
                quality_json = '{{}}'
            WHERE id IN ({placeholders})
        """, article_ids)

        conn.commit()

        print(f"\n✅ 已重置 {cursor.rowcount} 篇文章状态为 RECEIVED")
        print(f"⏳ 这些文章将在下次质检任务中重新处理（通常 1-2 分钟内）")
        print(f"📈 剩余待处理：{total - len(articles)} 篇")

    except Exception as e:
        print(f"\n❌ 执行失败：{e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    # 第一次运行：先看看情况（dry_run=True）
    # python scripts/recheck_ai_failed.py

    # 确认没问题后执行
    # python scripts/recheck_ai_failed.py --execute

    execute = "--execute" in sys.argv

    if execute:
        print("🚀 开始执行重置...\n")
        recheck_ai_failed_articles(batch_size=30, dry_run=False)
    else:
        print("🔍 预览模式（不会修改数据）\n")
        recheck_ai_failed_articles(batch_size=30, dry_run=True)
