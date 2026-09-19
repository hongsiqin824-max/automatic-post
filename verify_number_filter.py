#!/usr/bin/env python3
"""验证标题查重中数字硬过滤的影响

分析去掉 has_conflicting_numbers 硬过滤后，LLM 能否正确区分：
1. 真重复（应该拦截但被数字过滤放过的）
2. 不同事件（数字不同确实是不同事件的）

使用最近7天的已发布文章进行回放测试。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import AppConfig
from app.services import title_dedup
from app import repository as repo


def get_published_articles_last_7days(connection) -> list[dict[str, Any]]:
    """获取最近7天已发布的文章"""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")

    rows = connection.execute(
        """
        SELECT id, title_final, channels_json, status, published_at, dqd_archive_id, created_at
        FROM articles
        WHERE status = 'PUBLISHED'
          AND published_at >= ?
        ORDER BY published_at
        """,
        (since,),
    ).fetchall()

    return [
        {
            "id": row[0],
            "title_final": row[1],
            "channels": json.loads(row[2] or "[]"),
            "status": row[3],
            "published_at": row[4],
            "dqd_archive_id": row[5],
            "created_at": row[6],
        }
        for row in rows
    ]


def select_candidates_without_number_filter(
    candidate_title: str,
    articles: list[dict[str, Any]],
    *,
    candidate_id: int,
    channels: Any,
    dice_min: float,
    lcs_min: int,
    limit: int,
) -> list[dict[str, Any]]:
    """召回候选，但不使用数字冲突过滤（对比用）"""

    ranked: list[dict[str, Any]] = []
    for index, article in enumerate(articles or []):
        if not isinstance(article, dict):
            continue
        article_id = int(article.get("id") or 0)
        if article_id <= 0 or article_id >= candidate_id:
            continue
        title = str(article.get("title_final") or "")
        if not title.strip():
            continue
        shared = title_dedup.shared_channels(channels, article.get("channels"))
        if not shared:
            continue

        # 注意：这里不调用 has_conflicting_numbers，直接计算相似度
        score = title_dedup.score_title_similarity(candidate_title, title)

        if min(len(title_dedup.normalize_title(candidate_title)),
               len(title_dedup.normalize_title(title))) < 8:
            continue
        if not title_dedup.is_recall_hit(score, dice_min=dice_min, lcs_min=lcs_min):
            continue

        # 记录是否有数字冲突（用于分析）
        has_number_conflict = title_dedup.has_conflicting_numbers(candidate_title, title)

        ranked.append({
            "article": article,
            "score": score,
            "lexical_score": title_dedup.lexical_score(score),
            "shared_channels": shared,
            "index": index,
            "has_number_conflict": has_number_conflict,
        })

    ranked.sort(key=lambda item: (-item["lexical_score"], item["index"]))
    return ranked[: max(1, int(limit))]


def main():
    config = AppConfig()
    db_path = config.database_path

    print(f"数据库路径: {db_path}")
    print(f"查重配置:")
    print(f"  - title_dedup_enabled: {config.title_dedup_enabled}")
    print(f"  - title_dedup_hours: {config.title_dedup_hours}")
    print(f"  - title_dedup_dice_min: {config.title_dedup_dice_min}")
    print(f"  - title_dedup_lcs_min: {config.title_dedup_lcs_min}")
    print(f"  - title_dedup_max_candidates: {config.title_dedup_max_candidates}")
    print()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row

    print("正在加载最近7天的已发布文章...")
    published_articles = get_published_articles_last_7days(connection)
    print(f"找到 {len(published_articles)} 篇已发布文章\n")

    print("=" * 100)
    print("开始回放测试：模拟去掉数字硬过滤后的效果")
    print("=" * 100)
    print()

    # 统计数据
    total_checked = 0
    with_number_conflict_filtered = 0  # 当前被数字过滤掉的候选数
    potential_false_negatives = []  # 可能的漏报（被数字过滤但LLM认为重复）
    potential_false_positives = []   # 可能的误报（数字不同但LLM认为重复）

    # 对每篇文章进行回放测试
    for i, article in enumerate(published_articles):
        article_id = article["id"]
        title = article["title_final"]

        # 查询候选池（模拟发布前的状态）
        candidates_pool = [a for a in published_articles if a["id"] < article_id]

        if not candidates_pool:
            continue

        # 方案1：当前逻辑（带数字过滤）
        candidates_with_filter = title_dedup.select_candidates(
            title,
            candidates_pool,
            candidate_id=article_id,
            channels=article["channels"],
            dice_min=config.title_dedup_dice_min,
            lcs_min=config.title_dedup_lcs_min,
            limit=config.title_dedup_max_candidates,
        )

        # 方案2：去掉数字过滤
        candidates_without_filter = select_candidates_without_number_filter(
            title,
            candidates_pool,
            candidate_id=article_id,
            channels=article["channels"],
            dice_min=config.title_dedup_dice_min,
            lcs_min=config.title_dedup_lcs_min,
            limit=config.title_dedup_max_candidates,
        )

        # 如果两个方案的候选数量不同，说明有候选被数字过滤掉了
        if len(candidates_with_filter) != len(candidates_without_filter):
            total_checked += 1

            # 找出被数字过滤掉的候选
            filtered_out = []
            for c in candidates_without_filter:
                if c.get("has_number_conflict"):
                    # 检查是否在 with_filter 中
                    found = False
                    for c2 in candidates_with_filter:
                        if c2["article"]["id"] == c["article"]["id"]:
                            found = True
                            break
                    if not found:
                        filtered_out.append(c)
                        with_number_conflict_filtered += 1

            if filtered_out:
                print(f"\n[{i+1}/{len(published_articles)}] 文章 #{article_id}")
                print(f"标题: {title}")
                print(f"发布时间: {article['published_at']}")
                print(f"\n被数字硬过滤掉的候选（{len(filtered_out)}个）：")

                for fc in filtered_out:
                    other_article = fc["article"]
                    other_title = other_article["title_final"]

                    print(f"\n  候选 #{other_article['id']}: {other_title}")
                    print(f"  词法相似度: {fc['lexical_score']:.3f}")
                    print(f"  Dice: {fc['score']['bigram_dice']:.3f}")
                    print(f"  LCS: {fc['score']['lcs_chars']} 字符")

                    # 提取数字序列
                    num1 = title_dedup.number_sequences(title)
                    num2 = title_dedup.number_sequences(other_title)
                    print(f"  数字序列: {num1} vs {num2}")

                    # 调用LLM判断（如果配置了LLM）
                    if config.llm_configured:
                        try:
                            result = title_dedup.check_title_duplicate(
                                config,
                                article,
                                [fc],
                                publish_mode=1,
                            )

                            if result["outcome"] == "duplicate":
                                print(f"  ⚠️  LLM判断: 重复！原因: {result['reason']}")
                                potential_false_negatives.append({
                                    "article_id": article_id,
                                    "title": title,
                                    "matched_id": other_article["id"],
                                    "matched_title": other_title,
                                    "reason": result["reason"],
                                    "lexical_score": fc['lexical_score'],
                                })
                            else:
                                print(f"  ✓ LLM判断: 不重复。原因: {result['reason']}")
                        except Exception as e:
                            print(f"  ⚠️  LLM调用失败: {e}")
                    else:
                        print(f"  (未配置LLM，跳过语义判断)")

                print("-" * 100)

        # 进度提示
        if (i + 1) % 100 == 0:
            print(f"\n进度: {i+1}/{len(published_articles)} 篇已检查\n")

    # 输出统计结果
    print("\n")
    print("=" * 100)
    print("回放测试结果")
    print("=" * 100)
    print()
    print(f"总检查文章数: {len(published_articles)}")
    print(f"发现数字过滤差异的文章数: {total_checked}")
    print(f"被数字硬过滤掉的候选总数: {with_number_conflict_filtered}")
    print()
    print(f"可能的漏报（真重复但被数字过滤放过）: {len(potential_false_negatives)}")

    if potential_false_negatives:
        print("\n详细列表：")
        for item in potential_false_negatives:
            print(f"\n  文章 #{item['article_id']}: {item['title']}")
            print(f"  匹配 #{item['matched_id']}: {item['matched_title']}")
            print(f"  LLM判断: {item['reason']}")
            print(f"  词法相似度: {item['lexical_score']:.3f}")

    print()
    print("=" * 100)
    print("建议：")
    if len(potential_false_negatives) > 0:
        print("✓ 发现漏报案例，建议去掉数字硬过滤，交给LLM判断")
        print("✓ 同时需要优化few-shot，确保LLM能正确区分不同事件")
    else:
        print("✓ 未发现明显漏报，当前数字过滤逻辑基本合理")
        print("✓ 但仍建议在更大数据集上验证")
    print("=" * 100)

    connection.close()


if __name__ == "__main__":
    main()
