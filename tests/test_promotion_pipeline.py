from __future__ import annotations

from copy import deepcopy
import json

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once
from app.services.promotion_repair import (
    MAX_AI_REPAIR_REMOVED_CHARS,
    body_safety_stats,
)


TITLE = "澳超新赛季赛程公布及揭幕战安排确认"
ARTICLE_TEXT = "澳超官方公布了新赛季安排，揭幕战将在十月进行，各支球队正在按计划完成季前备战。"
PROMOTION = "点击查看2026/27赛季五十铃UTE澳超完整赛程"
PROGRAM_PROMOTION = "●矢部浩之先生的新节目《J.LEAGUE WEEKEND 周日的矢部萨卡》开播！ ｜ J联赛"
BRANDED_WATCH_PROMOTION = (
    "请在 ge、Globo 和 SporTV 上观看关于瓦斯科达伽马的全部内容："
)
IMAGE = '<img src="/fastdfs8/promotion-test.jpg" alt="澳超赛场">'


def _item(suffix: str, *, body: str | None = None) -> dict:
    return {
        "translate_title": TITLE,
        "archive_id": 0,
        "translate_body": body or f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>",
        "source": "marca",
        "source_url": f"https://example.com/promotion/{suffix}",
        "dqd_litpic": "/fastdfs8/promotion-test.jpg",
        "channels": [100],
    }


def _quality(*, needs_review: bool, dirty: list[str] | None = None, reason: str) -> dict:
    return {
        "pass": not needs_review,
        "needs_review": needs_review,
        "score": 50 if needs_review else 100,
        "level": "B",
        "issues": {
            "title_problems": [],
            "dirty_content": list(dirty or []),
            "completeness_problems": [],
            "channel_problems": [],
            "semantic_problems": [],
        },
        "reason": reason,
        "title_before": TITLE,
        "title_after": TITLE,
        "title_fix_method": "unchanged",
    }


FIRST_DIRTY = _quality(
    needs_review=True,
    dirty=["疑似广告或脏内容：完整赛程"],
    reason="命中独立推广段落",
)
SECOND_PASS = _quality(needs_review=False, reason="内容正常")
SECOND_FAIL = _quality(
    needs_review=True,
    dirty=["仍存在其他脏内容"],
    reason="二次质检仍未通过",
)


def _config(database: str) -> AppConfig:
    return AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        llm_api_key="",
        publisher_enabled=False,
        scheduler_enabled=False,
    )


def _llm_config(database: str) -> AppConfig:
    """Config with a configured LLM for transient-retry scheduling tests."""
    return AppConfig(
        database_path=database,
        material_api_key="test-key",
        material_caller="test-caller",
        llm_api_key="test-llm-key",
        publisher_enabled=False,
        scheduler_enabled=False,
    )


def _enable_source(database: str) -> None:
    conn = _connect(database)
    try:
        tab = repo.list_tabs(conn)[0]
        repo.update_source("marca", conn, tab_id=tab["id"], enabled=True)
    finally:
        conn.close()


def _fetch(monkeypatch, items: list[dict]) -> None:
    monkeypatch.setattr(
        "app.services.pipeline.MaterialClient.fetch_all",
        lambda self, sources, **kwargs: MaterialFetchResult(
            items=items, total=len(items), pages=1
        ),
    )


def _event_map(article_id: int, connection) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for event in repo.list_article_events(article_id, connection):
        result.setdefault(event["event_type"], []).append(event)
    return result


def test_pipeline_repairs_once_then_second_quality_passes(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    source = _item("pass")
    _fetch(monkeypatch, [source])
    calls: list[dict] = []
    answers = iter([deepcopy(FIRST_DIRTY), deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert PROMOTION in calls[0]["body"]
    assert PROMOTION not in calls[1]["body"]
    assert calls[0]["title"] == calls[1]["title"] == TITLE
    assert IMAGE in calls[0]["body"] and IMAGE in calls[1]["body"]

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["title_final"] == TITLE
        assert article["body_html"] == f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
        repair = article["quality"]["promotion_repair"]
        assert repair["attempted"] is True
        assert repair["applied"] is True
        assert repair["outcome"] == "passed"

        events = _event_map(article["id"], conn)
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1, 2]
        triggered = events["AUTO_REPAIR_TRIGGERED"][0]["payload"]
        assert triggered["match_count"] == 1
        assert triggered["matched_texts"] == [PROMOTION]
        assert triggered["first_quality"]["needs_review"] is True
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["match_count"] == 1
        assert applied["image_count_before"] == applied["image_count_after"] == 1
        assert applied["image_sources_unchanged"] is True
        assert applied["title_unchanged"] is True
        finished = events["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["outcome"] == "passed"
        assert finished["final_status"] == "READY_TO_PUBLISH"
        assert finished["destination"] == "发布队列"
        assert finished["second_quality"]["needs_review"] is False
    finally:
        conn.close()


def test_first_quality_pass_bypasses_repair_and_preserves_body(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("first-pass-unchanged", body=body)])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return deepcopy(SECOND_PASS)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    def unexpected_repair_call(*args, **kwargs):
        raise AssertionError("首轮通过文章不应进入任何修复调用")

    monkeypatch.setattr("app.services.pipeline.plan_local_repair", unexpected_repair_call)
    monkeypatch.setattr("app.services.pipeline.apply_repair_plan", unexpected_repair_call)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 1
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        events = _event_map(article["id"], conn)
        assert article["body_html"] == calls[0]["body"] == body
        assert article["title_final"] == TITLE
        assert "AUTO_REPAIR_TRIGGERED" not in events
        assert "AUTO_REPAIR_APPLIED" not in events
        assert "AUTO_REPAIR_FINISHED" not in events
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1]
    finally:
        conn.close()


def test_real_quality_contradiction_becomes_candidate_then_passes_full_recheck(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("decision-repairable", body=body)])

    class SequencedLLM:
        configured = True

        def __init__(self):
            self.calls = 0

        def chat_json(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return {
                    "title_complete": True,
                    "body_complete": True,
                    "has_ad_or_dirty": False,
                    "repairable": False,
                    "needs_review": False,
                    "reason": "正文完整，但末尾存在可删除的赛程入口",
                    "repair_plans": [{
                        "block_id": "b2",
                        "action": "remove_block",
                        "evidence": PROMOTION,
                        "issue_type": "traffic_generation",
                        "reason": "该赛程入口属于引流，与新闻事实无关",
                        "confidence": 0.99,
                    }],
                }
            return {
                "title_complete": True,
                "body_complete": True,
                "has_ad_or_dirty": False,
                "repairable": False,
                "needs_review": False,
                "reason": "内容正常",
                "repair_plans": [],
            }

    llm = SequencedLLM()
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: llm)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert llm.calls == 2
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert PROMOTION not in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["first_quality"]["decision"] == "repairable"
        assert repair["first_quality"]["repair_plan_warning"]
        assert repair["candidate_committed"] is True
    finally:
        conn.close()


def test_pipeline_repairs_duplicate_block_then_second_quality_passes(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    repeated = "费内巴切已确认，格林伍德和贡多齐正接受欧足联的纪律调查。"
    body = (
        f"<p>{repeated}</p><p>{ARTICLE_TEXT}</p>{IMAGE}<p>{repeated}</p>"
    )
    _fetch(monkeypatch, [_item("duplicate-block", body=body)])
    first = _quality(needs_review=True, reason="正文存在完全重复段落")
    first["issues"]["semantic_problems"] = ["正文存在完全重复段落"]
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b3",
            "keep_block_id": "b1",
            "action": "remove_block",
            "evidence": repeated,
            "issue_type": "duplicate_content",
            "reason": "b3 与 b1 完全重复，保留首次出现内容",
            "confidence": 0.97,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert calls[0]["body"].count(repeated) == 2
    assert calls[1]["body"].count(repeated) == 1
    assert IMAGE in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["body_html"].count(repeated) == 1
        assert article["quality"]["promotion_repair"]["candidate_committed"] is True
    finally:
        conn.close()


def test_pipeline_uses_separate_planner_when_first_failure_has_no_plan(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    extra = "【广告】点击查看新赛季完整赛程与购票入口"
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{extra}</p>"
    _fetch(monkeypatch, [_item("separate-planner", body=body)])
    first = _quality(needs_review=True, reason="正文含与新闻无关的广告入口")
    first["issues"]["semantic_problems"] = ["正文含与新闻无关的广告入口"]
    first["semantic_check"] = {
        "title_complete": True,
        "body_complete": True,
        "has_ad_or_dirty": False,
        "repairable": False,
        "needs_review": True,
    }
    answers = iter([first, deepcopy(SECOND_PASS)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: object())
    monkeypatch.setattr(
        "app.services.pipeline.plan_local_repair",
        lambda **kwargs: {
            "repairable": True,
            "reason": "可删除精确定位的广告入口",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": extra,
                "issue_type": "extraneous_content",
                "reason": "该广告入口与新闻事实无关",
                "confidence": 0.95,
            }],
            "repair_plan_error": None,
        },
    )

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert extra not in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["repair_planner"]["repairable"] is True
        assert repair["candidate_committed"] is True
    finally:
        conn.close()


def test_pipeline_replans_invalid_locator_once_then_runs_full_second_quality(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("locator-replan-pass", body=body)])
    first = _quality(needs_review=True, reason="正文末尾存在赛程引流")
    first["decision"] = "repairable"
    first["issues"]["semantic_problems"] = ["正文末尾存在赛程引流"]
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b2",
            "action": "remove_block",
            "evidence": PROMOTION + "。",
            "issue_type": "traffic_generation",
            "reason": "该赛程入口属于引流，与新闻事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    quality_calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        quality_calls.append(kwargs)
        return next(answers)

    replanner_calls: list[dict] = []

    def fake_replanner(**kwargs):
        replanner_calls.append(kwargs)
        return {
            "repairable": True,
            "reason": "已按真实正文块重新定位赛程入口",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": PROMOTION,
                "issue_type": "traffic_generation",
                "reason": "该赛程入口属于引流，与新闻事实无关",
                "confidence": 0.99,
            }],
            "repair_plan_error": None,
        }

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: object())
    monkeypatch.setattr("app.services.pipeline.plan_local_repair", fake_replanner)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(replanner_calls) == 1
    assert "证据与目标正文块不一致" in replanner_calls[0]["validation_error"]
    assert replanner_calls[0]["rejected_plans"][0]["evidence"].endswith("。")
    assert len(quality_calls) == 2
    assert PROMOTION not in quality_calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert PROMOTION not in article["body_html"]
        replanner = article["quality"]["promotion_repair"]["repair_replanner"]
        assert replanner["attempted"] is True
        assert replanner["validation_error"] is None
        assert article["quality"]["promotion_repair"]["candidate_committed"] is True
    finally:
        conn.close()


def test_pipeline_replan_exception_keeps_original_body_in_review(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("locator-replan-error", body=body)])
    first = _quality(needs_review=True, reason="正文末尾存在赛程引流")
    first["decision"] = "repairable"
    first["issues"]["semantic_problems"] = ["正文末尾存在赛程引流"]
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b99",
            "action": "remove_block",
            "evidence": PROMOTION,
            "issue_type": "traffic_generation",
            "reason": "该赛程入口属于引流，与新闻事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    quality_calls: list[dict] = []

    def fake_evaluate(**kwargs):
        quality_calls.append(kwargs)
        return first

    replanner_calls: list[dict] = []

    def failing_replanner(**kwargs):
        replanner_calls.append(kwargs)
        raise RuntimeError("replanner unavailable")

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: object())
    monkeypatch.setattr("app.services.pipeline.plan_local_repair", failing_replanner)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    assert len(replanner_calls) == 1
    assert len(quality_calls) == 1
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == body
        repair = article["quality"]["promotion_repair"]
        assert repair["applied"] is False
        assert repair["repair_replanner"]["attempted"] is True
        assert repair["repair_replanner"]["repair_plan_error"] == "replanner unavailable"
    finally:
        conn.close()


def _skipped_item_first_quality() -> dict:
    """首轮质检：一条可验证的引流删除 + 一条无法验证的正文改写。"""

    first = _quality(needs_review=True, reason="正文末尾存在赛程引流")
    first["decision"] = "repairable"
    first["issues"]["semantic_problems"] = ["正文末尾存在赛程引流"]
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [
            {
                "block_id": "b2",
                "action": "remove_block",
                "evidence": PROMOTION,
                "issue_type": "traffic_generation",
                "reason": "该赛程入口属于引流，与新闻事实无关",
                "confidence": 0.99,
            },
            {
                "block_id": "b1",
                "action": "replace_text",
                "evidence": ARTICLE_TEXT,
                "after": "澳超官方公布了新赛季安排。",
                "issue_type": "extraneous_content",
                "reason": "顺带精简正文表述",
                "confidence": 0.98,
            },
        ],
        "repair_plan_error": None,
    })
    return first


def test_pipeline_replans_skipped_plan_items_once(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("skipped-item-replan", body=body)])
    answers = iter([_skipped_item_first_quality(), deepcopy(SECOND_PASS)])
    quality_calls: list[dict] = []

    def fake_evaluate(**kwargs):
        quality_calls.append(kwargs)
        return next(answers)

    replanner_calls: list[dict] = []

    def fake_replanner(**kwargs):
        replanner_calls.append(kwargs)
        return {
            "repairable": True,
            "reason": "已剔除无法验证的正文改写，仅保留引流删除",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": PROMOTION,
                "issue_type": "traffic_generation",
                "reason": "该赛程入口属于引流，与新闻事实无关",
                "confidence": 0.99,
            }],
            "repair_plan_error": None,
        }

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: object())
    monkeypatch.setattr("app.services.pipeline.plan_local_repair", fake_replanner)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(replanner_calls) == 1
    assert "不是可验证的轻微局部修复" in replanner_calls[0]["validation_error"]
    assert len(quality_calls) == 2
    assert PROMOTION not in quality_calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert PROMOTION not in article["body_html"]
        assert ARTICLE_TEXT in article["body_html"]
        replanner = article["quality"]["promotion_repair"]["repair_replanner"]
        assert replanner["trigger"] == "skipped_plan_items"
        assert replanner["validation_error"] is None
    finally:
        conn.close()


def test_pipeline_keeps_verified_subset_when_skip_replan_fails(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("skipped-item-replan-error", body=body)])
    answers = iter([_skipped_item_first_quality(), deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        return next(answers)

    replanner_calls: list[dict] = []

    def failing_replanner(**kwargs):
        replanner_calls.append(kwargs)
        raise RuntimeError("replanner unavailable")

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: object())
    monkeypatch.setattr("app.services.pipeline.plan_local_repair", failing_replanner)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(replanner_calls) == 1
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        # 重规划不可用时保留已验证的那条删除，正文其余部分逐字不变。
        assert PROMOTION not in article["body_html"]
        assert ARTICLE_TEXT in article["body_html"]
        replanner = article["quality"]["promotion_repair"]["repair_replanner"]
        assert replanner["trigger"] == "skipped_plan_items"
        assert replanner["repair_plan_error"] == "replanner unavailable"
    finally:
        conn.close()


def test_pipeline_does_not_replan_a_confidence_safety_rejection(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("no-replan-for-confidence", body=body)])
    first = _quality(needs_review=True, reason="正文末尾存在赛程引流")
    first["decision"] = "repairable"
    first["issues"]["semantic_problems"] = ["正文末尾存在赛程引流"]
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b2",
            "action": "remove_block",
            "evidence": PROMOTION,
            "issue_type": "traffic_generation",
            "reason": "该赛程入口属于引流，与新闻事实无关",
            "confidence": 0.8,
        }],
        "repair_plan_error": None,
    })

    def unexpected_replanner(**kwargs):
        raise AssertionError("安全策略拒绝不能交给 AI 重规划绕过")

    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: first)
    monkeypatch.setattr("app.services.pipeline._make_llm", lambda config: object())
    monkeypatch.setattr("app.services.pipeline.plan_local_repair", unexpected_replanner)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["body_html"] == body
        assert "置信度不足" in article["quality"]["promotion_repair"]["plan_error"]
        assert "repair_replanner" not in article["quality"]["promotion_repair"]
    finally:
        conn.close()


def test_pipeline_second_quality_failure_goes_to_manual_review(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("second-fail")])
    answers = iter([deepcopy(FIRST_DIRTY), deepcopy(SECOND_FAIL)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert PROMOTION in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "failed"
        assert repair["candidate_created"] is True
        assert repair["candidate_committed"] is False
        finished = _event_map(article["id"], conn)["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["outcome"] == "failed"
        assert finished["final_status"] == "NEEDS_REVIEW"
        assert finished["destination"] == "人工审核"
        assert finished["second_quality"]["needs_review"] is True
    finally:
        conn.close()


def test_pipeline_photo_credit_is_stripped_before_quality_check(
    app, monkeypatch
) -> None:
    # 图片版权标记统一在质检前确定性删除：AI 看不到它，也就不会再因为
    # "该删还是该规范化"产生矛盾计划而转人工。图注与图片节点逐字保留。
    database = app.config["DATABASE"]
    _enable_source(database)
    second_image = '<img src="https://img.example/second.jpg" alt="次图">'
    caption = "球员在终场哨响后向主场球迷致意（比赛第88分钟）"
    body = (
        f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
        f"<p>{caption} [照片]＝Getty Images</p>{second_image}"
    )
    _fetch(monkeypatch, [_item("photo-credit-pass", body=body)])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return deepcopy(SECOND_PASS)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    run_once(_config(database))

    expected_body = (
        f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{caption}</p>{second_image}"
    )
    assert len(calls) == 1
    assert calls[0]["body"] == expected_body
    assert "Getty Images" not in calls[0]["body"]
    assert calls[0]["body"].count("<img") == 2
    assert calls[0]["body"].index('/fastdfs8/promotion-test.jpg') < calls[0]["body"].index(
        "https://img.example/second.jpg"
    )

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["body_html"] == expected_body
        preprocess = _event_map(article["id"], conn)["LINKS_REMOVED"][0]["payload"]
        assert preprocess["applied"] is True
        assert [item["rule"] for item in preprocess["media_artifact_lines"]] == [
            "photo_credit_tail",
        ]
    finally:
        conn.close()


def test_pipeline_photo_credit_strip_still_reviews_other_dirt(
    app, monkeypatch
) -> None:
    # 版权标记在质检前已删除，剩余问题仍未通过质检时照常转人工，
    # 且预处理结果不会因此回滚。
    database = app.config["DATABASE"]
    _enable_source(database)
    caption = "双方队长在赛前交换队旗"
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{caption} [照片]=Getty Images</p>"
    _fetch(monkeypatch, [_item("photo-credit-fail", body=body)])
    monkeypatch.setattr(
        "app.services.pipeline.evaluate", lambda **kwargs: deepcopy(SECOND_FAIL)
    )

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert "[照片]" not in article["body_html"]
        assert "Getty Images" not in article["body_html"]
        assert caption in article["body_html"]
    finally:
        conn.close()


def test_promotion_repair_blocks_title_change_from_second_quality(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("title-blocked")])
    changed_title_quality = {
        **deepcopy(SECOND_PASS),
        "title_after": "被二次质检改写的标题",
        "title_fix_method": "llm",
    }
    answers = iter([deepcopy(FIRST_DIRTY), changed_title_quality])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["title_final"] == TITLE
        assert article["quality"]["title_after"] == TITLE
        assert article["quality"]["title_fix_method"] == "blocked_during_promotion_repair"
        assert article["quality"]["promotion_repair"]["outcome"] == "failed"
        finished = _event_map(article["id"], conn)["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["final_status"] == "NEEDS_REVIEW"
        assert finished["destination"] == "人工审核"
    finally:
        conn.close()


def test_pipeline_second_quality_exception_fails_closed(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("second-error")])
    call_count = 0

    def fake_evaluate(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return deepcopy(FIRST_DIRTY)
        raise RuntimeError("second quality unavailable")

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["errors"] == 0
    assert call_count == 2
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert PROMOTION in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["attempted"] is True
        assert repair["outcome"] == "error"
        assert repair["error"] == "second quality unavailable"
        events = _event_map(article["id"], conn)
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1, 2]
        assert events["QUALITY_RESULT"][1]["payload"]["second_quality_error"] == (
            "second quality unavailable"
        )
        finished = events["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["outcome"] == "error"
        assert finished["final_status"] == "NEEDS_REVIEW"
        assert finished["error"] == "second quality unavailable"
    finally:
        conn.close()


def test_pipeline_second_quality_non_dict_fails_closed(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("second-invalid-shape")])
    answers = iter([deepcopy(FIRST_DIRTY), []])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    result = run_once(_config(database))

    assert result["errors"] == 0
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["quality"]["promotion_repair"]["outcome"] == "error"
        assert article["quality"]["promotion_repair"]["error"] == "二次质检返回结果格式错误"
    finally:
        conn.close()


def test_pipeline_does_not_apply_repair_when_it_would_empty_article(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    original_body = f"<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("empty-guard", body=original_body)])
    call_count = 0

    def fake_evaluate(**kwargs):
        nonlocal call_count
        call_count += 1
        return deepcopy(FIRST_DIRTY)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    run_once(_config(database))

    assert call_count == 1
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == original_body
        repair = article["quality"]["promotion_repair"]
        assert repair["attempted"] is True
        assert repair["applied"] is False
        assert repair["outcome"] == "failed"
        events = _event_map(article["id"], conn)
        assert "AUTO_REPAIR_APPLIED" not in events
        assert events["AUTO_REPAIR_FINISHED"][0]["payload"]["destination"].startswith("人工审核")
    finally:
        conn.close()


def test_pipeline_never_attempts_same_article_twice(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    source = _item("once-only")
    _fetch(monkeypatch, [source])
    answers = iter([
        deepcopy(FIRST_DIRTY),
        deepcopy(SECOND_FAIL),
        deepcopy(FIRST_DIRTY),
    ])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        repo.transition_status(article["id"], "ERROR", conn, message="simulate retry")
    finally:
        conn.close()

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        events = _event_map(article["id"], conn)
        assert len(events["AUTO_REPAIR_TRIGGERED"]) == 1
        assert "AUTO_REPAIR_APPLIED" not in events
        assert len(events["AUTO_REPAIR_FINISHED"]) == 1
        assert article["quality"]["promotion_repair"]["attempted"] is True
        assert article["status"] == "NEEDS_REVIEW"
    finally:
        conn.close()


def test_interrupted_or_previous_repair_cannot_become_ready_on_later_ai_pass(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    source = _item("recovery-fail-closed")
    _fetch(monkeypatch, [source])
    answers = iter([
        deepcopy(FIRST_DIRTY),
        deepcopy(SECOND_FAIL),
        deepcopy(SECOND_PASS),
    ])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["quality"]["promotion_repair"]["attempted"] is True
        repo.transition_status(article["id"], "ERROR", conn, message="simulate restart")
    finally:
        conn.close()

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["quality"]["pass"] is False
        assert article["quality"]["needs_review"] is True
        assert "已阻止重复处理" in article["quality"]["reason"]
        events = _event_map(article["id"], conn)
        assert len(events["AUTO_REPAIR_TRIGGERED"]) == 1
        assert "AUTO_REPAIR_APPLIED" not in events
        assert len(events["AUTO_REPAIR_FINISHED"]) == 1
    finally:
        conn.close()


def test_pipeline_promotion_repair_never_applies_suggested_title(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("title-guard")])
    suggested_title = "模型建议替换的标题"
    second_with_title = {
        **deepcopy(SECOND_PASS),
        "title_after": suggested_title,
        "title_fix_method": "llm",
    }
    answers = iter([deepcopy(FIRST_DIRTY), second_with_title])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["title_final"] == TITLE
        assert article["status"] == "NEEDS_REVIEW"
        assert article["quality"]["title_after"] == TITLE
        assert article["quality"]["title_fix_method"] == "blocked_during_promotion_repair"
        assert article["quality"]["promotion_repair"]["outcome"] == "failed"
        events = _event_map(article["id"], conn)
        assert "TITLE_FIXED" not in events
        assert "AUTO_REPAIR_APPLIED" not in events
        assert article["quality"]["promotion_repair"]["candidate_committed"] is False
    finally:
        conn.close()


def test_pipeline_unmatched_body_stays_exactly_unchanged(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = (
        " \n<!-- 点击查看完整赛程 -->"
        f'<ARTICLE><p class="lead">{ARTICLE_TEXT}</p>{IMAGE}</ARTICLE>\n '
    )
    _fetch(monkeypatch, [_item("unmatched", body=body)])
    monkeypatch.setattr(
        "app.services.pipeline.evaluate", lambda **kwargs: deepcopy(FIRST_DIRTY)
    )

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        # Material normalization trims only the outer feed whitespace; the
        # unmatched HTML itself must not be rewritten by promotion repair.
        assert article["body_html"] == body.strip()
        assert article["status"] == "NEEDS_REVIEW"
        events = _event_map(article["id"], conn)
        assert "AUTO_REPAIR_TRIGGERED" not in events
        assert "AUTO_REPAIR_APPLIED" not in events
    finally:
        conn.close()


def test_pipeline_applies_validated_ai_plan_for_two_tail_promotions(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    whatsapp = "点击这里关注 WhatsApp 频道，获取最新消息"
    watch = "观看 ge、Globo 和 sportv 上的全部内容"
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{whatsapp}</p><p>{watch}</p>"
    _fetch(monkeypatch, [_item("ai-plan", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        # The AI plans reference the pre-preprocessing block numbering. The
        # deterministic quality preprocessing removes the WhatsApp CTA line
        # first, which shifts the watch block from b3 to b2 before the plan
        # is applied; the evidence-based relocation is expected to recover
        # the watch plan while the already-cleaned WhatsApp plan is skipped.
        "repair_plans": [
            {
                "block_id": "b2",
                "action": "remove_block",
                "evidence": whatsapp,
                "confidence": 0.98,
            },
            {
                "block_id": "b3",
                "action": "remove_block",
                "evidence": watch,
                "confidence": 0.96,
            },
        ],
        "repair_plan_error": None,
    })
    answers = iter([first, deepcopy(SECOND_PASS)])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    run_once(_config(database))

    assert len(calls) == 2
    # The WhatsApp CTA is removed by quality preprocessing before the first
    # quality pass, so only the watch line reaches the AI quality check.
    assert whatsapp not in calls[0]["body"] and watch in calls[0]["body"]
    assert whatsapp not in calls[1]["body"] and watch not in calls[1]["body"]
    assert IMAGE in calls[0]["body"] and IMAGE in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["body_html"] == f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
        repair = article["quality"]["promotion_repair"]
        assert repair["removed_count"] == 1
        assert repair["outcome"] == "passed"
    finally:
        conn.close()


def test_pipeline_repairs_real_branded_watch_plan_then_runs_full_second_quality(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    context = (
        "报道完整介绍了比赛进程、球队表现、教练赛后评价和接下来的赛事安排，"
        "并引用了俱乐部相关人员对于球队现状的说明。"
    )
    body = (
        f"<p>{context}</p>{IMAGE}<p>{context}</p>"
        f"<p>{BRANDED_WATCH_PROMOTION}</p>"
    )
    _fetch(monkeypatch, [_item("real-branded-watch", body=body)])
    first = deepcopy(SECOND_PASS)
    first.update({
        "pass": False,
        "needs_review": True,
        "score": 50,
        "issues": {
            **deepcopy(SECOND_PASS["issues"]),
            "dirty_content": ["AI 判断可能含广告或脏内容：正文末尾包含媒体引流"],
        },
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b3",
            "action": "remove_block",
            "evidence": BRANDED_WATCH_PROMOTION,
            "issue_type": "media_promotion",
            "reason": (
                "该段以‘观看全部内容’为号召，推广在指定媒体平台观看内容，"
                "与新闻事实无关。"
            ),
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert BRANDED_WATCH_PROMOTION in calls[0]["body"]
    assert BRANDED_WATCH_PROMOTION not in calls[1]["body"]
    assert calls[1]["title"] == TITLE
    assert IMAGE in calls[1]["body"]
    assert context in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert BRANDED_WATCH_PROMOTION not in article["body_html"]
        assert IMAGE in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "passed"
        assert repair["removed_count"] == 1
        events = _event_map(article["id"], conn)
        assert [
            event["payload"]["quality_round"]
            for event in events["QUALITY_RESULT"]
        ] == [1, 2]
        assert events["AUTO_REPAIR_APPLIED"][0]["payload"][
            "image_attributes_unchanged"
        ] is True
        assert events["AUTO_REPAIR_FINISHED"][0]["payload"]["destination"] == "发布队列"
    finally:
        conn.close()


def test_pipeline_does_not_apply_plan_from_inconsistent_first_pass(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("ai-plan-needs-review-false", body=body)])
    first = deepcopy(SECOND_PASS)
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b2",
            "action": "remove_block",
            "evidence": PROMOTION,
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    assert len(calls) == 1
    assert PROMOTION in calls[0]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert PROMOTION in article["body_html"]
        assert article["quality"]["promotion_repair"]["outcome"] == "failed"
    finally:
        conn.close()


def test_pipeline_treats_photo_credit_advisory_as_non_blocking_second_pass(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = (
        "<p>前锋德田誉独中两元（Hiroyuki SATO），球队随后赢得比赛。</p>"
        f"{IMAGE}<p>{PROMOTION}</p>"
    )
    _fetch(monkeypatch, [_item("photo-advisory-second-pass", body=body)])
    photo_plan = {
        "block_id": "b1",
        "action": "replace_text",
        "evidence": "前锋德田誉独中两元（Hiroyuki SATO）",
        "after": "前锋德田誉独中两元（摄影：Hiroyuki SATO）",
        "confidence": 0.9,
    }
    second = deepcopy(SECOND_PASS)
    second.update({
        "semantic_check": {
            "has_ad_or_dirty": False,
            "needs_review": False,
        },
        "repair_plans": [photo_plan],
        "repair_plan_error": None,
    })
    answers = iter([deepcopy(FIRST_DIRTY), second])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        quality = article["quality"]
        assert quality["repair_plans"] == []
        assert quality["repair_plan_error"] is None
        assert quality["advisory_repair_plans"] == [photo_plan]
        assert quality["promotion_repair"]["outcome"] == "passed"
    finally:
        conn.close()


def test_pipeline_keeps_contradictory_remove_plan_blocked_on_second_pass(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("remove-plan-contradiction-second-pass")])
    second = deepcopy(SECOND_PASS)
    second.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b1",
            "action": "remove_block",
            "evidence": ARTICLE_TEXT,
            "confidence": 0.99,
        }],
        "repair_plan_error": "AI 修复计划与质检结论矛盾",
    })
    answers = iter([deepcopy(FIRST_DIRTY), second])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["quality"]["promotion_repair"]["outcome"] == "failed"
        assert article["quality"]["repair_plans"]
    finally:
        conn.close()


def test_pipeline_runs_three_repair_rounds_until_quality_passes(
    app, monkeypatch
) -> None:
    """模型每轮只报一部分脏内容时，应连续修到干净而不是转人工。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    tail = f"{ARTICLE_TEXT}Getty"
    orphan = "里尔前锋上田绮世"
    body = f"<p>{tail}</p><p>{orphan}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("three-round-repair", body=body)])

    def _dirty_with_plan(plans: list[dict]) -> dict:
        quality = deepcopy(FIRST_DIRTY)
        quality.update({
            "decision": "repairable",
            "semantic_check": {
                "title_complete": True,
                "body_complete": True,
                "has_ad_or_dirty": True,
                "repairable": True,
                "needs_review": False,
            },
            "repair_plans": plans,
            "repair_plan_error": None,
        })
        return quality

    # 第 1 轮删推广段，第 2 轮删孤立片段，第 3 轮才报出段尾的图库署名。
    first = _dirty_with_plan([{
        "block_id": "b3",
        "action": "remove_block",
        "evidence": PROMOTION,
        "issue_type": "promotion",
        "reason": "独立推广段落，与新闻事实无关",
        "confidence": 0.99,
    }])
    second = _dirty_with_plan([{
        "block_id": "b2",
        "action": "remove_block",
        "evidence": orphan,
        "issue_type": "template_artifact",
        "reason": "不完整的孤立片段，与正文事实无关",
        "confidence": 0.99,
    }])
    third = _dirty_with_plan([{
        "block_id": "b1",
        "action": "replace_text",
        "evidence": tail,
        "after": ARTICLE_TEXT,
        "issue_type": "extraneous_content",
        "reason": "段尾 Getty 是图库版权标记，删除后剩余正文语义完整",
        "confidence": 0.99,
    }])
    answers = iter([first, second, third, deepcopy(SECOND_PASS)])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 4
    assert PROMOTION not in calls[1]["body"]
    assert orphan not in calls[2]["body"]
    assert "Getty" not in calls[3]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert "Getty" not in article["body_html"]
        assert IMAGE in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "passed"
        assert repair["removed_count"] == 3
        assert repair["followup_repair"]["rounds"] == 2
        assert repair["followup_repair"]["applied"] is True
        events = _event_map(article["id"], conn)
        assert [
            event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]
        ] == [1, 2, 3, 4]
    finally:
        conn.close()


def test_pipeline_stops_after_the_repair_round_cap(app, monkeypatch) -> None:
    """轮数用完仍不干净时必须转人工，不能无限调用模型。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    body = (
        f"<p>{ARTICLE_TEXT}</p><p>推广段甲</p><p>推广段乙</p>"
        f"<p>推广段丙</p><p>推广段丁</p>{IMAGE}"
    )
    _fetch(monkeypatch, [_item("repair-round-cap", body=body)])

    def _dirty_with_plan(block_id: str, evidence: str) -> dict:
        quality = deepcopy(FIRST_DIRTY)
        quality.update({
            "decision": "repairable",
            "semantic_check": {
                "title_complete": True,
                "body_complete": True,
                "has_ad_or_dirty": True,
                "repairable": True,
                "needs_review": False,
            },
            "repair_plans": [{
                "block_id": block_id,
                "action": "remove_block",
                "evidence": evidence,
                "issue_type": "promotion",
                "reason": "独立推广段落，与新闻事实无关",
                "confidence": 0.99,
            }],
            "repair_plan_error": None,
        })
        return quality

    answers = iter([
        _dirty_with_plan("b2", "推广段甲"),
        _dirty_with_plan("b2", "推广段乙"),
        _dirty_with_plan("b2", "推广段丙"),
        _dirty_with_plan("b2", "推广段丁"),
    ])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    # 3 轮修复 + 每轮一次质检 = 4 次调用，之后不再追加。
    assert len(calls) == 4
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        # 未通过的候选正文不提交，正文保持原样。
        assert "推广段甲" in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "failed"
        assert repair["applied"] is False
        assert repair["followup_repair"]["rounds"] == 2
    finally:
        conn.close()


def test_pipeline_allows_one_safe_followup_repair_after_second_pass(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    orphan = "里尔前锋上田绮世"
    body = f"<p>{ARTICLE_TEXT}</p><p>{orphan}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("followup-repair", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b3",
            "action": "remove_block",
            "evidence": PROMOTION,
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    second = deepcopy(SECOND_PASS)
    second.update({
        "pass": False,
        "needs_review": True,
        "issues": {
            "title_problems": [],
            "dirty_content": [],
            "completeness_problems": [],
            "channel_problems": [],
            "semantic_problems": ["AI 修复计划与质检结论矛盾，需要人工确认"],
        },
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": False,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b2",
            "action": "remove_block",
            "evidence": orphan,
            "issue_type": "template_artifact",
            "reason": "不完整的孤立片段，与正文事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": "AI 修复计划与质检结论矛盾",
    })
    calls: list[dict] = []
    answers = iter([first, second, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 3
    assert PROMOTION in calls[0]["body"]
    assert PROMOTION not in calls[1]["body"]
    assert orphan in calls[1]["body"]
    assert orphan not in calls[2]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert PROMOTION not in article["body_html"]
        assert orphan not in article["body_html"]
        assert IMAGE in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "passed"
        assert repair["removed_count"] == 2
        assert repair["followup_repair"]["attempted"] is True
        assert repair["followup_repair"]["applied"] is True
        events = _event_map(article["id"], conn)
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1, 2, 3]
        final_stats = body_safety_stats(article["body_html"])
        original_stats = body_safety_stats(body)
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert repair["after"] == json.loads(json.dumps(final_stats))
        assert applied["body_sha256_before"] == original_stats["sha256"]
        assert applied["body_length_before"] == len(body)
        assert applied["body_sha256_after"] == final_stats["sha256"]
        assert applied["body_length_after"] == len(article["body_html"])
        assert applied["image_count_after"] == final_stats["image_count"]
        assert applied["image_sources_unchanged"] is True
        assert applied["image_attributes_unchanged"] is True
    finally:
        conn.close()


def test_pipeline_rejects_followup_when_cumulative_removal_exceeds_limit(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    repeated = "重复推广内容" * 90
    body = (
        f"<p>{ARTICLE_TEXT}</p>"
        f"<p>{repeated}</p><p>{repeated}</p><p>{repeated}</p>"
        f"{IMAGE}"
    )
    _fetch(monkeypatch, [_item("followup-cumulative-limit", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "decision": "repairable",
        "issues": {
            "title_problems": [],
            "dirty_content": [],
            "completeness_problems": [],
            "channel_problems": [],
            "semantic_problems": ["正文存在重复段落"],
        },
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b4",
            "keep_block_id": "b2",
            "action": "delete_duplicate",
            "evidence": repeated,
            "issue_type": "duplicate_content",
            "reason": "该段与保留段落重复，与正文事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    second = deepcopy(FIRST_DIRTY)
    second.update({
        "decision": "repairable",
        "issues": {
            "title_problems": [],
            "dirty_content": [],
            "completeness_problems": [],
            "channel_problems": [],
            "semantic_problems": ["正文仍有一处重复段落"],
        },
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b3",
            "keep_block_id": "b2",
            "action": "delete_duplicate",
            "evidence": repeated,
            "issue_type": "duplicate_content",
            "reason": "该段与保留段落重复，与正文事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, second, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    assert len(calls) == 3
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == body
        repair = article["quality"]["promotion_repair"]
        assert repair["applied"] is False
        assert repair["candidate_committed"] is False
        assert repair["cumulative_removed_visible_chars"] > MAX_AI_REPAIR_REMOVED_CHARS
        assert repair["removed_visible_chars_limit"] == MAX_AI_REPAIR_REMOVED_CHARS
        assert repair["cumulative_limit_exceeded"] is True
        assert repair["followup_repair"]["applied"] is False
        assert "累计删除" in repair["followup_repair"]["error"]
        assert str(MAX_AI_REPAIR_REMOVED_CHARS) in repair["followup_repair"]["error"]
        events = _event_map(article["id"], conn)
        quality_events = events["QUALITY_RESULT"]
        assert [event["payload"]["quality_round"] for event in quality_events] == [1, 2, 3]
        assert quality_events[-1]["payload"]["pass"] is True
        assert quality_events[-1]["payload"]["needs_review"] is False
        assert "AUTO_REPAIR_APPLIED" not in events
        assert "累计删除" in events["AUTO_REPAIR_FINISHED"][0]["payload"]["repair_error"]
    finally:
        conn.close()


def test_pipeline_applies_ai_line_plan_and_runs_second_quality(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = (
        f"<p>{ARTICLE_TEXT}\n{PROGRAM_PROMOTION}\n赛后主教练接受采访并肯定了球队的表现。</p>"
        f"{IMAGE}<p>{ARTICLE_TEXT}</p><p>{ARTICLE_TEXT}</p>"
        f"<p>{ARTICLE_TEXT}</p><p>{ARTICLE_TEXT}</p>"
    )
    _fetch(monkeypatch, [_item("ai-line-plan", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "segment_id": "b1.s2",
            "action": "remove_text_line",
            "evidence": PROGRAM_PROMOTION,
            "issue_type": "program_promotion",
            "reason": "该独立节目推广行与新闻事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    answers = iter([first, deepcopy(SECOND_PASS)])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert PROGRAM_PROMOTION in calls[0]["body"]
    assert PROGRAM_PROMOTION not in calls[1]["body"]
    assert ARTICLE_TEXT in calls[1]["body"]
    assert IMAGE in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert PROGRAM_PROMOTION not in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["removed_count"] == 1
        assert repair["matches"][0]["segment_id"] == "b1.s2"
        assert repair["outcome"] == "passed"
        events = _event_map(article["id"], conn)
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["removed_blocks"][0]["segment_id"] == "b1.s2"
        assert [e["payload"]["quality_round"] for e in events["QUALITY_RESULT"]] == [1, 2]
    finally:
        conn.close()


def test_pipeline_applies_unknown_ai_program_promotion_and_audits_reason(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = (
        f"<p>{ARTICLE_TEXT}\n{PROGRAM_PROMOTION}\n赛后主教练接受了采访并肯定了球队的表现。</p>"
        f"{IMAGE}<p>{ARTICLE_TEXT}</p><p>{ARTICLE_TEXT}</p>"
        f"<p>{ARTICLE_TEXT}球队还将根据教练组的安排继续完成训练，并准备下一场比赛。</p>"
    )
    _fetch(monkeypatch, [_item("ai-unknown-program", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        "repair_plans": [{
            "segment_id": "b1.s2",
            "action": "remove_text_line",
            "evidence": PROGRAM_PROMOTION,
            "issue_type": "standalone_program_promotion",
            "reason": "独立节目推广内容，与新闻事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert PROGRAM_PROMOTION in calls[0]["body"]
    assert PROGRAM_PROMOTION not in calls[1]["body"]
    assert IMAGE in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        applied = _event_map(article["id"], conn)["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["removed_blocks"][0]["issue_type"] == "standalone_program_promotion"
        assert applied["removed_blocks"][0]["reason"] == "独立节目推广内容，与新闻事实无关"
        assert applied["removed_blocks"][0]["confidence"] == 0.99
        assert applied["removed_blocks"][0]["block_id"] == "b1"
        assert applied["removed_blocks"][0]["segment_id"] == "b1.s2"
    finally:
        conn.close()


def test_pipeline_applies_bracketed_artifact_ai_plan_and_preserves_images(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    artifact = "【广告】点击查看新赛季完整赛程与购票入口"
    image_one = (
        '<IMG SRC="/fastdfs8/artifact-main.jpg" ALT="主图" width="640" '
        'loading="lazy" class="hero" data-slot="main">'
    )
    image_two = (
        '<img class="detail" src="https://img.example/artifact-detail.jpg" '
        'alt="细节图" width="320" height="180" data-slot="detail">'
    )
    context = (
        "报道完整介绍了球员转会背景、合同安排、球队计划和后续训练，"
        "并引用了俱乐部及相关人员对下一阶段工作的说明。"
    )
    body = (
        f"<p>{context}</p>{image_one}<p>{artifact}</p>"
        f"{image_two}<p>{context}</p><p>{context}</p>"
    )
    _fetch(monkeypatch, [_item("ai-bracketed-artifact", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b2",
            "action": "remove_block",
            "evidence": artifact,
            "issue_type": "extraneous_content",
            "reason": "该广告入口属于采集模板残留，与球员转会新闻事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)
    before_stats = body_safety_stats(body)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert artifact in calls[0]["body"]
    assert artifact not in calls[1]["body"]
    assert body_safety_stats(calls[1]["body"])["image_count"] == before_stats["image_count"]
    assert body_safety_stats(calls[1]["body"])["image_sources"] == before_stats["image_sources"]
    assert body_safety_stats(calls[1]["body"])["image_attributes"] == before_stats["image_attributes"]

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert artifact not in article["body_html"]
        assert body_safety_stats(article["body_html"])["image_attributes"] == before_stats["image_attributes"]
        events = _event_map(article["id"], conn)
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1, 2]
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["image_sources_unchanged"] is True
        assert applied["image_attributes_unchanged"] is True
        assert applied["removed_blocks"][0]["issue_type"] == "extraneous_content"
        assert applied["removed_blocks"][0]["reason"] == "该广告入口属于采集模板残留，与球员转会新闻事实无关"
        finished = events["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["destination"] == "发布队列"
    finally:
        conn.close()


def test_pipeline_runs_second_quality_for_exact_plan_without_allowed_type(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    retained = "报道还补充了双方主教练的赛后采访、关键球员表现以及下一轮备战安排。"
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{retained}</p>"
    _fetch(monkeypatch, [_item("ai-plan-rejected", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "has_ad_or_dirty": True,
            "repairable": True,
        },
        "repair_plans": [{
            "block_id": "b1",
            "action": "remove_block",
            "evidence": ARTICLE_TEXT,
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []
    answers = iter([first, deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    assert len(calls) == 2
    assert ARTICLE_TEXT in calls[0]["body"]
    assert ARTICLE_TEXT not in calls[1]["body"]

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["body_html"] == f"{IMAGE}<p>{retained}</p>"
        repair = article["quality"]["promotion_repair"]
        assert repair["applied"] is True
        assert repair["outcome"] == "passed"
        assert repair["matches"][0]["validation"] == "ai_exact_target"
    finally:
        conn.close()


def test_pipeline_rejects_plan_without_explicit_repairable_true(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{PROMOTION}</p>"
    _fetch(monkeypatch, [_item("ai-plan-missing-repairable", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "has_ad_or_dirty": True,
            "needs_review": True,
        },
        "repair_plans": [{
            "block_id": "b2",
            "action": "remove_block",
            "evidence": PROMOTION,
            "issue_type": "advertisement",
            "reason": "独立推广内容，与新闻事实无关",
            "confidence": 0.99,
        }],
        "repair_plan_error": None,
    })
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return first

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    assert len(calls) == 1
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == body
        assert article["quality"]["promotion_repair"]["applied"] is False
        assert article["quality"]["promotion_repair"]["plan_error"]
    finally:
        conn.close()


def test_pipeline_repair_plan_error_goes_to_review_without_changing_body(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
    _fetch(monkeypatch, [_item("contradictory-plan", body=body)])
    first = deepcopy(FIRST_DIRTY)
    first.update({
        "semantic_check": {
            "has_ad_or_dirty": False,
            "needs_review": False,
        },
        "repair_plans": [{
            "block_id": "b1",
            "action": "remove_block",
            "evidence": ARTICLE_TEXT,
            "confidence": 0.99,
        }],
        "repair_plan_error": "AI 修复计划与质检结论矛盾",
    })
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: first)

    result = run_once(_config(database))

    assert result["status_counts"] == {"NEEDS_REVIEW": 1}
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == body
        repair = article["quality"]["promotion_repair"]
        assert repair["attempted"] is True
        assert repair["applied"] is False
        assert repair["plan_error"] == "AI 修复计划与质检结论矛盾"
        events = _event_map(article["id"], conn)
        assert "QUALITY_ERROR" not in events
        assert events["AUTO_REPAIR_FINISHED"][0]["payload"]["final_status"] == (
            "NEEDS_REVIEW"
        )
    finally:
        conn.close()


def test_pipeline_second_quality_requires_valid_empty_issue_schema(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("invalid-second")])
    invalid_second = deepcopy(SECOND_PASS)
    invalid_second["issues"]["dirty_content"] = ["结构上仍有问题"]
    answers = iter([deepcopy(FIRST_DIRTY), invalid_second])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["quality"]["promotion_repair"]["outcome"] == "failed"
        finished = _event_map(article["id"], conn)["AUTO_REPAIR_FINISHED"][0]
        assert finished["payload"]["final_status"] == "NEEDS_REVIEW"
    finally:
        conn.close()


def test_pipeline_does_not_batch_process_unfetched_historical_article(app, monkeypatch) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    historical = _item("historical")
    conn = _connect(database)
    try:
        old_article = repo.upsert_material(historical, conn)["article"]
        repo.transition_status(
            old_article["id"],
            "NEEDS_REVIEW",
            conn,
            message="historical article already awaiting manual review",
        )
        old_body = old_article["body_html"]
    finally:
        conn.close()

    new_item = _item("new", body=f"<p>{ARTICLE_TEXT}</p>{IMAGE}")
    _fetch(monkeypatch, [new_item])
    monkeypatch.setattr(
        "app.services.pipeline.evaluate", lambda **kwargs: deepcopy(SECOND_PASS)
    )

    run_once(_config(database))

    conn = _connect(database)
    try:
        historical_after = repo.get_article(old_article["id"], conn)
        assert historical_after["status"] == "NEEDS_REVIEW"
        assert historical_after["body_html"] == old_body
        events = _event_map(old_article["id"], conn)
        assert "QUALITY_STARTED" not in events
        assert "AUTO_REPAIR_TRIGGERED" not in events
    finally:
        conn.close()


def test_pipeline_strips_caption_and_byline_before_first_quality(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    caption = "【图片】“感觉很强”大阪钢巴新援卡马拉！"
    byline = "编写●足球文摘Web编辑部"
    body = (
        f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
        f"<p>{ARTICLE_TEXT}<br>{caption}<br>{ARTICLE_TEXT}</p>"
        f"<p>{byline}</p>"
    )
    _fetch(monkeypatch, [_item("caption-strip", body=body)])
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return deepcopy(SECOND_PASS)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    result = run_once(_config(database))

    assert result["status_counts"] == {"READY_TO_PUBLISH": 1}
    # The artifacts are gone before the first check, so no repair round is needed.
    assert len(calls) == 1
    assert caption not in calls[0]["body"]
    assert byline not in calls[0]["body"]
    assert calls[0]["body"].count(ARTICLE_TEXT) == 3
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert caption not in article["body_html"]
        assert byline not in article["body_html"]
        assert IMAGE in article["body_html"]
        payload = _event_map(article["id"], conn)["LINKS_REMOVED"][0]["payload"]
        assert payload["rule_version"] == "quality-preprocess-v2"
        assert payload["applied"] is True
        assert payload["image_count_before"] == payload["image_count_after"] == 1
        assert [item["text"] for item in payload["media_artifact_lines"]] == [caption, byline]
        assert [item["rule"] for item in payload["media_artifact_lines"]] == [
            "media_caption_line",
            "editorial_byline_line",
        ]
    finally:
        conn.close()


def test_schedule_transient_rechecks_requeues_llm_outage_articles(app) -> None:
    from app.services import pipeline as pipeline_module

    database = app.config["DATABASE"]
    _enable_source(database)
    conn = _connect(database)
    try:
        saved = repo.upsert_material(_item("transient-outage"), conn)
        article_id = int(saved["article"]["id"])
        quality = {
            "pass": False,
            "needs_review": True,
            "score": 50,
            "level": "B",
            "issues": {
                "title_problems": [],
                "dirty_content": [],
                "completeness_problems": [],
                "channel_problems": [],
                "semantic_problems": [
                    "AI 服务暂时不可用，已重试仍未返回，需要人工确认"
                ],
            },
            "reason": "AI 服务暂时不可用，已重试仍未返回，需要人工确认",
        }
        conn.execute(
            "UPDATE articles SET status='NEEDS_REVIEW', quality_json=?, "
            "updated_at='2020-01-01T00:00:00.000Z' WHERE id=?",
            (json.dumps(quality, ensure_ascii=False), article_id),
        )
        conn.commit()
    finally:
        conn.close()

    scheduled = pipeline_module.schedule_transient_rechecks(
        _llm_config(database), _connect(database)
    )
    assert article_id in scheduled
    conn = _connect(database)
    try:
        moved = repo.get_article(article_id, conn)
        assert moved["status"] == "RECEIVED"
    finally:
        conn.close()


def test_schedule_transient_rechecks_skips_content_failures(app) -> None:
    from app.services import pipeline as pipeline_module

    database = app.config["DATABASE"]
    _enable_source(database)
    conn = _connect(database)
    try:
        saved = repo.upsert_material(_item("content-failure"), conn)
        article_id = int(saved["article"]["id"])
        quality = {
            "pass": False,
            "needs_review": True,
            "score": 50,
            "issues": {
                "title_problems": [],
                "dirty_content": ["疑似广告或脏内容：摄影[：:]"],
                "completeness_problems": [],
                "channel_problems": [],
                "semantic_problems": [],
            },
            "reason": "疑似广告或脏内容：摄影[：:]",
        }
        conn.execute(
            "UPDATE articles SET status='NEEDS_REVIEW', quality_json=?, "
            "updated_at='2020-01-01T00:00:00.000Z' WHERE id=?",
            (json.dumps(quality, ensure_ascii=False), article_id),
        )
        conn.commit()
    finally:
        conn.close()

    scheduled = pipeline_module.schedule_transient_rechecks(
        _llm_config(database), _connect(database)
    )
    assert article_id not in scheduled


# 段内重复句与无关插入句：模型给的都是逐字删除计划，此前被形态白名单一律否决。
DUPLICATE_SENTENCE = "结果，天安综合运动场获得最高分。"
DUPLICATE_PARAGRAPH = (
    "绿茵场奖的评选综合了比赛监督和客队球员的评价，并纳入主裁判的评分。"
    f"{DUPLICATE_SENTENCE}{DUPLICATE_SENTENCE}"
    "天安市公社持续改善草皮密度，维持场地最佳状态。"
)
EXTRANEOUS_FRAGMENT = "长泽雅美、Gakki、广濑铃等人都被压过，排在第1的是……"
EXTRANEOUS_PARAGRAPH = (
    "据雅虎日本报道，J1联赛町田泽维亚宣布，中场增山朝阳租借加盟长崎。"
    f"{EXTRANEOUS_FRAGMENT}"
    "东福冈高中时期被称为“东之C罗”的增山，此前效力于神户胜利船。"
)


def _dirty_with_repair_plan(plans: list[dict]) -> dict:
    quality = deepcopy(FIRST_DIRTY)
    quality.update({
        "decision": "repairable",
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": True,
            "needs_review": False,
        },
        "repair_plans": plans,
        "repair_plan_error": None,
    })
    return quality


def test_pipeline_removes_interior_duplicate_sentence_without_model_call(
    app, monkeypatch
) -> None:
    """段内重复句删除由机械判据放行：信息在正文别处仍存在，无需核验调用。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{DUPLICATE_PARAGRAPH}</p>{IMAGE}"
    _fetch(monkeypatch, [_item("interior-duplicate", body=body)])
    first = _dirty_with_repair_plan([{
        "block_id": "b1",
        "action": "replace_text",
        "evidence": DUPLICATE_PARAGRAPH,
        "after": DUPLICATE_PARAGRAPH.replace(DUPLICATE_SENTENCE, "", 1),
        "issue_type": "duplicate_content",
        "reason": "该段中这句与前一句完全重复，删除后语义不变",
        "confidence": 0.99,
    }])
    answers = iter([first, deepcopy(SECOND_PASS)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    def fail_verification(**kwargs):
        raise AssertionError("重复内容不应触发核验调用")

    monkeypatch.setattr(
        "app.services.pipeline.verify_removal_keeps_facts", fail_verification
    )

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["body_html"].count(DUPLICATE_SENTENCE) == 1
        assert IMAGE in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "passed"
        assert repair["applied"] is True
        assert repair["matches"][0]["validation"] == "ai_verified_removal"
        assert repair["removal_verification"][0]["channel"] == "duplicate_elsewhere"
    finally:
        conn.close()


def test_pipeline_removes_unrelated_fragment_after_fact_verification(
    app, monkeypatch
) -> None:
    """无关插入句不命中任何残留词典，改由一次信息保全核验放行。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{EXTRANEOUS_PARAGRAPH}</p>{IMAGE}"
    _fetch(monkeypatch, [_item("unrelated-infix", body=body)])
    first = _dirty_with_repair_plan([{
        "block_id": "b1",
        "action": "replace_text",
        "evidence": EXTRANEOUS_PARAGRAPH,
        "after": EXTRANEOUS_PARAGRAPH.replace(EXTRANEOUS_FRAGMENT, "", 1),
        "issue_type": "extraneous_content",
        "reason": "该句提及明星排名，与租借新闻无关，删除后不影响新闻事实",
        "confidence": 0.98,
    }])
    answers = iter([first, deepcopy(SECOND_PASS)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))
    verifications: list[dict] = []

    def fake_verification(**kwargs):
        verifications.append(kwargs)
        return {"safe": True, "loses_fact": False, "confidence": 0.97, "reason": "无事实丢失"}

    monkeypatch.setattr(
        "app.services.pipeline.verify_removal_keeps_facts", fake_verification
    )

    run_once(_llm_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert EXTRANEOUS_FRAGMENT not in article["body_html"]
        assert "增山朝阳租借加盟长崎" in article["body_html"]
        assert "神户胜利船" in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "passed"
        assert repair["removal_verification"][0]["channel"] == "fact_verification"
    finally:
        conn.close()
    # 每个片段只核验一次，计划校验的重试不应放大调用量。
    assert len(verifications) == 1
    assert verifications[0]["removed_text"] == EXTRANEOUS_FRAGMENT


def test_pipeline_keeps_body_when_fact_verification_declines(app, monkeypatch) -> None:
    """核验认为会丢事实时保持 fail-closed：正文不动，转人工并记录原因。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{EXTRANEOUS_PARAGRAPH}</p>{IMAGE}"
    _fetch(monkeypatch, [_item("verification-veto", body=body)])
    first = _dirty_with_repair_plan([{
        "block_id": "b1",
        "action": "replace_text",
        "evidence": EXTRANEOUS_PARAGRAPH,
        "after": EXTRANEOUS_PARAGRAPH.replace(EXTRANEOUS_FRAGMENT, "", 1),
        "issue_type": "extraneous_content",
        "reason": "该句疑似与新闻无关",
        "confidence": 0.98,
    }])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: deepcopy(first))
    monkeypatch.setattr(
        "app.services.pipeline.verify_removal_keeps_facts",
        lambda **kwargs: {
            "safe": False,
            "loses_fact": True,
            "confidence": 0.94,
            "reason": "被删片段含删除后无法找回的信息",
        },
    )

    run_once(_llm_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == body
        repair = article["quality"]["promotion_repair"]
        assert repair["applied"] is False
        assert repair["plan_error"] == "AI 删除内容未通过信息保全核验"
        assert repair["removal_verification"][0]["safe"] is False
    finally:
        conn.close()


def test_failed_repair_retries_after_rule_version_bump(app, monkeypatch) -> None:
    """失败的修复没有改动正文，因此校验规则升级后应允许再尝试一次。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    body = f"<p>{EXTRANEOUS_PARAGRAPH}</p>{IMAGE}"
    _fetch(monkeypatch, [_item("rule-version-retry", body=body)])
    plan = {
        "block_id": "b1",
        "action": "replace_text",
        "evidence": EXTRANEOUS_PARAGRAPH,
        "after": EXTRANEOUS_PARAGRAPH.replace(EXTRANEOUS_FRAGMENT, "", 1),
        "issue_type": "extraneous_content",
        "reason": "该句提及明星排名，与租借新闻无关",
        "confidence": 0.98,
    }
    monkeypatch.setattr(
        "app.services.pipeline.evaluate",
        lambda **kwargs: _dirty_with_repair_plan([deepcopy(plan)]),
    )
    monkeypatch.setattr(
        "app.services.pipeline.verify_removal_keeps_facts",
        lambda **kwargs: {"safe": False, "loses_fact": True, "confidence": 0.95, "reason": "保守拒绝"},
    )

    run_once(_llm_config(database))
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        repo.transition_status(article["id"], "ERROR", conn, message="rerun after fix")
    finally:
        conn.close()

    # 规则版本提升 + 核验放行：同一篇文章重新获得一次修复机会。
    monkeypatch.setattr("app.services.pipeline.REPAIR_RULE_VERSION", 999)
    answers = iter([_dirty_with_repair_plan([deepcopy(plan)]), deepcopy(SECOND_PASS)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))
    monkeypatch.setattr(
        "app.services.pipeline.verify_removal_keeps_facts",
        lambda **kwargs: {"safe": True, "loses_fact": False, "confidence": 0.97, "reason": "无事实丢失"},
    )

    run_once(_llm_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert EXTRANEOUS_FRAGMENT not in article["body_html"]
        assert article["quality"]["promotion_repair"]["outcome"] == "passed"
    finally:
        conn.close()


def _dirty_without_plan(targets: list[dict]) -> dict:
    """复刻线上最大一桶：报脏、给出定位、但拒绝给 repair_plans。"""
    quality = deepcopy(FIRST_DIRTY)
    quality.update({
        "decision": "manual_review",
        "semantic_check": {
            "title_complete": True,
            "body_complete": True,
            "has_ad_or_dirty": True,
            "repairable": False,
            "needs_review": False,
        },
        "repair_plans": [],
        "repair_plan_error": None,
        "dirty_targets": targets,
    })
    return quality


def test_pipeline_synthesizes_plan_from_dirty_targets(app, monkeypatch) -> None:
    """模型只说位置不给计划时，程序据此合成删除计划并完成修复。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("dirty-target-synthesis")])
    first = _dirty_without_plan([{
        "block_id": "b2",
        "evidence": PROMOTION,
        "issue_type": "promotion",
        "reason": "独立推广段落，与新闻事实无关",
    }])
    answers = iter([first, deepcopy(SECOND_PASS)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))
    # 规划器不该被调用：位置已经给出，不需要再花一次调用去规划。
    monkeypatch.setattr(
        "app.services.pipeline.plan_local_repair",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("不应调用规划器")),
    )

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert PROMOTION not in article["body_html"]
        assert ARTICLE_TEXT in article["body_html"]
        assert IMAGE in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["outcome"] == "passed"
        assert repair["applied"] is True
        assert repair["dirty_target_plans"][0]["action"] == "remove_block"
    finally:
        conn.close()


def test_pipeline_falls_back_to_planner_without_dirty_targets(app, monkeypatch) -> None:
    """没有可引用的定位时保持原路径：调用规划器，仍不可修则转人工。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    _fetch(monkeypatch, [_item("dirty-target-missing")])
    monkeypatch.setattr(
        "app.services.pipeline.evaluate", lambda **kwargs: _dirty_without_plan([])
    )
    calls: list[dict] = []

    def fake_planner(**kwargs):
        calls.append(kwargs)
        return {"repairable": False, "reason": "无法局部处理", "repair_plans": [],
                "repair_plan_error": None}

    monkeypatch.setattr("app.services.pipeline.plan_local_repair", fake_planner)

    run_once(_llm_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert PROMOTION in article["body_html"]
        assert "dirty_target_plans" not in article["quality"]["promotion_repair"]
    finally:
        conn.close()
    assert len(calls) == 1


def test_pipeline_synthesized_inline_plan_needs_fact_verification(app, monkeypatch) -> None:
    """合成的段内删除同样要过信息保全核验，核验否决则正文不动。"""

    database = app.config["DATABASE"]
    _enable_source(database)
    # 刻意选一段不命中任何残留词典的文字，逼迫走核验通道而不是免检快路径。
    tail = "顺带一提，当地的天气一直不错。"
    paragraph = f"{ARTICLE_TEXT}{tail}"
    body = f"<p>{paragraph}</p>{IMAGE}"
    _fetch(monkeypatch, [_item("dirty-target-inline", body=body)])
    monkeypatch.setattr(
        "app.services.pipeline.evaluate",
        lambda **kwargs: _dirty_without_plan([{
            "block_id": "b1",
            "evidence": tail,
            "issue_type": "extraneous_content",
            "reason": "段尾与本篇新闻无关的闲话",
        }]),
    )
    monkeypatch.setattr(
        "app.services.pipeline.verify_removal_keeps_facts",
        lambda **kwargs: {"safe": False, "loses_fact": True, "confidence": 0.96,
                          "reason": "被删片段含删除后无法找回的信息"},
    )
    monkeypatch.setattr(
        "app.services.pipeline.plan_local_repair",
        lambda **kwargs: {"repairable": False, "reason": "无法局部处理",
                          "repair_plans": [], "repair_plan_error": None},
    )

    run_once(_llm_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert article["body_html"] == body
        repair = article["quality"]["promotion_repair"]
        # 核验否决 → 合成计划被丢弃 → 正文一个字都没动
        assert repair["applied"] is False
        assert "dirty_target_plans" not in repair
        assert repair["removal_verification"][0]["safe"] is False
    finally:
        conn.close()
