"""Prepare article HTML for the DQD create-article endpoint."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser


DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES = frozenset(
    {
        "data-image-meta",
        "data-attachment-id",
        "data-comments-opened",
        "data-image-caption",
        "data-image-description",
        "data-image-title",
        "data-large-file",
        "data-orig-file",
        "data-orig-size",
        "data-permalink",
    }
)

_SAFE_IMAGE_ATTRIBUTES = frozenset(
    {
        "align",
        "alt",
        "border",
        "class",
        "crossorigin",
        "decoding",
        "height",
        "hspace",
        "id",
        "ismap",
        "loading",
        "referrerpolicy",
        "sizes",
        "src",
        "srcset",
        "style",
        "title",
        "usemap",
        "vspace",
        "width",
    }
)
_VALID_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_.:-]*$")
_IMG_START = re.compile(r"<\s*img\b", re.IGNORECASE)
_UNSUPPORTED_ATTRIBUTE_NAME = re.compile(
    r"\b(?:"
    + "|".join(re.escape(name) for name in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES)
    + r")\b",
    re.IGNORECASE,
)


class DqdPublishHtmlError(ValueError):
    """Raised when submission-only HTML cleanup cannot preserve the article."""


class _AttributeFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found = False
        self.unparsed_text: list[str] = []
        self._raw_text_depth = 0

    def _inspect(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img" and any(
            name.lower() in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES for name, _ in attrs
        ):
            self.found = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)
        if tag.lower() in {"script", "style"}:
            self._raw_text_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._raw_text_depth:
            self._raw_text_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._raw_text_depth:
            self.unparsed_text.append(data)


def _safe_image_attribute(name: str, value: str | None) -> bool:
    lowered = name.lower()
    return (
        lowered in _SAFE_IMAGE_ATTRIBUTES
        or lowered.startswith("data-")
        or lowered.startswith("aria-")
    ) and bool(_VALID_ATTRIBUTE_NAME.fullmatch(name)) and (
        value is not None or lowered == "ismap"
    )


def _has_unparsed_unsupported_image_attribute(text: str) -> bool:
    """Detect a dangerous attribute in an img fragment HTMLParser rejected."""

    return any(
        _UNSUPPORTED_ATTRIBUTE_NAME.search(text[match.end():]) is not None
        for match in _IMG_START.finditer(text)
    )


class _DqdHtmlCleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def _append_start_tag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        has_unsupported_metadata = tag.lower() == "img" and any(
            name.lower() in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES for name, _ in attrs
        )
        if not has_unsupported_metadata:
            raw_tag = self.get_starttag_text()
            if raw_tag is None:
                raise DqdPublishHtmlError("无法读取正文原始标签")
            self.parts.append(raw_tag)
            return

        self.parts.append(f"<{tag}")
        retained_names: set[str] = set()
        for name, value in attrs:
            lowered = name.lower()
            if lowered in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES:
                continue
            # A-Leagues occasionally embeds an unescaped apostrophe inside a
            # single-quoted JSON value. HTMLParser then exposes JSON fragments
            # as fake attributes; keep only real image and custom data fields.
            if not _safe_image_attribute(name, value):
                continue
            if lowered in retained_names:
                raise DqdPublishHtmlError(f"清理后的图片包含重复属性：{lowered}")
            retained_names.add(lowered)
            self.parts.append(f" {name}")
            if value is not None:
                self.parts.append(f'="{html.escape(value, quote=True)}"')
        self.parts.append(" />" if self_closing else ">")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_start_tag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_start_tag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.parts.append(f"<![{data}]>")


class _ContentSignatureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.image_sources: list[str | None] = []
        self.image_attributes: list[tuple[tuple[str, str | None], ...]] = []
        self.link_targets: list[str | None] = []
        self.unsupported_attributes: list[str] = []
        self.invalid_attributes: list[str] = []
        self.structure: list[tuple[object, ...]] = []

    def _inspect(
        self,
        kind: str,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered_tag = tag.lower()
        self.structure.append((kind, lowered_tag))
        self.invalid_attributes.extend(
            name for name, _ in attrs if not _VALID_ATTRIBUTE_NAME.fullmatch(name)
        )
        if lowered_tag == "img":
            self.image_sources.append(
                next((value for name, value in attrs if name.lower() == "src"), None)
            )
            self.image_attributes.append(
                tuple(
                    (name.lower(), value)
                    for name, value in attrs
                    if _safe_image_attribute(name, value)
                    and name.lower() not in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES
                )
            )
            self.unsupported_attributes.extend(
                name.lower()
                for name, _ in attrs
                if name.lower() in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES
            )
        if lowered_tag == "a":
            self.link_targets.append(
                next((value for name, value in attrs if name.lower() == "href"), None)
            )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect("start", tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect("startend", tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self.structure.append(("end", tag.lower()))

    def handle_comment(self, data: str) -> None:
        self.structure.append(("comment", data))

    def handle_decl(self, decl: str) -> None:
        self.structure.append(("decl", decl))

    def handle_pi(self, data: str) -> None:
        self.structure.append(("pi", data))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _feed(parser: HTMLParser, body: str) -> None:
    try:
        parser.feed(body)
        parser.close()
    except DqdPublishHtmlError:
        raise
    except (AssertionError, TypeError, ValueError) as exc:
        raise DqdPublishHtmlError(f"正文 HTML 无法解析：{exc}") from exc


def _signature(body: str) -> _ContentSignatureParser:
    parser = _ContentSignatureParser()
    _feed(parser, body)
    return parser


def sanitize_dqd_publish_html(body_html: str | None) -> str:
    """Remove only DQD-incompatible WordPress metadata before submission.

    HTML without an exact unsupported attribute is returned byte-for-byte.
    The stored article body is never changed by this function.
    """

    body = str(body_html or "")
    lowered = body.lower()
    if not any(name in lowered for name in DQD_UNSUPPORTED_WORDPRESS_ATTRIBUTES):
        return body

    finder = _AttributeFinder()
    _feed(finder, body)
    if not finder.found:
        if _has_unparsed_unsupported_image_attribute("".join(finder.unparsed_text)):
            raise DqdPublishHtmlError("正文包含无法安全解析的 WordPress 图片属性")
        return body

    before = _signature(body)
    cleaner = _DqdHtmlCleaner()
    _feed(cleaner, body)
    cleaned = "".join(cleaner.parts)
    after = _signature(cleaned)

    if not cleaned.strip():
        raise DqdPublishHtmlError("清理后的正文为空")
    if before.text != after.text:
        raise DqdPublishHtmlError("清理前后正文文字不一致")
    if before.image_sources != after.image_sources:
        raise DqdPublishHtmlError("清理前后图片数量、顺序或地址不一致")
    if before.image_attributes != after.image_attributes:
        raise DqdPublishHtmlError("清理前后图片保留属性不一致")
    if before.link_targets != after.link_targets:
        raise DqdPublishHtmlError("清理前后链接地址不一致")
    if before.structure != after.structure:
        raise DqdPublishHtmlError("清理前后标签结构不一致")
    if after.unsupported_attributes:
        raise DqdPublishHtmlError("正文仍包含懂球帝不支持的 WordPress 图片属性")
    if after.invalid_attributes:
        raise DqdPublishHtmlError("清理后的正文仍包含异常 HTML 属性")
    return cleaned
