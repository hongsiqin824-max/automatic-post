"""Flask application and JSON endpoints for the pre-publish workflow."""

from __future__ import annotations

import html
import json
import os
import sqlite3
from urllib.parse import urlparse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request

from . import db, repository as repo
from .catalog import seed_catalog
from .config import AppConfig, ensure_instance_dir
from .db import get_db, init_app as init_db_app, init_db
from .services.material_client import cdn_url
from .services.open_platform import OpenPlatformClient, auth_record_summary, build_draft_url
from .services.pipeline import RunController
from .services.scheduler import Scheduler
from .services.publisher import DraftClaimSkipped, create_draft_for_article
from .services.quality import html_to_text
from .statuses import STATUS_LABELS

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

STATUS_ALIASES = {
    "ingested": "RECEIVED",
    "received": "RECEIVED",
    "quality_checking": "QUALITY_CHECKING",
    "needs_review": "NEEDS_REVIEW",
    "ready_to_publish": "READY_TO_PUBLISH",
    "publishing": "PUBLISHING",
    "draft_created": "DRAFT_CREATED",
    "publish_failed": "PUBLISH_FAILED",
    "published": "ALREADY_PUBLISHED",
    "already_published": "ALREADY_PUBLISHED",
    "rejected": "REJECTED",
    "failed": "ERROR",
    "error": "ERROR",
}

EVENT_LABELS = {
    "MATERIAL_RECEIVED": "素材进入系统",
    "QUALITY_STARTED": "开始自动质检",
    "QUALITY_SAVED": "保存质检结果",
    "QUALITY_RESULT": "自动质检完成",
    "QUALITY_ERROR": "自动质检失败",
    "TITLE_FIXED": "自动修正标题",
    "MANUAL_REVIEW": "人工审核",
    "ALREADY_PUBLISHED_DETECTED": "发现已有文章 ID",
    "PUBLISHED": "发布完成",
    "DRAFT_CREATE_STARTED": "开始创建草稿",
    "DRAFT_CREATED": "草稿创建完成",
    "DRAFT_CREATE_FAILED": "草稿创建失败",
    "DRAFT_CREATE_BLOCKED": "草稿创建被阻止",
    "DRAFT_RETRY_STARTED": "开始重新创建草稿",
    "DRAFT_RETRY_SUCCEEDED": "重新创建草稿成功",
    "DRAFT_RETRY_FAILED": "重新创建草稿失败",
    "DRAFT_RETRY_BLOCKED": "草稿重试被阻止",
    "DRAFT_ALREADY_EXISTS": "草稿已存在",
    "PUBLISHING_RECOVERED": "草稿状态已自动恢复",
    "PUBLISHING_TIMED_OUT": "草稿创建超时自动恢复",
    "STATUS_CHANGED": "状态更新",
}


def format_beijing_time(value: Any) -> str:
    """Format stored UTC timestamps for operators in Beijing time."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _status_value(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return STATUS_ALIASES.get(text.lower(), text.upper())


def _status_key(value: str | None) -> str:
    return str(value or "").lower()


def _tab_view(tab: dict | None) -> dict:
    tab = dict(tab or {})
    tab.setdefault("name", "未配置")
    tab.setdefault("id", None)
    return tab


def _source_view(source: dict) -> dict:
    item = dict(source)
    item["name"] = item.get("display_name") or item.get("code")
    return item


def _article_view(article: dict | None) -> dict | None:
    if article is None:
        return None
    item = dict(article)
    item["status_key"] = _status_key(item.get("status"))
    item["status_label"] = STATUS_LABELS.get(item.get("status", ""), item.get("status", "未知"))
    quality = item.get("quality") or {}
    issues = quality.get("issues") if isinstance(quality, dict) else {}
    issues = issues if isinstance(issues, dict) else {}
    title_problems = issues.get("title_problems") or []
    completeness = issues.get("completeness_problems") or []
    dirty = issues.get("dirty_content") or []
    item["quality"] = quality
    item["quality_passed"] = quality.get("pass") if isinstance(quality, dict) else None
    item["quality_reason"] = quality.get("reason", "") if isinstance(quality, dict) else ""
    item["title_complete"] = not title_problems
    item["body_complete"] = not completeness
    item["has_ad"] = any("广告" in str(v) or "购买" in str(v) for v in dirty)
    item["has_dirty"] = bool(dirty)
    item["needs_review"] = bool(quality.get("needs_review")) if isinstance(quality, dict) else None
    item["body_excerpt"] = html_to_text(item.get("body_html", ""))[:360]
    item["cover_url"] = cdn_url(item.get("litpic"))
    draft_archive_id = int(item.get("dqd_archive_id") or 0)
    item["has_dqd_draft"] = draft_archive_id > 0
    item["draft_recovered"] = item["status_key"] == "publish_failed" and draft_archive_id > 0
    item["draft_action_label"] = "同步草稿状态" if draft_archive_id > 0 else "重新创建懂球帝草稿"
    item["draft_action_confirm"] = "确认同步草稿状态吗？" if draft_archive_id > 0 else "确认重新创建懂球帝草稿吗？"
    item["draft_notice"] = (
        "这条文章其实已经创建过懂球帝草稿，当前失败更可能发生在后续步骤，可直接打开链接。"
        if item["draft_recovered"]
        else ""
    )
    item["tab"] = _tab_view({
        "id": item.get("tab_id"),
        "name": item.get("tab_name") or "未配置栏目",
        "backend_tab_id": item.get("backend_tab_id"),
    })
    item["channels"] = [{"id": channel_id} for channel_id in (item.get("channels") or [])]
    item["draft_url"] = build_draft_url(item.get("dqd_archive_id"))
    return item


def _event_view(event: dict) -> dict:
    item = dict(event)
    target = event.get("to_status")
    event_type = str(event.get("event_type") or "")
    item["label"] = EVENT_LABELS.get(event_type) or STATUS_LABELS.get(target or "", "状态更新")
    item["message"] = event.get("message") or {
        "QUALITY_SAVED": "质检结果已保存",
        "QUALITY_RESULT": "质检结果已记录",
        "STATUS_CHANGED": "状态已更新",
    }.get(event_type, "")
    if event.get("payload_json"):
        try:
            item["payload"] = json.loads(event["payload_json"])
        except (TypeError, ValueError):
            item["payload"] = {}
    return item


def _settings_view(app: Flask) -> dict:
    cfg: AppConfig = app.extensions["app_config"]
    scheduler: Scheduler = app.extensions["scheduler"]
    conn = get_db()
    enabled_sources = len(repo.list_sources(conn, include_disabled=False))
    open_auth = auth_record_summary(db.get_open_platform_auth(cfg.database_path))
    return {
        "scheduler_enabled": bool(scheduler.enabled),
        "interval_minutes": max(1, cfg.scheduler_interval_seconds // 60),
        "last_run_at": repo.get_setting("last_run_at", "", conn),
        "next_run_at": scheduler.next_run_at or "",
        "last_error": repo.get_setting("last_error", "", conn),
        "enabled_sources": enabled_sources,
        "material_configured": cfg.material_configured,
        "llm_configured": cfg.llm_configured,
        "dqd_configured": cfg.dqd_configured,
        "dqd_open_configured": cfg.dqd_open_configured,
        "dqd_open_redirect_uri": cfg.dqd_open_redirect_uri,
        "open_platform_auth": open_auth,
        "publisher_enabled": cfg.publisher_enabled,
    }


def _run_views(conn: Any) -> list[dict]:
    result = []
    for row in repo.list_run_logs(conn, limit=12):
        item = dict(row)
        try:
            details = json.loads(item.get("details_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        item["fetched"] = item.get("fetched_count", details.get("fetched", 0))
        item["created"] = item.get("inserted_count", details.get("inserted", 0))
        item["duplicates"] = item.get("updated_count", details.get("updated", 0))
        publish = details.get("publish") if isinstance(details, dict) else {}
        item["drafts"] = int((publish or {}).get("draft_created", 0)) if isinstance(publish, dict) else 0
        failed_items = details.get("failed_items") if isinstance(details, dict) else []
        item["message"] = item.get("error") or details.get("message") or "任务执行完成"
        if failed_items:
            first = failed_items[0] if isinstance(failed_items[0], dict) else {}
            item["message"] = f"{len(failed_items)} 条素材失败；首条 {first.get('source', '未知来源')}：{first.get('error', '处理失败')}"
        item["status"] = str(item.get("status", "")).lower()
        item["status_label"] = {"success": "完成", "partial": "部分完成", "error": "失败", "running": "运行中"}.get(item["status"], item["status"])
        result.append(item)
    return result


def _dashboard_status_groups(stats: dict[str, int]) -> list[dict]:
    definitions = [
        ("RECEIVED", "已获取", "等待进入质检"),
        ("QUALITY_CHECKING", "质检中", "正在检查标题和正文"),
        ("NEEDS_REVIEW", "待人工审核", "需要人工确认或修正"),
        ("READY_TO_PUBLISH", "待发队列", "质检通过，等待发布适配"),
        ("PUBLISHING", "创建草稿中", "正在调用开放平台"),
        ("DRAFT_CREATED", "草稿已创建", "开放平台已返回 archive_id"),
        ("PUBLISH_FAILED", "创建草稿失败", "开放平台调用失败"),
        ("ALREADY_PUBLISHED", "已存在后台文章", "接口记录已有文章 ID"),
        ("REJECTED", "已驳回", "不会进入发布队列"),
        ("ERROR", "处理失败", "可查看详情后重试"),
    ]
    return [
        {"status": _status_key(code), "label": label, "description": desc, "count": stats.get(code, 0)}
        for code, label, desc in definitions
    ]


def create_app(test_config: dict | None = None) -> Flask:
    ensure_instance_dir()
    cfg = AppConfig()
    if test_config and test_config.get("DATABASE"):
        cfg = replace(cfg, database_path=str(test_config["DATABASE"]))
    _secret_key = os.getenv("AUTOMATIC_POST_SECRET")
    if not _secret_key:
        if test_config is not None:
            _secret_key = "test-only-secret-not-for-production"
        else:
            raise RuntimeError(
                "环境变量 AUTOMATIC_POST_SECRET 未设置。"
                "请在 .env 文件中配置：AUTOMATIC_POST_SECRET=<随机字符串>"
            )
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        static_folder=str(Path(__file__).resolve().parents[1] / "static"),
    )
    app.config.from_mapping(
        SECRET_KEY=_secret_key,
        DATABASE=cfg.database_path,
        TESTING=bool(test_config and test_config.get("TESTING")),
    )
    if test_config:
        app.config.update(test_config)
    app.extensions["app_config"] = cfg
    init_db_app(app)
    with app.app_context():
        init_db()
        seed_catalog()

    controller = RunController(cfg)
    scheduler = Scheduler(controller, cfg.scheduler_interval_seconds)
    app.extensions["run_controller"] = controller
    app.extensions["scheduler"] = scheduler
    with app.app_context():
        scheduler_enabled = bool(repo.get_setting("scheduler_enabled", cfg.scheduler_enabled))
    if scheduler_enabled and not app.config.get("TESTING") and not test_config:
        scheduler.start()

    app.jinja_env.globals["cdn_url"] = cdn_url
    app.jinja_env.globals["status_label"] = lambda value: STATUS_LABELS.get(value, value)
    app.jinja_env.filters["beijing_time"] = format_beijing_time

    def _safe_back_url(fallback: str) -> str:
        """Return request.referrer only when it is same-origin; fall back otherwise."""
        ref = request.referrer
        if not ref:
            return fallback
        try:
            parsed = urlparse(ref)
            if parsed.scheme in ("http", "https") and parsed.netloc == request.host:
                return ref
        except Exception:
            pass
        return fallback

    app.jinja_env.globals["safe_back_url"] = _safe_back_url

    @app.context_processor
    def inject_navigation_context():
        try:
            conn = get_db()
            review_count = repo.count_articles(conn, status="NEEDS_REVIEW")
            settings = _settings_view(app)
        except Exception:
            app.logger.exception("导航上下文加载失败")
            review_count, settings = 0, {"scheduler_enabled": False}
        return {"review_count": review_count, "settings": settings}

    @app.get("/")
    def dashboard():
        conn = get_db()
        stats = repo.article_counts(conn)
        recent = [_article_view(row) for row in repo.list_articles(conn, limit=12)]
        runs = _run_views(conn)
        return render_template(
            "dashboard.html",
            stats={key.lower(): value for key, value in stats.items()},
            status_groups=_dashboard_status_groups(stats),
            recent=recent,
            runs=runs,
        )

    @app.get("/articles")
    def articles():
        conn = get_db()
        status = _status_value(request.args.get("status"))
        tab_arg = request.args.get("tab")
        try:
            tab_id = int(tab_arg) if tab_arg else None
        except ValueError:
            tab_id = None
        rows = repo.list_articles(
            conn,
            status=status,
            source=request.args.get("source") or None,
            tab_id=tab_id,
            query=request.args.get("q") or None,
            limit=200,
        )
        return render_template(
            "articles.html",
            articles=[_article_view(row) for row in rows],
            tabs=[_tab_view(row) for row in repo.list_tabs(conn)],
            sources=[_source_view(row) for row in repo.list_sources(conn)],
            status_labels={_status_key(key): value for key, value in STATUS_LABELS.items()},
            filters={"q": request.args.get("q", ""), "status": _status_key(status),
                     "source": request.args.get("source", ""), "tab": tab_arg or ""},
        )

    @app.get("/articles/<int:article_id>")
    def article_detail(article_id: int):
        conn = get_db()
        article = _article_view(repo.get_article(article_id, conn))
        if article is None:
            abort(404)
        events = [_event_view(row) for row in repo.list_article_events(article_id, conn)]
        settings = _settings_view(app)
        return render_template(
            "article_detail.html",
            article=article,
            tab=article.get("tab", {}),
            channels=article.get("channels", []),
            quality=article.get("quality", {}),
            status_label=article.get("status_label"),
            events=events,
            settings=settings,
            can_create_draft=(
                article.get("status_key") in {"ready_to_publish", "publish_failed"}
                and settings["publisher_enabled"]
                and settings["dqd_open_configured"]
            ),
        )

    @app.get("/review")
    def review():
        conn = get_db()
        article_id = request.args.get("article_id")
        rows = repo.list_articles(conn, status="NEEDS_REVIEW", limit=200)
        if article_id:
            try:
                selected = repo.get_article(int(article_id), conn)
                if selected and selected not in rows:
                    rows.insert(0, selected)
            except ValueError:
                pass
        return render_template("review.html", articles=[_article_view(row) for row in rows])

    @app.get("/config", endpoint="config")
    def config_page():
        conn = get_db()
        return render_template(
            "config.html",
            tabs=[_tab_view(row) for row in repo.list_tabs(conn)],
            sources=[_source_view(row) for row in repo.list_sources(conn)],
            settings=_settings_view(app),
        )

    @app.post("/api/run")
    def api_run():
        controller: RunController = app.extensions["run_controller"]
        if not controller.start():
            return jsonify({"success": False, "error": "已有任务正在运行"}), 409
        return jsonify({"success": True, "message": "拉取任务已启动", "status": controller.status()})

    @app.get("/api/run/status")
    def api_run_status():
        return jsonify({"success": True, **app.extensions["run_controller"].status()})

    @app.get("/api/open/auth/status")
    def api_open_auth_status():
        cfg: AppConfig = app.extensions["app_config"]
        return jsonify({
            "success": True,
            "configured": bool(cfg.dqd_open_appid and cfg.dqd_open_appsecret),
            "redirect_uri": cfg.dqd_open_redirect_uri,
            "auth": auth_record_summary(db.get_open_platform_auth(cfg.database_path)),
        })

    @app.post("/api/open/auth/start")
    def api_open_auth_start():
        cfg: AppConfig = app.extensions["app_config"]
        try:
            result = OpenPlatformClient(cfg).start_authorization()
            return jsonify({"success": True, **result})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/api/open/auth/callback")
    def api_open_auth_callback():
        cfg: AppConfig = app.extensions["app_config"]
        error = request.args.get("error", "").strip()
        error_description = request.args.get("error_description", "").strip()
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        client = OpenPlatformClient(cfg)
        if error:
            db.update_open_platform_auth(
                cfg.database_path,
                auth_status="ERROR",
                last_error=f"{error}: {error_description}" if error_description else error,
            )
            return (
                f"<html><body><h1>授权失败</h1><p>{html.escape(error)}</p><p>{html.escape(error_description)}</p><p>你可以关闭这个页面后重试。</p></body></html>",
                400,
            )
        if not code:
            return ("<html><body><h1>缺少授权 code</h1><p>请从开放平台重新发起授权。</p></body></html>", 400)
        try:
            client.handle_callback(code, state or None)
            return "<html><body><h1>授权成功</h1><p>可以关闭这个页面，返回工作台继续创建草稿。</p></body></html>"
        except Exception as exc:
            return (f"<html><body><h1>授权失败</h1><pre>{html.escape(str(exc))}</pre></body></html>", 400)

    @app.post("/api/open/auth/refresh")
    def api_open_auth_refresh():
        cfg: AppConfig = app.extensions["app_config"]
        try:
            token = OpenPlatformClient(cfg).refresh_access_token()
            return jsonify({"success": True, "has_token": bool(token)})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/open/auth/reset")
    def api_open_auth_reset():
        cfg: AppConfig = app.extensions["app_config"]
        item = db.reset_open_platform_auth(cfg.database_path)
        return jsonify({"success": True, "item": item})

    @app.post("/api/scheduler/toggle")
    def api_scheduler_toggle():
        enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
        scheduler: Scheduler = app.extensions["scheduler"]
        if enabled:
            scheduler.start()
        else:
            scheduler.stop()
        repo.set_setting("scheduler_enabled", enabled, get_db())
        return jsonify({"success": True, "enabled": enabled, "message": "自动任务已恢复" if enabled else "自动任务已暂停"})

    @app.get("/api/articles")
    def api_articles():
        conn = get_db()
        status = _status_value(request.args.get("status"))
        rows = repo.list_articles(conn, status=status, source=request.args.get("source"), limit=200)
        return jsonify({"success": True, "items": [_article_view(row) for row in rows]})

    @app.get("/api/articles/<int:article_id>")
    def api_article(article_id: int):
        conn = get_db()
        article = _article_view(repo.get_article(article_id, conn))
        if article is None:
            return jsonify({"success": False, "error": "文章不存在"}), 404
        article["events"] = [_event_view(row) for row in repo.list_article_events(article_id, conn)]
        return jsonify({"success": True, "article": article})

    @app.post("/api/articles/<int:article_id>/review")
    def api_review(article_id: int):
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        if action == "fix_pass":
            action = "manual_fix_then_pass"
        try:
            result = repo.manual_review_update(
                article_id,
                action,
                get_db(),
                title=payload.get("title"),
                body_html=payload.get("body", payload.get("body_html")),
                litpic=payload.get("litpic"),
                channels=payload.get("channels"),
                tab_id=payload.get("tab_id"),
                note=str(payload.get("reason") or payload.get("note") or ""),
            )
            return jsonify({"success": True, "message": "审核操作已保存", "article": _article_view(result)})
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/articles/<int:article_id>/create-draft")
    def api_create_draft(article_id: int):
        cfg: AppConfig = app.extensions["app_config"]
        conn = get_db()
        try:
            result = create_draft_for_article(cfg, conn, article_id)
            article = _article_view(repo.get_article(article_id, conn))
            return jsonify({
                "success": True,
                "message": "检测到已存在草稿链接，已同步状态" if result.get("reused_existing_archive") else "草稿重新创建已提交",
                "result": result,
                "article": article,
            })
        except DraftClaimSkipped as exc:
            article = _article_view(repo.get_article(article_id, conn))
            return jsonify({
                "success": True,
                "message": str(exc),
                "result": {"skipped": True},
                "article": article,
            })
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/tabs")
    def api_create_tab():
        payload = request.get_json(silent=True) or {}
        try:
            backend_id = payload.get("backend_tab_id")
            if backend_id in (None, ""):
                raise ValueError("后台栏目 ID 必须填写，避免创建无法发布的栏目")
            tab = repo.create_tab(str(payload.get("name") or ""), int(backend_id))
            return jsonify({"success": True, "message": "栏目已新增", "tab": tab})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/tabs/<int:tab_id>")
    def api_update_tab(tab_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            tab = repo.update_tab(
                tab_id,
                name=payload.get("name") if "name" in payload else None,
                backend_tab_id=(int(payload["backend_tab_id"]) if payload.get("backend_tab_id") not in (None, "") else None),
                enabled=payload.get("enabled") if "enabled" in payload else None,
            )
            return jsonify({"success": True, "message": "栏目已更新", "tab": tab})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post("/api/sources/<path:code>")
    def api_update_source(code: str):
        payload = request.get_json(silent=True) or {}
        try:
            tab_id = payload.get("tab_id")
            has_tab_field = "tab_id" in payload
            clear_tab = bool(payload.get("clear_tab")) if "clear_tab" in payload else (has_tab_field and tab_id in (None, ""))
            result = repo.update_source(
                code,
                display_name=payload.get("display_name") if "display_name" in payload else None,
                enabled=bool(payload.get("enabled")) if "enabled" in payload else None,
                tab_id=int(tab_id) if tab_id not in (None, "") else None,
                clear_tab=clear_tab,
            )
            return jsonify({"success": True, "message": "来源配置已更新", "source": _source_view(result)})
        except (ValueError, TypeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/health")
    def health():
        cfg: AppConfig = app.extensions["app_config"]
        return jsonify({
            "ok": True,
            "material_api_configured": cfg.material_configured,
            "llm_configured": cfg.llm_configured,
            "dqd_session_configured": cfg.dqd_configured,
            "dqd_open_configured": cfg.dqd_open_configured,
            "dqd_open_redirect_uri": cfg.dqd_open_redirect_uri,
            "publisher_enabled": cfg.publisher_enabled,
        })

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="没有找到这个页面或文章。"), 404

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.exception("未处理的页面错误", exc_info=error)
        return render_template("error.html", code=500, message="系统处理请求时遇到错误，请稍后重试。"), 500

    return app
