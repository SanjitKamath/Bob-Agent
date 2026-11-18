"""Web search with no key: DuckDuckGo's plain HTML page, Wikipedia if that does not work."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
from bob_sdk.runtime import ToolError
from bs4 import BeautifulSoup

from .net import USER_AGENT
from .text import real_link

DDG = "https://html.duckduckgo.com/html/"
WIKI = "https://en.wikipedia.org/w/api.php"
_TAGS = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_duckduckgo(html: str, limit: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict[str, str]] = []
    for block in soup.select("div.result"):
        if "result--ad" in (block.get("class") or []):
            continue
        link = block.select_one("a.result__a")
        if link is None or not link.get("href"):
            continue
        url = real_link(str(link["href"]))
        if not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue
        snippet = block.select_one(".result__snippet")
        found.append(
            {
                "title": _clean(link.get_text(" ")),
                "url": url,
                "snippet": _clean(snippet.get_text(" ")) if snippet else "",
            }
        )
        if len(found) >= limit:
            break
    return found


def parse_wikipedia(data: dict[str, Any], limit: int) -> list[dict[str, str]]:
    found = []
    for item in (data.get("query") or {}).get("search") or []:
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        slug = title.replace(" ", "_")
        found.append(
            {
                "title": f"{title} - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/" + quote(slug, safe="_(),:'!*"),
                "snippet": _clean(_TAGS.sub("", str(item.get("snippet", "")))),
            }
        )
        if len(found) >= limit:
            break
    return found


def search(
    query: str, limit: int, client: httpx.Client | None = None
) -> tuple[str, list[dict[str, str]]]:
    """(provider, results). Raises ToolError with something worth showing when nothing works."""
    query = " ".join((query or "").split())
    if not query:
        raise ToolError("Say what to search for.")
    if len(query) > 300:
        raise ToolError("That search is too long.")
    own = client is None
    http = client or httpx.Client(timeout=12.0, follow_redirects=True, trust_env=False)
    problems: list[str] = []
    try:
        try:
            response = http.post(DDG, data={"q": query}, headers={"User-Agent": USER_AGENT})
            if response.status_code == 200:
                results = parse_duckduckgo(response.text, limit)
                if results:
                    return "DuckDuckGo", results
                problems.append("DuckDuckGo gave no results")
            else:
                problems.append(f"DuckDuckGo was not available (HTTP {response.status_code})")
        except httpx.HTTPError as exc:
            problems.append(f"DuckDuckGo could not be reached ({exc})")
        try:
            response = http.get(
                WIKI,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": limit,
                    "format": "json",
                },
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code == 200:
                results = parse_wikipedia(response.json(), limit)
                if results:
                    return "Wikipedia", results
                problems.append("Wikipedia had nothing either")
            else:
                problems.append(f"Wikipedia was not available (HTTP {response.status_code})")
        except (httpx.HTTPError, ValueError) as exc:
            problems.append(f"Wikipedia could not be reached ({exc})")
    finally:
        if own:
            http.close()
    raise ToolError(
        "Nothing could be found. " + "; ".join(problems) + ". Check the internet connection."
    )
