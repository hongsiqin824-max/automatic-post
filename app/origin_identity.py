"""Stable upstream article identities for exact source-level deduplication."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit


_KBS_NCD_RE = re.compile(r"[0-9]+")


def source_origin_key(source: str, source_url: str) -> str | None:
    """Return a stable identity only when the source exposes an exact ID."""

    if str(source or "").strip().lower() != "kbs":
        return None

    try:
        parsed = urlsplit(str(source_url or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if (parsed.hostname or "").lower() != "news.kbs.co.kr":
        return None

    values = parse_qs(parsed.query, keep_blank_values=True).get("ncd", [])
    if len(values) != 1 or _KBS_NCD_RE.fullmatch(values[0]) is None:
        return None
    number = int(values[0])
    if number <= 0:
        return None
    return f"kbs:ncd:{number}"
