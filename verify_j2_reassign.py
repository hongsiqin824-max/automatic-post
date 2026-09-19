"""一次性离线验证脚本：评估「AI 判不属于日职联的文章改判到日职乙」的可行性。

背景：日职乙栏目仅 95 篇，而日职联栏目下有约 289 篇被 AI guard 判定
``belongs=False``（判不属于日职联后维持草稿、栏目不变）。这些文章里究竟有多少
真的属于日职乙，决定了「二次判断 + 改判栏目」这个功能值不值得做。

本脚本在投入功能开发前先验证假设：对那批文章复用线上同一个
``check_league_membership()``，改问「是否属于日职乙」，产出命中率与逐条理由供人工抽查。

设计约束：
- 只读数据库，绝不写库，不改任何文章状态。
- 结果以 JSONL 增量落盘，支持断点续跑：重跑自动跳过已判定的文章，避免重复计费。
- ``--dry-run`` 零成本预览候选池与词典预筛统计，不调用任何 LLM。
- 词典预筛只作为并行统计维度（评估「词典预筛 + AI 确认」混合方案能省多少调用），
  不参与也不干扰 AI 判定结果。

用法：
    python3 verify_j2_reassign.py --dry-run          # 零成本预览
    python3 verify_j2_reassign.py --limit 20         # 小批量试跑
    python3 verify_j2_reassign.py                    # 全量
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from app.config import AppConfig
from app.services.quality import (
    LEAGUE_GUARD_MIN_CONFIDENCE,
    LLMCallError,
    LLMService,
    check_league_membership,
    html_to_text,
)

SOURCE_TAB_NAME = "日职联"
TARGET_TAB_NAME = "日职乙"
DEFAULT_OUTPUT = Path("instance") / "j2_verify_results.jsonl"

# 近似的 J2 球队中文名，仅用于评估「词典预筛」的有效性，不作为判定依据。
# 升降级每年变动，这份名单不追求权威；它只回答一个问题：如果先用球队名预筛、
# 命中才调 AI，能把 LLM 调用量压到多少。
J2_TEAM_KEYWORDS = (
    "水户", "栃木", "群马草津温泉", "大宫松鼠", "千叶市原", "甲府风林",
    "清水心跳", "藤枝MYFC", "磐田喜悦", "爱媛", "德岛漩涡", "今治",
    "长崎成功丸", "熊本深红", "大分三神", "山形山神", "秋田蓝闪电",
    "仙台维加泰", "冈山绿雉", "山口雷诺法", "鹿儿岛联", "新潟天鹅",
    "札幌冈萨多", "富山",
)
# 明确的干扰项：这些词出现时容易被误当成日职乙（德乙/日职丙等）。
DECOY_KEYWORDS = ("德乙", "德国乙级", "圣保利", "卡尔斯鲁厄", "J3", "日职丙")


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(AppConfig().database_path)


def _load_target_definition(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT ai_league_guard_definition FROM tabs WHERE name = ?",
        (TARGET_TAB_NAME,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"数据库中找不到栏目「{TARGET_TAB_NAME}」")
    definition = str(row[0] or "").strip()
    if not definition:
        raise SystemExit(
            f"栏目「{TARGET_TAB_NAME}」的 ai_league_guard_definition 为空，无法判定"
        )
    return definition


def _iter_candidates(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    """产出被 AI 判定不属于日职联的文章（候选改判池）。"""

    rows = conn.execute(
        """
        SELECT id,
               COALESCE(title_final, title_original) AS title,
               body_html,
               quality_json
        FROM articles
        WHERE quality_json LIKE '%league_guard%'
        ORDER BY id
        """
    ).fetchall()
    for article_id, title, body_html, quality_json in rows:
        try:
            guard = (json.loads(quality_json or "{}") or {}).get("league_guard") or {}
        except (TypeError, ValueError):
            continue
        if str(guard.get("tab_name") or "") != SOURCE_TAB_NAME:
            continue
        verdict = guard.get("verdict") or {}
        if verdict.get("belongs") is not False:
            continue
        yield {
            "article_id": int(article_id),
            "title": str(title or ""),
            "body_html": str(body_html or ""),
            "j1_reason": str(verdict.get("reason") or ""),
            "j1_confidence": verdict.get("confidence"),
        }


def _keyword_hits(blob: str, keywords: tuple[str, ...]) -> list[str]:
    return [word for word in keywords if word and word in blob]


def _load_done_ids(output_path: Path) -> set[int]:
    if not output_path.exists():
        return set()
    done: set[int] = set()
    with output_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(int(json.loads(line)["article_id"]))
            except (TypeError, ValueError, KeyError):
                continue
    return done


def _print_dry_run(candidates: list[dict[str, Any]]) -> None:
    prefilter_hit = 0
    title_hit = 0
    decoy_only = 0
    keyword_counter: Counter[str] = Counter()
    title_keyword_counter: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    title_samples: list[dict[str, Any]] = []

    for item in candidates:
        blob = f"{item['title']} {html_to_text(item['body_html'])[:4000]} {item['j1_reason']}"
        hits = _keyword_hits(blob, J2_TEAM_KEYWORDS)
        title_hits = _keyword_hits(item["title"], J2_TEAM_KEYWORDS)
        decoys = _keyword_hits(blob, DECOY_KEYWORDS)
        keyword_counter.update(hits)
        title_keyword_counter.update(title_hits)
        if title_hits:
            title_hit += 1
            if len(title_samples) < 20:
                title_samples.append({**item, "hits": title_hits, "decoys": decoys})
        if hits:
            prefilter_hit += 1
            if len(samples) < 8:
                samples.append({**item, "hits": hits, "decoys": decoys})
        elif decoys:
            decoy_only += 1

    total = len(candidates)
    print(f"候选池（被判不属于「{SOURCE_TAB_NAME}」）：{total} 篇")
    if not total:
        return
    print()
    print("【方案对比】两种词典预筛范围的召回量")
    print(f"  A. 标题+正文命中：{prefilter_hit} 篇 ({prefilter_hit / total * 100:.1f}%)"
          f" → 省 {total - prefilter_hit} 次调用")
    print(f"  B. 仅标题命中　：{title_hit} 篇 ({title_hit / total * 100:.1f}%)"
          f" → 省 {total - title_hit} 次调用")
    print(f"  仅命中干扰词（德乙/J3 等）：{decoy_only} 篇")
    print("\n标题命中球队词频 top10：")
    for word, count in title_keyword_counter.most_common(10):
        print(f"  {word}: {count}")
    print(f"\n--- 方案A（标题+正文）命中样本 8 条：观察误命中 ---")
    for item in samples:
        print(f"#{item['article_id']} {item['title'][:44]}")
        print(f"    命中: {item['hits']}"
              + (f" | 干扰词: {item['decoys']}" if item["decoys"] else ""))
    print(f"\n--- 方案B（仅标题）命中样本（最多 20 条，供人工核对精度）---")
    for item in title_samples:
        print(f"#{item['article_id']} {item['title'][:44]}")
        print(f"    命中: {item['hits']}")
        print(f"    判不属于{SOURCE_TAB_NAME}的理由: {item['j1_reason'][:80]}")
    print("\n（dry-run 未调用任何 LLM，数据库只读）")


def _run_verification(
    candidates: list[dict[str, Any]],
    definition: str,
    output_path: Path,
    sleep_seconds: float,
) -> None:
    config = AppConfig()
    if not config.llm_configured:
        raise SystemExit("LLM_API_KEY 未配置，无法执行真实判定（可先用 --dry-run）")
    llm = LLMService(
        config.llm_api_key,
        config.llm_base_url,
        config.llm_model,
        config.llm_timeout,
        config.llm_max_retries,
        config.llm_retry_delay_seconds,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()
    total = len(candidates)
    print(f"开始判定 {total} 篇是否属于「{TARGET_TAB_NAME}」，模型={config.llm_model}")
    print(f"结果追加写入 {output_path}（可中断，重跑自动续跑）\n")

    with output_path.open("a", encoding="utf-8") as handle:
        for index, item in enumerate(candidates, start=1):
            blob = f"{item['title']} {html_to_text(item['body_html'])[:4000]} {item['j1_reason']}"
            record: dict[str, Any] = {
                "article_id": item["article_id"],
                "title": item["title"],
                "j1_reason": item["j1_reason"],
                "prefilter_hits": _keyword_hits(blob, J2_TEAM_KEYWORDS),
                "decoy_hits": _keyword_hits(blob, DECOY_KEYWORDS),
            }
            try:
                verdict = check_league_membership(
                    item["title"],
                    item["body_html"],
                    TARGET_TAB_NAME,
                    definition,
                    llm,
                )
            except LLMCallError as exc:
                record["error"] = f"{exc.category}: {exc}"
                stats["error"] += 1
                label = "ERR "
            else:
                confident = (
                    verdict["belongs"]
                    and verdict["confidence"] >= LEAGUE_GUARD_MIN_CONFIDENCE
                )
                record["verdict"] = verdict
                record["would_reassign"] = confident
                if confident:
                    stats["reassign"] += 1
                    label = "→J2 "
                elif verdict["belongs"]:
                    stats["belongs_low_confidence"] += 1
                    label = "低置信"
                else:
                    stats["not_j2"] += 1
                    label = "非J2 "

            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            print(f"[{index}/{total}] {label} #{item['article_id']} {item['title'][:40]}")
            if sleep_seconds > 0 and index < total:
                time.sleep(sleep_seconds)

    print("\n=== 本次判定汇总 ===")
    print(f"可改判到{TARGET_TAB_NAME}（belongs 且置信度≥{LEAGUE_GUARD_MIN_CONFIDENCE}）："
          f"{stats['reassign']}")
    print(f"判属于但置信度不足：{stats['belongs_low_confidence']}")
    print(f"判不属于{TARGET_TAB_NAME}：{stats['not_j2']}")
    print(f"调用失败：{stats['error']}")
    judged = stats["reassign"] + stats["belongs_low_confidence"] + stats["not_j2"]
    if judged:
        print(f"改判命中率：{stats['reassign'] / judged * 100:.1f}%")
    print(f"\n完整结果：{output_path}（建议人工抽查 would_reassign=true 的条目）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"验证「判不属于{SOURCE_TAB_NAME}的文章」中有多少属于{TARGET_TAB_NAME}"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览候选池与词典预筛统计，不调用 LLM")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少篇（0 表示全部）")
    parser.add_argument("--db", default=None, help="数据库路径（默认取 AppConfig）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"结果 JSONL 路径（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--sleep", type=float, default=0.3,
                        help="每次调用之间的间隔秒数，避免打爆 API（默认 0.3）")
    parser.add_argument("--restart", action="store_true",
                        help="忽略已有结果重新判定（默认跳过已判定的文章）")
    parser.add_argument("--prefilter", choices=("none", "body", "title"), default="none",
                        help="只判定词典预筛命中的文章："
                             "body=标题+正文命中(方案A)，title=仅标题命中(方案B)，none=全部")
    parser.add_argument("--invert-prefilter", action="store_true",
                        help="反转预筛：只判定词典*未*命中的文章，用于测量漏召回率")
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    output_path = Path(args.output)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        candidates = list(_iter_candidates(conn))
        definition = "" if args.dry_run else _load_target_definition(conn)
    finally:
        conn.close()

    if args.dry_run:
        if args.limit > 0:
            candidates = candidates[: args.limit]
        _print_dry_run(candidates)
        return 0

    if args.prefilter != "none":
        before = len(candidates)
        filtered = []
        for item in candidates:
            if args.prefilter == "title":
                scope = item["title"]
            else:
                scope = (
                    f"{item['title']} {html_to_text(item['body_html'])[:4000]} "
                    f"{item['j1_reason']}"
                )
            if bool(_keyword_hits(scope, J2_TEAM_KEYWORDS)) != bool(args.invert_prefilter):
                filtered.append(item)
        candidates = filtered
        label = "未命中" if args.invert_prefilter else "命中"
        print(f"词典预筛（{args.prefilter}，只取{label}）：{before} → {len(candidates)} 篇")

    if not args.restart:
        done = _load_done_ids(output_path)
        if done:
            before = len(candidates)
            candidates = [c for c in candidates if c["article_id"] not in done]
            print(f"断点续跑：跳过已判定 {before - len(candidates)} 篇")
    if args.limit > 0:
        candidates = candidates[: args.limit]
    if not candidates:
        print("没有待判定的文章。")
        return 0

    _run_verification(candidates, definition, output_path, args.sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
