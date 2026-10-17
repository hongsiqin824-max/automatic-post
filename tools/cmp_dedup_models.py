"""Compare title-dedup verdicts between two models on real historical prompts.

Read-only: samples past TITLE_DUPLICATE_* events, rebuilds the exact production
prompt via title_dedup.build_prompt, and runs each model on it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import title_dedup as td
from app.services.quality import LLMService

DB = "instance/automatic_post.sqlite3"
BASE = os.environ["LLM_BASE_URL"]
KEY = os.environ["LLM_API_KEY"]
MODELS = ["deepseek-v4-pro", "glm-5.3-flash"]
LIMIT = int(os.environ.get("CMP_LIMIT", "60"))


def load_cases(limit: int) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select article_id, payload_json from article_events "
        "where event_type in ('TITLE_DUPLICATE_DETECTED','TITLE_DUPLICATE_RELEASED') "
        "order by id desc limit 4000"
    ).fetchall()

    def title_of(aid: int) -> str:
        r = conn.execute(
            "select title_final, title_original from articles where id=?", (aid,)
        ).fetchone()
        if not r:
            return ""
        return str(r["title_final"] or r["title_original"] or "")

    cases: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        if len(cases) >= limit:
            break
        payload = json.loads(row["payload_json"])
        matched = payload.get("matched") or {}
        mid = matched.get("id")
        cand_title = title_of(row["article_id"])
        m_title = str(matched.get("title") or "")
        if not mid or not cand_title or not m_title:
            continue
        key = (int(row["article_id"]), int(mid))
        if key in seen:
            continue
        seen.add(key)
        body = payload.get("body_confirm") or {}
        cases.append({
            "article_id": int(row["article_id"]),
            "matched_id": int(mid),
            "candidate_title": cand_title,
            "matched_title": m_title,
            "hist_outcome": payload.get("outcome"),
            "hist_body_dup": body.get("duplicate"),
        })
    conn.close()
    return cases


def run_one(model: str, case: dict) -> dict:
    svc = LLMService(KEY, BASE, model, timeout=90, max_retries=3, retry_delay_seconds=6.0)
    candidates = [{"article": {"id": case["matched_id"], "title_final": case["matched_title"]}}]
    prompt = td.build_prompt(case["candidate_title"], candidates)
    try:
        raw = svc.chat_json(prompt)
        decision = td.parse_decision(raw, candidates)
        return {"ok": True, "duplicate": decision["duplicate"], "reason": decision["reason"]}
    except Exception as exc:  # noqa: BLE001 - report any failure as a cell value
        return {"ok": False, "duplicate": None, "reason": f"{type(exc).__name__}: {exc}"[:200]}


def main() -> None:
    cases = load_cases(LIMIT)
    print(f"测试样本: {len(cases)} 组真实标题对\n", flush=True)

    jobs = [(i, m) for i in range(len(cases)) for m in MODELS]
    results: dict[tuple[int, str], dict] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(run_one, m, cases[i]): (i, m) for i, m in jobs}
        done = 0
        for fut in futs:
            pass
        for fut, key in futs.items():
            results[key] = fut.result()
            done += 1
            if done % 20 == 0:
                print(f"  ...{done}/{len(jobs)}", flush=True)

    agree = disagree = errs = 0
    rows_out = []
    for i, case in enumerate(cases):
        a = results[(i, MODELS[0])]
        b = results[(i, MODELS[1])]
        if not a["ok"] or not b["ok"]:
            errs += 1
            status = "ERROR"
        elif a["duplicate"] == b["duplicate"]:
            agree += 1
            status = "一致"
        else:
            disagree += 1
            status = "不一致"
        rows_out.append({**case, "pro": a, "flash": b, "status": status})

    print("\n==== 结论一致性 ====")
    valid = agree + disagree
    if valid:
        print(f"一致 {agree}/{valid} = {agree / valid:.1%}   不一致 {disagree}   调用失败 {errs}")

    def acc(side: str) -> str:
        n = ok = 0
        for r in rows_out:
            ref = r["hist_body_dup"]
            if r[side]["ok"] and isinstance(ref, bool):
                n += 1
                ok += int(r[side]["duplicate"] == ref)
        return f"{ok}/{n} = {ok / n:.1%}" if n else "n/a"

    print(f"\n与历史正文确认结论吻合度（参考标签）:")
    print(f"  deepseek-v4-pro : {acc('pro')}")
    print(f"  glm-5.3-flash   : {acc('flash')}")

    print("\n==== 不一致/失败明细 ====")
    for r in rows_out:
        if r["status"] == "一致":
            continue
        print(f"\n[{r['status']}] #{r['article_id']} vs #{r['matched_id']}  历史正文确认={r['hist_body_dup']}")
        print(f"  候选: {r['candidate_title']}")
        print(f"  已发: {r['matched_title']}")
        print(f"  pro  -> dup={r['pro']['duplicate']} | {r['pro']['reason'][:120]}")
        print(f"  flash-> dup={r['flash']['duplicate']} | {r['flash']['reason'][:120]}")

    with open("/tmp/cmp_dedup_result.json", "w", encoding="utf-8") as fh:
        json.dump(rows_out, fh, ensure_ascii=False, indent=1)
    print("\n明细已写入 /tmp/cmp_dedup_result.json")


if __name__ == "__main__":
    main()
