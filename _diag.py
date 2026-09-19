import json
import sqlite3

c = sqlite3.connect("instance/automatic_post.sqlite3")
c.row_factory = sqlite3.Row

for aid in (20412, 20427):
    r = c.execute(
        "SELECT id, source, status, quality_json FROM articles WHERE id=?", (aid,)
    ).fetchone()
    q = json.loads(r["quality_json"] or "{}")
    pr_ = q.get("promotion_repair") or {}
    print("=" * 70)
    print(f"#{aid}  source={r['source']}  status={r['status']}")
    print("=" * 70)
    print("decision        :", q.get("decision"), "|", q.get("decision_reason"))
    print("issues          :", json.dumps(q.get("issues"), ensure_ascii=False))
    sc = q.get("semantic_check") or {}
    print("semantic has_ad :", sc.get("has_ad_or_dirty"), "repairable=", sc.get("repairable"))
    print("  reason        :", sc.get("reason"))
    print()
    print("--- promotion_repair 关键字段 ---")
    for k in (
        "attempted", "applied", "outcome", "plan_error", "error",
        "safety_check_passed", "candidate_created", "candidate_committed",
        "removed_count", "cumulative_removed_visible_chars",
        "cumulative_limit_exceeded",
    ):
        if k in pr_:
            print(f"  {k} = {pr_[k]!r}")
    print("  keys:", list(pr_.keys()))
    print()
    print("--- 首轮计划 ---")
    for it in (pr_.get("repair_plans") or q.get("repair_plans") or []):
        print("  ", json.dumps(it, ensure_ascii=False)[:400])
    print()
    print("--- matches（逐项校验结果）---")
    for m in (pr_.get("matches") or [])[:6]:
        print("  ", json.dumps(m, ensure_ascii=False)[:400])
    print()
    sq = pr_.get("second_quality") or {}
    if sq:
        print("--- 二次质检结果 ---")
        print("   pass=", sq.get("pass"), "needs_review=", sq.get("needs_review"),
              "decision=", sq.get("decision"))
        print("   issues:", json.dumps(sq.get("issues"), ensure_ascii=False)[:500])
        print("   repair_plans:", json.dumps(sq.get("repair_plans"), ensure_ascii=False)[:300])
        print("   repair_plan_error:", sq.get("repair_plan_error"))
    fr = pr_.get("followup_repair") or {}
    if fr:
        print("--- 后续轮修复 ---")
        print("   ", json.dumps({k: v for k, v in fr.items() if k not in ("before", "after")}, ensure_ascii=False)[:700])
    print()
    print("--- 事件流水 ---")
    for e in c.execute(
        "SELECT event_type, from_status, to_status, message, created_at "
        "FROM article_events WHERE article_id=? ORDER BY created_at", (aid,)
    ):
        print(f"  {e['created_at'][11:23]} {e['event_type']:<24} "
              f"{e['from_status']}->{e['to_status']}  {(e['message'] or '')[:70]}")
    print()
