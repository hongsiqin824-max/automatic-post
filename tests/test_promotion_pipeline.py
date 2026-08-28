from __future__ import annotations

from copy import deepcopy

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once


TITLE = "澳超新赛季赛程公布及揭幕战安排确认"
ARTICLE_TEXT = "澳超官方公布了新赛季安排，揭幕战将在十月进行，各支球队正在按计划完成季前备战。"
PROMOTION = "点击查看2026/27赛季五十铃UTE澳超完整赛程"
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
        assert PROMOTION not in article["body_html"]
        assert article["quality"]["promotion_repair"]["outcome"] == "failed"
        finished = _event_map(article["id"], conn)["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["outcome"] == "failed"
        assert finished["final_status"] == "NEEDS_REVIEW"
        assert finished["destination"] == "人工审核"
        assert finished["second_quality"]["needs_review"] is True
    finally:
        conn.close()


def test_pipeline_photo_credit_only_repair_passes_second_quality_with_audit(
    app, monkeypatch
) -> None:
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
    answers = iter([deepcopy(FIRST_DIRTY), deepcopy(SECOND_PASS)])

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("app.services.pipeline.evaluate", fake_evaluate)

    run_once(_config(database))

    expected_body = (
        f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
        f"<p>{caption}（图片来源：Getty Images）</p>{second_image}"
    )
    assert len(calls) == 2
    assert calls[0]["body"] == body
    assert calls[1]["body"] == expected_body
    for submitted_body in (calls[0]["body"], calls[1]["body"]):
        assert submitted_body.count("<img") == 2
        assert submitted_body.index('/fastdfs8/promotion-test.jpg') < submitted_body.index(
            "https://img.example/second.jpg"
        )

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["body_html"] == expected_body
        repair = article["quality"]["promotion_repair"]
        assert repair["removed_count"] == 0
        assert repair["attribution_normalized_count"] == 1
        assert repair["outcome"] == "passed"

        events = _event_map(article["id"], conn)
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1, 2]
        triggered = events["AUTO_REPAIR_TRIGGERED"][0]["payload"]
        assert triggered["match_count"] == 1
        assert triggered["rules"] == ["photo_credit_marker"]
        assert triggered["matched_texts"] == [
            f"{caption} [照片]＝Getty Images"
        ]
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["removed_count"] == 0
        assert applied["attribution_normalized_count"] == 1
        assert applied["attribution_only"] is True
        assert applied["image_count_before"] == applied["image_count_after"] == 2
        assert applied["image_sources_unchanged"] is True
        assert applied["attribution_changes"] == [{
            "rule": "photo_credit_marker",
            "tag": "p",
            "caption": caption,
            "source": "Getty Images",
            "before": f"{caption} [照片]＝Getty Images",
            "after": f"{caption}（图片来源：Getty Images）",
        }]
        finished = events["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["outcome"] == "passed"
        assert finished["final_status"] == "READY_TO_PUBLISH"
        assert finished["second_quality"]["needs_review"] is False
    finally:
        conn.close()


def test_pipeline_photo_credit_repair_second_quality_failure_is_audited(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    caption = "双方队长在赛前交换队旗"
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{caption} [照片]=Getty Images</p>"
    _fetch(monkeypatch, [_item("photo-credit-fail", body=body)])
    answers = iter([deepcopy(FIRST_DIRTY), deepcopy(SECOND_FAIL)])
    monkeypatch.setattr("app.services.pipeline.evaluate", lambda **kwargs: next(answers))

    run_once(_config(database))

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "NEEDS_REVIEW"
        assert "[照片]" not in article["body_html"]
        assert f"{caption}（图片来源：Getty Images）" in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["attribution_normalized_count"] == 1
        assert repair["removed_count"] == 0
        assert repair["outcome"] == "failed"

        events = _event_map(article["id"], conn)
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["attribution_only"] is True
        assert applied["image_count_before"] == applied["image_count_after"] == 1
        assert applied["image_sources_unchanged"] is True
        finished = events["AUTO_REPAIR_FINISHED"][0]["payload"]
        assert finished["outcome"] == "failed"
        assert finished["final_status"] == "NEEDS_REVIEW"
        assert finished["destination"] == "人工审核"
        assert finished["second_quality"]["needs_review"] is True
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
        assert PROMOTION not in article["body_html"]
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
        assert len(events["AUTO_REPAIR_APPLIED"]) == 1
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
        assert len(events["AUTO_REPAIR_APPLIED"]) == 1
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
        assert events["AUTO_REPAIR_APPLIED"][0]["payload"]["title_unchanged"] is True
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
