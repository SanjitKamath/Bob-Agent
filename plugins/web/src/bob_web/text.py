"""Turning a page into the text a person would read, and nothing a person could not see."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Comment

_DROP = (
    "script",
    "style",
    "noscript",
    "svg",
    "template",
    "iframe",
    "form",
    "nav",
    "footer",
    "header",
    "aside",
    "canvas",
    "button",
    "select",
)
_HIDDEN = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(?![.\d])", re.I
)
_SPACE = re.compile(r"[ \t\r\f\v ]+")
_LINES = re.compile(r"\n{3,}")


def page_text(html: str, limit: int) -> tuple[str, str, bool]:
    """(title, text, was_cut). Hidden elements, comments and scripts are dropped: those are
    where text written to steer a model, rather than to be read, is usually put."""
    soup = BeautifulSoup(html, "html.parser")
    title = " ".join(soup.title.get_text(" ").split()) if soup.title else ""
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(_DROP):
        tag.decompose()
    for tag in soup.find_all(True):
        style = tag.get("style") or ""
        if tag.has_attr("hidden") or tag.get("aria-hidden") == "true" or _HIDDEN.search(style):
            tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    text = root.get_text("\n")
    text = "\n".join(_SPACE.sub(" ", line).strip() for line in text.splitlines())
    text = _LINES.sub("\n\n", text).strip()
    if len(text) > limit:
        cut = text[:limit].rsplit("\n", 1)[0] if "\n" in text[:limit] else text[:limit]
        return title, cut.rstrip(), True
    return title, text, False


def plain(text: str, limit: int) -> tuple[str, bool]:
    text = _LINES.sub("\n\n", text.strip())
    return (text[:limit].rstrip(), True) if len(text) > limit else (text, False)


def real_link(href: str) -> str:
    """DuckDuckGo wraps each result in a redirect; the site itself is in the uddg parameter."""
    address = "https:" + href if href.startswith("//") else href
    query = parse_qs(urlsplit(address).query)
    return query["uddg"][0] if "uddg" in query else address
