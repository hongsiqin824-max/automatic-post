"""来源抓取量的对比与异常判定。

纯计算，不碰数据库：期统计与快照读写都在 :mod:`app.repository`，这里只负责
「拿到两期数字之后怎么判断」。取数与判断分开后，阈值调整不需要重跑 SQL，
测试也不必准备一整个库。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

# 判定门槛按 24 小时一期定标。一天分成多期后每期的量天然变少，直接套用会让
# 小来源永远达不到门槛、彻底不报警，所以按期长等比缩放。
_BASELINE_PERIOD_HOURS = 24.0
_STOPPED_MIN_BASELINE = 10
_DROP_MIN_BASELINE = 5


def scale_threshold(baseline: int, period_hours: float) -> int:
    """Scale a 24h-calibrated article-count threshold to a shorter period.

    Never returns less than 1: a zero threshold would fire on every source
    that produced nothing, which is normal for a small source in an 8h window.
    """

    hours = max(0.0, float(period_hours))
    if hours <= 0 or hours >= _BASELINE_PERIOD_HOURS:
        return max(1, int(baseline))
    return max(1, round(int(baseline) * hours / _BASELINE_PERIOD_HOURS))


def compare_periods(
    current_stats: Sequence[Mapping[str, Any]],
    baseline_stats: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Pair each source's current-period counts with the same period yesterday.

    The baseline is yesterday's *same* period rather than yesterday as a whole:
    an 8h window compared against a 24h one is short by construction and would
    report every source as collapsing.

    When the baseline is missing entirely — the first period after switching the
    feature on, or after an outage — every source is marked ``no_baseline``
    instead of ``new``. Calling them all "new sources" would fill the first
    message with 30 bogus alerts; without a baseline there is simply nothing to
    compare against yet.
    """

    baseline_map = {
        str(item.get("source_code") or ""): item for item in baseline_stats
    }
    comparison: List[Dict[str, Any]] = []
    for current in current_stats:
        source_code = str(current.get("source_code") or "")
        baseline = baseline_map.get(source_code)
        if not baseline_map:
            change = {"rate": None, "display": "无基准", "type": "no_baseline"}
        elif baseline is None:
            change = {"rate": None, "display": "新增", "type": "new"}
        else:
            change = calculate_change(
                int(current.get("total_count") or 0),
                int(baseline.get("total_count") or 0),
            )
        comparison.append({
            "source_code": source_code,
            "source_name": str(current.get("source_name") or source_code),
            "current": dict(current),
            "baseline": dict(baseline) if baseline else {},
            "change": change,
        })
    return comparison


def calculate_change(current_count: int, baseline_count: int) -> Dict[str, Any]:
    """Return ``{rate, display, type}`` for one source's period-over-period move."""

    if baseline_count == 0:
        if current_count > 0:
            return {"rate": None, "display": "新增", "type": "new"}
        return {"rate": 0, "display": "0%", "type": "neutral"}

    rate = (current_count - baseline_count) / baseline_count
    display = f"{'+' if rate > 0 else ''}{rate * 100:.0f}%"
    if rate > 1.0:
        change_type = "surge"
    elif rate < -0.8:
        change_type = "critical_drop"
    elif rate < -0.5:
        change_type = "drop"
    elif abs(rate) < 0.1:
        change_type = "stable"
    else:
        change_type = "normal"
    return {"rate": rate, "display": display, "type": change_type}


def detect_anomalies(
    comparison: Sequence[Mapping[str, Any]],
    *,
    period_hours: float = _BASELINE_PERIOD_HOURS,
) -> Dict[str, List[Dict[str, Any]]]:
    """Split sources into critical / warning / info / normal buckets."""

    stopped_min = scale_threshold(_STOPPED_MIN_BASELINE, period_hours)
    drop_min = scale_threshold(_DROP_MIN_BASELINE, period_hours)

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "critical": [], "warning": [], "info": [], "normal": [],
    }
    for item in comparison:
        current_count = int((item.get("current") or {}).get("total_count") or 0)
        baseline_count = int((item.get("baseline") or {}).get("total_count") or 0)
        change = item.get("change") or {}
        change_type = change.get("type")
        rate = change.get("rate")

        message = None
        level = "normal"
        if current_count == 0 and baseline_count >= stopped_min:
            message = f"🚨 本期0篇，昨日同期{baseline_count}篇，疑似故障"
            level = "critical"
        elif baseline_count >= stopped_min and current_count > 0 and change_type == "critical_drop":
            message = f"🚨 文章数骤降{abs(float(rate) * 100):.0f}%"
            level = "critical"
        elif baseline_count >= drop_min and change_type == "drop":
            message = f"⚠️ 文章数下降{abs(float(rate) * 100):.0f}%"
            level = "warning"
        elif baseline_count >= drop_min and change_type == "surge":
            message = f"📈 文章数激增{float(rate) * 100:.0f}%（可能新增频道）"
            level = "info"
        elif change_type == "new" and current_count > 0:
            message = f"✨ 新增来源，本期{current_count}篇"
            level = "info"

        buckets[level].append({**item, "anomaly_message": message})
    return buckets


def generate_summary(comparison: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate one period across all sources for the report header."""

    total_articles = sum(
        int((item.get("current") or {}).get("total_count") or 0) for item in comparison
    )
    total_published = sum(
        int((item.get("current") or {}).get("published_count") or 0) for item in comparison
    )
    # 「待处理」= 已建草稿 + 待人工复核。项目里没有 DRAFT/REJECTED 这两个状态，
    # 早先按它们统计，结果这两行常年是 0。
    total_draft = sum(
        int((item.get("current") or {}).get("draft_count") or 0)
        + int((item.get("current") or {}).get("review_count") or 0)
        for item in comparison
    )
    baseline_total = sum(
        int((item.get("baseline") or {}).get("total_count") or 0) for item in comparison
    )

    if baseline_total > 0:
        total_rate = (total_articles - baseline_total) / baseline_total
        total_change = f"{'+' if total_rate > 0 else ''}{total_rate * 100:.0f}% vs 昨日同期"
    else:
        total_change = "无昨日同期数据"

    published_rate = (total_published / total_articles * 100) if total_articles else 0
    draft_rate = (total_draft / total_articles * 100) if total_articles else 0
    return {
        "total_articles": total_articles,
        "total_published": total_published,
        "total_draft": total_draft,
        "total_change": total_change,
        "published_rate": f"{published_rate:.0f}",
        "draft_rate": f"{draft_rate:.0f}",
        "active_sources": len(comparison),
    }
