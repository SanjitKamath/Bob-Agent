"""The helper's main loop.

python -c "from bob_voice.cli import main; main()"        the protocol on stdin and stdout
... --wav FILE [LANGUAGE] [MODEL]                           transcribe a recording
... --probe                                                 say whether this Mac can run it
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import wave
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from . import protocol
from .models import ModelError, ensure, language_code, pick
from .protocol import AUDIO, COMMAND, SAMPLE_RATE, Writer, frame, read_frames
from .segmenter import Action, Tracker, to_float

Emit = Callable[[dict[str, Any]], None]
#: Turns audio into words. Tests give it a stand-in.
Transcribe = Callable[[np.ndarray], str]
#: Loads the engine for a start command and returns its transcribe function.
Load = Callable[[dict[str, Any], Callable[[float], None]], Transcribe]

_STOP = object()
_CANCEL = object()


def load_whisper(command: dict[str, Any], progress: Callable[[float], None]) -> Transcribe:
    """The real engine: find (or fetch) the model, load it, return its transcribe function."""
    locale = str(command.get("locale") or "en-US")
    model = pick(locale, str(command.get("model") or "base"))
    models_dir = Path(os.environ.get("BOB_VOICE_MODELS") or Path.home() / ".bob-voice")
    bundled = os.environ.get("BOB_VOICE_BUNDLED")
    path = ensure(model, models_dir, Path(bundled) if bundled else None, progress)
    from .engine import Whisper

    vocabulary = [str(item) for item in command.get("vocabulary") or [] if str(item).strip()]
    return Whisper(path, language_code(locale), vocabulary).transcribe


def serve(frames: Iterable[tuple[int, bytes]], emit: Emit, load: Load = load_whisper) -> int:
    """Run one session. Returns the exit code."""
    work: queue.Queue[Any] = queue.Queue()
    worker: threading.Thread | None = None

    def start(command: dict[str, Any]) -> bool:
        nonlocal worker
        emit({"type": "progress", "what": "loading", "fraction": 0.0})
        try:
            transcribe = load(
                command, lambda f: emit({"type": "progress", "what": "downloading", "fraction": f})
            )
        except ModelError as exc:
            emit({"type": "error", "code": "failed", "message": str(exc)})
            return False
        except Exception as exc:  # noqa: BLE001 - whatever the engine raises is reported, not fatal
            emit(
                {
                    "type": "error",
                    "code": "failed",
                    "message": f"The speech engine would not start: {exc}",
                }
            )
            return False
        worker = threading.Thread(target=_work, args=(work, transcribe, emit), daemon=True)
        worker.start()
        emit(
            {"type": "ready", "engine": "whisper", "locale": str(command.get("locale") or "en-US")}
        )
        return True

    started = False
    for kind, payload in frames:
        if kind == COMMAND:
            try:
                command = json.loads(payload)
            except ValueError:
                continue
            name = command.get("cmd")
            if name == "start" and not started:
                started = start(command)
                if not started:
                    return 1
            elif name in ("stop", "cancel"):
                if started and worker is not None:
                    work.put(_STOP if name == "stop" else _CANCEL)
                    worker.join()
                emit({"type": "closed"})
                return 0
        elif kind == AUDIO and started:
            work.put(payload)
    if started and worker is not None:
        work.put(_CANCEL)
        worker.join()
    return 0


def _work(work: queue.Queue[Any], transcribe: Transcribe, emit: Emit) -> None:
    tracker = Tracker()
    last_partial = ""

    def say(action: Action) -> None:
        nonlocal last_partial
        text = transcribe(action.audio)
        if action.kind == "partial":
            if text and text != last_partial:
                last_partial = text
                emit({"type": "partial", "text": text})
        else:
            last_partial = ""
            if text:
                emit({"type": "final", "text": text})

    while True:
        batch = [work.get()]
        # Decoding can be slower than the audio arrives: take everything waiting at once.
        while True:
            try:
                batch.append(work.get_nowait())
            except queue.Empty:
                break
        if _CANCEL in batch:
            return
        stopping = _STOP in batch
        actions: list[Action] = []
        for item in batch:
            if isinstance(item, bytes):
                actions += tracker.push(to_float(item))
        if stopping:
            actions += tracker.flush()
        # Only the newest partial matters, and none that a final has already replaced.
        last_final = max((i for i, a in enumerate(actions) if a.kind == "final"), default=-1)
        newest_partial = max((i for i, a in enumerate(actions) if a.kind == "partial"), default=-1)
        for index, action in enumerate(actions):
            if action.kind == "final" or (index == newest_partial and index > last_final):
                say(action)
        if stopping:
            return


def read_wav(path: str) -> np.ndarray:
    """A 16-bit WAV file as 16 kHz mono floats (resampled simply if it is another rate)."""
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("use a 16-bit WAV file")
        channels, rate = wav.getnchannels(), wav.getframerate()
        samples = (
            np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
            / 32768.0
        )
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        target = int(samples.size * SAMPLE_RATE / rate)
        samples = np.interp(
            np.linspace(0, samples.size - 1, target), np.arange(samples.size), samples
        ).astype(np.float32)
    return samples


def wav_frames(path: str, locale: str, model: str) -> Iterable[tuple[int, bytes]]:
    audio = read_wav(path)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    yield COMMAND, json.dumps({"cmd": "start", "locale": locale, "model": model}).encode()
    for offset in range(0, len(pcm), 1280):
        yield AUDIO, pcm[offset : offset + 1280]
    yield COMMAND, json.dumps({"cmd": "stop"}).encode()


def probe() -> dict[str, Any]:
    try:
        import pywhispercpp  # noqa: F401
    except ImportError as exc:
        return {"ok": False, "detail": f"pywhispercpp is missing: {exc}"}
    return {"ok": True, "detail": "whisper.cpp is available"}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    writer = Writer()
    if "--probe" in args:
        writer(probe())
        raise SystemExit(0)
    if "--wav" in args:
        rest = args[args.index("--wav") + 1 :]
        if not rest:
            raise SystemExit("usage: --wav FILE [LANGUAGE] [MODEL]")
        locale = rest[1] if len(rest) > 1 else "en-US"
        model = rest[2] if len(rest) > 2 else "base"
        raise SystemExit(serve(wav_frames(rest[0], locale, model), writer))
    raise SystemExit(serve(read_frames(sys.stdin.buffer), writer))


__all__ = ["frame", "main", "protocol", "serve"]
