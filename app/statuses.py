"""Workflow statuses shared by persistence, workers and the UI."""

from __future__ import annotations

STATUS_LABELS = {
    "RECEIVED": "已获取",
    "QUALITY_CHECKING": "质检中",
    "NEEDS_REVIEW": "待人工审核",
    "READY_TO_PUBLISH": "待发队列",
    "PUBLISHING": "提交懂球帝中",
    "DRAFT_CONFIRMING": "提交结果确认中",
    "DRAFT_CREATED": "草稿已创建",
    "PUBLISHED": "已直接发布",
    "PUBLISH_FAILED": "提交懂球帝失败",
    "ALREADY_PUBLISHED": "已存在后台文章",
    "REJECTED": "已驳回",
    "MAPPING_BLOCKED": "待匹配后台素材",
    "SOURCE_DUPLICATE": "来源重复（已拦截）",
    "TITLE_DUPLICATE": "标题重复（已取消自动发布）",
    "ERROR": "处理失败",
}

TERMINAL_STATUSES = {"REJECTED", "ALREADY_PUBLISHED", "DRAFT_CREATED", "PUBLISHED", "SOURCE_DUPLICATE", "TITLE_DUPLICATE"}


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value or "", value or "未知")
