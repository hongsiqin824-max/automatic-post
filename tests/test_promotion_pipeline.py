from __future__ import annotations

from copy import deepcopy

from app import repository as repo
from app.config import AppConfig
from app.db import _connect
from app.services.material_client import MaterialFetchResult
from app.services.pipeline import run_once
from app.services.promotion_repair import body_safety_stats


TITLE = "澳超新赛季赛程公布及揭幕战安排确认"
ARTICLE_TEXT = "澳超官方公布了新赛季安排，揭幕战将在十月进行，各支球队正在按计划完成季前备战。"
PROMOTION = "点击查看2026/27赛季五十铃UTE澳超完整赛程"
VIDEO_TEASER = "【视频】佐藤龙之介送出引发进球的凶狠逼抢，以及他的威胁场面"
PROGRAM_PROMOTION = "●矢部浩之先生的新节目《J.LEAGUE WEEKEND 周日的矢部萨卡》开播！ ｜ J联赛"
SCOREBOARD_MARKER = "【积分榜】明治安田J1联赛2026/27"
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
    extra = "【图片】三井寺眞崇拜的两位世界级球员"
    body = f"<p>{ARTICLE_TEXT}</p>{IMAGE}<p>{extra}</p>"
    _fetch(monkeypatch, [_item("separate-planner", body=body)])
    first = _quality(needs_review=True, reason="正文含与新闻无关的图片入口")
    first["issues"]["semantic_problems"] = ["正文含与新闻无关的图片入口"]
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
            "reason": "可删除精确定位的图片入口",
            "repair_plans": [{
                "block_id": "b2",
                "action": "remove_block",
                "evidence": extra,
                "issue_type": "extraneous_content",
                "reason": "该图片入口与新闻事实无关",
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
        assert "[照片]" in article["body_html"]
        assert f"{caption}（图片来源：Getty Images）" not in article["body_html"]
        repair = article["quality"]["promotion_repair"]
        assert repair["attribution_normalized_count"] == 1
        assert repair["removed_count"] == 0
        assert repair["outcome"] == "failed"
        assert repair["candidate_committed"] is False

        events = _event_map(article["id"], conn)
        assert "AUTO_REPAIR_APPLIED" not in events
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
    assert whatsapp in calls[0]["body"] and watch in calls[0]["body"]
    assert whatsapp not in calls[1]["body"] and watch not in calls[1]["body"]
    assert IMAGE in calls[0]["body"] and IMAGE in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert article["body_html"] == f"<p>{ARTICLE_TEXT}</p>{IMAGE}"
        repair = article["quality"]["promotion_repair"]
        assert repair["removed_count"] == 2
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


def test_pipeline_applies_ai_line_plan_and_runs_second_quality(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    body = (
        f"<p>{ARTICLE_TEXT}\n{VIDEO_TEASER}\n赛后主教练接受采访并肯定了球队的表现。</p>"
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
            "evidence": VIDEO_TEASER,
            "issue_type": "video_promotion",
            "reason": "该独立视频引流行与新闻事实无关",
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
    assert VIDEO_TEASER in calls[0]["body"]
    assert VIDEO_TEASER not in calls[1]["body"]
    assert ARTICLE_TEXT in calls[1]["body"]
    assert IMAGE in calls[1]["body"]
    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert VIDEO_TEASER not in article["body_html"]
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


def test_pipeline_applies_scoreboard_ai_plan_with_guidance_reason_and_preserves_images(
    app, monkeypatch
) -> None:
    database = app.config["DATABASE"]
    _enable_source(database)
    image_one = (
        '<IMG SRC="/fastdfs8/scoreboard-main.jpg" ALT="主图" width="640" '
        'loading="lazy" class="hero" data-slot="main">'
    )
    image_two = (
        '<img class="detail" src="https://img.example/scoreboard-detail.jpg" '
        'alt="细节图" width="320" height="180" data-slot="detail">'
    )
    context = (
        "报道完整介绍了球员转会背景、合同安排、球队计划和后续训练，"
        "并引用了俱乐部及相关人员对下一阶段工作的说明。"
    )
    body = (
        f"<p>{context}</p>{image_one}<p>{SCOREBOARD_MARKER}</p>"
        f"{image_two}<p>{context}</p><p>{context}</p>"
    )
    _fetch(monkeypatch, [_item("ai-scoreboard", body=body)])
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
            "evidence": SCOREBOARD_MARKER,
            "issue_type": "traffic_generation",
            "reason": "该独立积分榜入口用于引导访问赛事榜单，与球员转会新闻事实无关",
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
    assert SCOREBOARD_MARKER in calls[0]["body"]
    assert SCOREBOARD_MARKER not in calls[1]["body"]
    assert body_safety_stats(calls[1]["body"])["image_count"] == before_stats["image_count"]
    assert body_safety_stats(calls[1]["body"])["image_sources"] == before_stats["image_sources"]
    assert body_safety_stats(calls[1]["body"])["image_attributes"] == before_stats["image_attributes"]

    conn = _connect(database)
    try:
        article = repo.list_articles(conn)[0]
        assert article["status"] == "READY_TO_PUBLISH"
        assert SCOREBOARD_MARKER not in article["body_html"]
        assert body_safety_stats(article["body_html"])["image_attributes"] == before_stats["image_attributes"]
        events = _event_map(article["id"], conn)
        assert [event["payload"]["quality_round"] for event in events["QUALITY_RESULT"]] == [1, 2]
        applied = events["AUTO_REPAIR_APPLIED"][0]["payload"]
        assert applied["image_sources_unchanged"] is True
        assert applied["image_attributes_unchanged"] is True
        assert applied["removed_blocks"][0]["issue_type"] == "traffic_generation"
        assert applied["removed_blocks"][0]["reason"] == "该独立积分榜入口用于引导访问赛事榜单，与球员转会新闻事实无关"
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
