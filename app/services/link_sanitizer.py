"""Remove linked text while preserving article images and non-link content.

The material API can return promotional and inline text wrapped in ``<a>``
elements.  Published articles must not retain either the clickable target or
its linked text.  Images wrapped by anchors remain article media, so only
their clickable wrapper is removed.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


_ANCHOR_START = re.compile(r"<\s*a(?:\s|/?>)", re.IGNORECASE)
_RESIDUAL_ANCHOR = re.compile(r"<\s*/?\s*a\b[^>]*(?:>|$)", re.IGNORECASE)


class _ClickableLinkRemover(HTMLParser):
    """Drop anchor text and nested markup while retaining linked images."""

    def __init__(self, *, drop_linked_content: bool = False) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._link_depth = 0
        self._drop_linked_content = drop_linked_content

    def _raw_start_tag(self) -> str:
        raw = self.get_starttag_text()
        return raw if raw is not None else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if self._link_depth:
            if lowered == "a":
                self._link_depth += 1
            elif lowered == "img":
                # A linked image is still article media; only its clickable
                # wrapper should disappear.
                self.parts.append(self._raw_start_tag())
            elif not self._drop_linked_content:
                self.parts.append(self._raw_start_tag())
            return
        if lowered == "a":
            self._link_depth = 1
            return
        self.parts.append(self._raw_start_tag())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if self._link_depth:
            if lowered == "img":
                self.parts.append(self._raw_start_tag())
            elif not self._drop_linked_content:
                self.parts.append(self._raw_start_tag())
            return
        if lowered == "a":
            return
        self.parts.append(self._raw_start_tag())

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._link_depth:
            if lowered == "a":
                self._link_depth -= 1
            elif not self._drop_linked_content:
                self.parts.append(f"</{tag}>")
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if not self._link_depth or not self._drop_linked_content:
            self.parts.append(f"<?{data}>")


def remove_clickable_links(body_html: str | None) -> str:
    """Remove closed ``<a>`` elements together with their linked text.

    The function is intentionally idempotent.  Non-link markup and text are
    retained, and images inside links are preserved without the clickable
    wrapper. Invalid or incomplete HTML is handled by :class:`HTMLParser`
    without raising. When an anchor is not closed, its boundary is ambiguous,
    so the fail-safe removes the link tag but retains the remaining text.
    """

    body = str(body_html or "")
    if _ANCHOR_START.search(body) is None:
        return body
    parser = _ClickableLinkRemover(drop_linked_content=True)
    parser.feed(body)
    parser.close()
    # An unclosed anchor would otherwise make the parser drop the rest of an
    # article. In that malformed case, unwrap anchors and retain their content
    # so no clickable target remains and valid article text is not lost.
    if parser._link_depth:
        fallback = _ClickableLinkRemover(drop_linked_content=False)
        fallback.feed(body)
        fallback.close()
        return _RESIDUAL_ANCHOR.sub("", "".join(fallback.parts))
    cleaned = "".join(parser.parts)
    # HTMLParser treats an incomplete start tag as plain data. Remove that
    # residual fragment as a final fail-safe so malformed upstream HTML cannot
    # leave a clickable href in the submitted body.
    return _RESIDUAL_ANCHOR.sub("", cleaned)
