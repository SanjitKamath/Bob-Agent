"""Turning documents into field lists and structured text.

``pdf_fields`` is the first step of form authoring: a PDF form becomes a list
of fields (name, label, type, options) that a draft can be checked against.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from bob_sdk.runtime import ToolError

MIN_TEXT_CHARS = 40
MAX_FIELDS = 600

# "Label: ______", "Label ........", "Label [   ]"
_BLANK = re.compile(r"^\s*(?P<label>[^:_.\[\]]{2,80}?)\s*[:\-]?\s*(?:_{3,}|\.{5,}|\[\s*\])")
_CHECKBOX = re.compile(r"^\s*(?:☐|□|\[\s?\]|\(\s?\))\s*(?P<label>.{2,80})$")
_PLACEHOLDER = re.compile(
    r"\{\{\s*([\w.\- ]+?)\s*\}\}|\$\{\s*([\w.\-]+)\s*\}|<<\s*([\w.\- ]+?)\s*>>"
)
_SLUG = re.compile(r"[^a-z0-9]+")

_WIDGET_TYPES = {
    "Text": "text", "CheckBox": "checkbox", "RadioButton": "radio", "ComboBox": "dropdown",
    "ListBox": "list", "Signature": "signature", "Button": "button",
}  # fmt: skip


def slug(label: str) -> str:
    return _SLUG.sub("_", label.lower()).strip("_")[:60] or "field"


def tesseract(image: Path, *, timeout_s: float = 120.0) -> str:
    binary = shutil.which("tesseract")
    if binary is None:
        return ""
    try:
        done = subprocess.run(  # noqa: S603 - fixed binary; the path is one argument
            [binary, str(image), "stdout"],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def fields_from_text(text: str, page: int) -> list[dict[str, Any]]:
    """Find fill-in blanks and checkboxes in a page's text."""
    fields: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _CHECKBOX.match(line)
        if match:
            label = match.group("label").strip()
            fields.append({"name": slug(label), "label": label, "type": "checkbox", "page": page})
            continue
        match = _BLANK.match(line)
        if match:
            label = match.group("label").strip(" :-")
            if len(label) >= 2:
                fields.append({"name": slug(label), "label": label, "type": "text", "page": page})
    return fields


def _text_lines(page, widget_rects: list[Any]) -> list[tuple[float, float, float, float, str]]:
    """The page's text lines, without the values shown inside form widgets."""
    lines: list[tuple[float, float, float, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = " ".join("".join(span["text"] for span in line["spans"]).split())
            x0, y0, x1, y1 = line["bbox"]
            centre = ((x0 + x1) / 2, (y0 + y1) / 2)
            inside = any(
                r.x0 <= centre[0] <= r.x1 and r.y0 <= centre[1] <= r.y1 for r in widget_rects
            )
            if text and len(text) <= 120 and not inside:
                lines.append((x0, y0, x1, y1, text))
    return lines


def _nearest_label(lines: list[tuple[float, float, float, float, str]], rect) -> str:
    """The words just left of, or just above, a form widget."""
    best: tuple[float, str] | None = None
    for x0, y0, x1, y1, text in lines:
        same_row = y0 < rect.y1 and y1 > rect.y0 and x1 <= rect.x0 + 2
        above = y1 <= rect.y0 + 2 and x0 < rect.x1 and x1 > rect.x0
        if same_row:
            distance = rect.x0 - x1
        elif above:
            distance = (rect.y0 - y1) + 1000  # prefer a label on the same row
        else:
            continue
        if distance >= 0 and (best is None or distance < best[0]):
            best = (distance, text)
    return best[1].rstrip(":").strip() if best else ""


def pdf_fields(path: Path) -> dict[str, Any]:
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises its own error types
        raise ToolError(f"cannot open PDF: {exc}") from exc
    with document:
        if document.needs_pass:
            raise ToolError("the PDF is password-protected")
        fields: list[dict[str, Any]] = []
        for number, page in enumerate(document, start=1):
            widgets = list(page.widgets() or [])
            lines = _text_lines(page, [widget.rect for widget in widgets]) if widgets else []
            for widget in widgets:
                kind = _WIDGET_TYPES.get(widget.field_type_string, "text")
                if kind == "button":
                    continue
                label = (widget.field_label or "").strip() or _nearest_label(lines, widget.rect)
                name = (widget.field_name or "").strip() or slug(label)
                field: dict[str, Any] = {
                    "name": name,
                    "label": label or name,
                    "type": kind,
                    "page": number,
                }
                options = [
                    str(o[0] if isinstance(o, list | tuple) else o)
                    for o in widget.choice_values or []
                ]
                if options:
                    field["options"] = options
                if widget.field_value not in (None, "", "Off"):
                    field["value"] = str(widget.field_value)
                if kind == "text" and widget.text_maxlen:
                    field["max_length"] = int(widget.text_maxlen)
                fields.append(field)
        source = "widgets"
        if not fields:
            # A flat PDF: look for blanks in the text layer, then in OCR output.
            source = "text"
            for number, page in enumerate(document, start=1):
                text = page.get_text("text")
                if len(text.strip()) < MIN_TEXT_CHARS:
                    with tempfile.TemporaryDirectory(prefix="bob-ocr-") as folder:
                        image = Path(folder) / "page.png"
                        page.get_pixmap(dpi=200).save(image)
                        text = tesseract(image)
                    if text:
                        source = "ocr"
                fields.extend(fields_from_text(text, number))
        pages = document.page_count
    # Radio groups appear once per button; fold them into one field with options.
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for field in fields:
        key = (field["name"], field["type"])
        if field["type"] == "radio" and key in merged:
            continue
        merged.setdefault(key, field)
    result = list(merged.values())[:MAX_FIELDS]
    return {
        "path": str(path),
        "pages": pages,
        "source": source,
        "count": len(result),
        "fields": result,
        "truncated": len(merged) > MAX_FIELDS,
    }


def docx_parse(path: Path) -> dict[str, Any]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # python-docx raises package and zip errors
        raise ToolError(f"cannot open DOCX: {exc}") from exc
    sections: list[dict[str, Any]] = [{"heading": "", "level": 0, "text": ""}]
    tables: list[dict[str, Any]] = []
    placeholders: list[str] = []

    def note_placeholders(text: str) -> None:
        for match in _PLACEHOLDER.finditer(text):
            name = next(group for group in match.groups() if group)
            if name not in placeholders:
                placeholders.append(name)

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            note_placeholders(text)
            style = (block.style.name or "") if block.style is not None else ""
            if style.startswith("Heading") or style == "Title":
                digits = "".join(ch for ch in style if ch.isdigit())
                sections.append({"heading": text, "level": int(digits or 1), "text": ""})
            else:
                current = sections[-1]
                current["text"] = f"{current['text']}\n{text}".strip()
        elif isinstance(block, Table):
            rows = [[cell.text.strip() for cell in row.cells] for row in block.rows]
            rows = [row for row in rows if any(row)]
            for row in rows:
                for cell in row:
                    note_placeholders(cell)
            if rows:
                tables.append(
                    {"under": sections[-1]["heading"], "header": rows[0], "rows": rows[1:200]}
                )
    sections = [s for s in sections if s["heading"] or s["text"]]
    for section in sections:
        section["text"] = section["text"][:6000]
    return {
        "path": str(path),
        "sections": sections[:300],
        "tables": tables[:100],
        "placeholders": placeholders,
        "fields": [field for section in sections for field in fields_from_text(section["text"], 1)][
            :MAX_FIELDS
        ],
    }
