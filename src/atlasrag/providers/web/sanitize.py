"""Bounded, deterministic HTML-to-text extraction and sanitization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "object",
        "embed",
        "form",
        "input",
        "button",
        "nav",
        "footer",
        "header",
        "aside",
        "canvas",
        "video",
        "audio",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "blockquote",
        "table",
    }
)
_HIDDEN_STYLE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[;\s]|$))",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class SanitizedPage:
    title: str
    content: str


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._hidden_stack: list[str] = []
        self._title_parts: list[str] = []
        self._heading_parts: list[str] = []
        self._body_parts: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._saw_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        attribute_map = {key.casefold(): (value or "") for key, value in attrs}
        hidden = (
            "hidden" in attribute_map
            or attribute_map.get("aria-hidden", "").casefold() == "true"
        )
        if _HIDDEN_STYLE.search(attribute_map.get("style", "")):
            hidden = True
        if hidden:
            self._hidden_stack.append(tag)
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1" and not self._saw_h1:
            self._in_h1 = True
        if tag in _BLOCK_TAGS:
            self._body_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _SKIP_TAGS:
            if tag in self._skip_stack:
                self._skip_stack.remove(tag)
            return
        if tag in self._hidden_stack:
            self._hidden_stack.remove(tag)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._saw_h1 = True
        if tag in _BLOCK_TAGS:
            self._body_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_stack or self._hidden_stack:
            return
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_h1:
            self._heading_parts.append(data)
        else:
            self._body_parts.append(data)

    def finish(self) -> SanitizedPage:
        title = _clean(" ".join(self._title_parts) or " ".join(self._heading_parts))
        body = _clean("".join(self._body_parts))
        if not body:
            body = title
        return SanitizedPage(title=title, content=body)


def _clean(value: str) -> str:
    value = _CONTROL.sub("", value)
    lines = []
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def sanitize_html(raw: str, *, max_chars: int) -> SanitizedPage:
    if not isinstance(raw, str):
        raise TypeError("HTML input must be text")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be positive")
    parser = _Extractor()
    parser.feed(raw)
    parser.close()
    page = parser.finish()
    content = page.content[:max_chars].rstrip()
    title = (page.title or content.split("\n", 1)[0])[:500].strip()
    if not content:
        raise ValueError("sanitized HTML has no usable text")
    if not title:
        title = content[:100].strip()
    return SanitizedPage(title=title, content=content)


def sanitize_text(raw: str, *, max_chars: int) -> str:
    """Bound provider snippet text without accepting markup/control content."""
    if not isinstance(raw, str):
        raise TypeError("snippet input must be text")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be positive")
    try:
        cleaned = sanitize_html(raw, max_chars=max_chars).content
    except ValueError:
        raise ValueError("snippet has no usable text") from None
    if not cleaned:
        raise ValueError("snippet has no usable text")
    return cleaned
