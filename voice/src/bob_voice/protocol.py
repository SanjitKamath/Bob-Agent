"""The wire format shared with the app (app/shared/voice.ts).

stdin  frames: one byte of kind (1 = JSON command, 2 = PCM audio), four bytes of length
       (little-endian), then the payload. Audio is 16 kHz, mono, signed 16-bit little-endian.
stdout one JSON object per line: ready, progress, partial, final, error, closed.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
from collections.abc import Iterator
from typing import Any, BinaryIO

COMMAND = 1
AUDIO = 2
MAX_PAYLOAD = 1 << 20
SAMPLE_RATE = 16000


def read_frames(stream: BinaryIO) -> Iterator[tuple[int, bytes]]:
    """Frames until the stream ends or stops making sense."""
    while True:
        header = _exactly(stream, 5)
        if header is None:
            return
        kind, length = header[0], struct.unpack("<I", header[1:])[0]
        if length > MAX_PAYLOAD:
            return  # a broken stream: stop listening to it
        payload = _exactly(stream, length) if length else b""
        if payload is None:
            return
        yield kind, payload


def _exactly(stream: BinaryIO, count: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def frame(kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + struct.pack("<I", len(payload)) + payload


class Writer:
    """Writes events as JSON lines, one at a time, from any thread."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
