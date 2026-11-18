"""MCP server for the Documents plugin. Every tool is read-only."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
from bob_sdk.paths import ReadGuard
from bob_sdk.runtime import PluginContext, ToolError, create_server, manifest_for, run

from . import extract

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)
_ctx: PluginContext | None = None

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
WEAK_OCR_CHARS = 20
VISION_PROMPT = "Transcribe all text in this image exactly. Output the text only."


def context() -> PluginContext:
    global _ctx
    if _ctx is None:
        _ctx = PluginContext.load(MANIFEST)
    return _ctx


def _path(path: str, suffixes: set[str]) -> Path:
    file = ReadGuard(context().setting("read_roots")).check(path)
    if not file.is_file():
        raise ToolError(f"{file} is not a file")
    if file.suffix.lower() not in suffixes:
        raise ToolError(f"expected one of {sorted(suffixes)}, got {file.suffix or 'no extension'}")
    return file


async def _vision_ocr(image: Path) -> str:
    """Fallback for scans Tesseract reads badly: the local vision model."""
    ctx = context()
    if "vision" not in ctx.models:
        return ""
    spec = ctx.models["vision"]
    payload = {
        "model": spec.model,
        "stream": False,
        "options": {"num_ctx": spec.num_ctx},
        "messages": [
            {
                "role": "user",
                "content": VISION_PROMPT,
                "images": [base64.b64encode(image.read_bytes()).decode()],
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=180) as http:
            response = await http.post(f"{ctx.ollama_url}/api/chat", json=payload)
            response.raise_for_status()
    except httpx.HTTPError:
        return ""
    return str(response.json().get("message", {}).get("content", "")).strip()


@server.tool()
def pdf_fields(path: str) -> dict[str, Any]:
    """List the form fields of a PDF: name, label, type, options and page."""
    return extract.pdf_fields(_path(path, {".pdf"}))


@server.tool()
def docx_parse(path: str) -> dict[str, Any]:
    """Parse a Word document into sections, tables, placeholders and fill-in fields."""
    return extract.docx_parse(_path(path, {".docx"}))


@server.tool()
async def ocr(path: str) -> dict[str, Any]:
    """Read the text in an image (Tesseract, then the local vision model for weak scans)."""
    image = _path(path, IMAGE_SUFFIXES)
    text, engine = extract.tesseract(image), "tesseract"
    if len(text) < WEAK_OCR_CHARS:
        vision = await _vision_ocr(image)
        if len(vision) > len(text):
            text, engine = vision, "vision"
    if not text:
        raise ToolError("no text could be read from the image")
    return {"path": str(image), "engine": engine, "text": text}


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
