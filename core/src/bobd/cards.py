"""Rendering plugin widget cards: the big number, the status line and the buttons."""

from __future__ import annotations

import re
from typing import Any

from bob_sdk.manifest import Manifest, WidgetCard

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w.]*)\}")


def lookup(data: Any, path: str) -> Any:
    """Follow a dotted path such as ``counts.urgent`` into nested dictionaries."""
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def fill(template: str, data: Any) -> str:
    """Fill ``{name}`` placeholders from the result's counts, then from the result itself."""
    counts = data.get("counts", {}) if isinstance(data, dict) else {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = lookup(counts, name)
        if value is None:
            value = lookup(data, name)
        return "–" if value is None or isinstance(value, dict | list) else str(value)

    return _PLACEHOLDER.sub(replace, template)


MAX_ITEMS = 500


def items_of(data: Any) -> list[dict[str, str]]:
    """The lines a card lists under its number: ``items`` in the source tool's result, each
    with a title, an optional detail (a due date, a sender) and an optional flag."""
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw[:MAX_ITEMS]:
        if not isinstance(entry, dict) or not str(entry.get("title") or "").strip():
            continue
        items.append(
            {
                "title": " ".join(str(entry["title"]).split())[:200],
                "detail": " ".join(str(entry.get("detail") or "").split())[:120],
                "flag": str(entry.get("flag") or "")[:20],
            }
        )
    return items


def render(manifest: Manifest, card: WidgetCard, data: Any) -> dict[str, Any]:
    """The card as the app draws it."""
    value = lookup(data, card.value) if card.value else None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        value = None
    status = fill(card.status, data) if card.status else ""
    if not status and isinstance(data, dict) and isinstance(data.get("status"), str):
        status = data["status"]
    if isinstance(data, dict) and data.get("set_up") is False:
        # A plugin with no account yet says what to do instead of showing empty numbers.
        value, status = None, str(data.get("status") or "Not set up")
    return {
        "plugin": manifest.id,
        "title": card.title,
        "value": value,
        "status": status,
        "error": "",
        "refresh_seconds": card.refresh_seconds,
        "items": items_of(data),
        "buttons": [
            {
                "label": button.label,
                "plugin": manifest.id,
                "tool": manifest.button_tool(button.tool),
                "args": button.args,
                # An external tool only ever opens its confirm card.
                "asks_first": manifest.tools[manifest.button_tool(button.tool)].needs_confirmation,
            }
            for button in card.buttons
        ],
    }
