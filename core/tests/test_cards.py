from __future__ import annotations

from bob_sdk.manifest import parse_manifest
from bobd.cards import fill, lookup, render

MANIFEST = parse_manifest(
    {
        "id": "mail", "name": "Mail", "version": "1", "mcp": "python -m x",
        "tools": {"summary": "read", "triage": "draft", "send": "external"},
        "widget_card": {
            "title": "Mail", "value": "counts.urgent", "status": "{needs_reply} need a reply",
            "buttons": [{"label": "Triage now", "tool": "mail.triage"}, {"label": "Send", "tool": "send"}],
        },
    }
)  # fmt: skip


def test_lookup_and_fill():
    data = {
        "counts": {"urgent": 3, "needs_reply": 2},
        "status": "Last triage 09:00",
        "nested": {"a": 1},
    }
    assert lookup(data, "counts.urgent") == 3 and lookup(data, "counts.none") is None
    assert lookup(data, "status.deeper") is None
    assert fill("{needs_reply} need a reply, {urgent} urgent", data) == "2 need a reply, 3 urgent"
    assert fill("{status} / {missing} / {nested}", data) == "Last triage 09:00 / – / –"


def test_render_card_and_button_tiers():
    card = render(MANIFEST, MANIFEST.widget_card, {"counts": {"urgent": 3, "needs_reply": 2}})
    assert (card["value"], card["status"], card["refresh_seconds"]) == (3, "2 need a reply", None)
    assert [(b["tool"], b["asks_first"]) for b in card["buttons"]] == [
        ("triage", False),
        ("send", True),
    ]
    empty = render(MANIFEST, MANIFEST.widget_card, {})
    assert empty["value"] is None and empty["status"] == "– need a reply"
    assert render(MANIFEST, MANIFEST.widget_card, {"counts": {"urgent": [1]}})["value"] is None


def test_a_card_lists_its_items_and_ignores_malformed_ones():
    from bobd.cards import MAX_ITEMS, items_of

    data = {"items": [{"title": "  File  taxes ", "detail": "due today", "flag": "today"},
                      {"title": ""}, "junk", {"detail": "no title"}]}  # fmt: skip
    assert items_of(data) == [{"title": "File taxes", "detail": "due today", "flag": "today"}]
    assert items_of({"counts": {}}) == [] and items_of(None) == []
    assert len(items_of({"items": [{"title": str(i)} for i in range(MAX_ITEMS + 50)]})) == MAX_ITEMS
