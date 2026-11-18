"""Whisper, through whisper.cpp (the pywhispercpp wheel): one model kept loaded."""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

# Things Whisper writes for sound that is not speech: [BLANK_AUDIO], (music), a music note.
_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|[♪♫]+")
# Whisper's habit of inventing a closing line for near-silence.
_INVENTED = re.compile(
    r"^(thanks for watching|thank you for watching|thank you\.?|you\.?|bye\.?)$", re.I
)
# Quieter than this is not worth transcribing at all.
_SILENT = 0.004


def clean(text: str, audio_level: float = 1.0) -> str:
    """The words, without sound-effect tags, and nothing for audio that is only noise."""
    words = " ".join(_NOISE.sub(" ", text).split())
    if audio_level < _SILENT or (audio_level < 0.02 and _INVENTED.match(words)):
        return ""
    return words


class Whisper:
    def __init__(
        self, model_path: Path, language: str, vocabulary: list[str] | None = None
    ) -> None:
        from pywhispercpp.model import Model  # imported late: it loads native libraries

        options: dict[str, object] = {
            "n_threads": max(1, min(4, os.cpu_count() or 2)),
            "language": language,
            "no_context": True,
            "single_segment": True,
            "suppress_blank": True,
            "suppress_nst": True,
            "print_progress": False,
            "print_realtime": False,
            "print_timestamps": False,
            "print_special": False,
        }
        if vocabulary:
            # Names and terms the person uses: Whisper spells them the way they are given here.
            options["initial_prompt"] = ", ".join(vocabulary[:40])[:400]
        # whisper.cpp writes its own log to the error stream; none of it is wanted.
        self._model = Model(str(model_path), redirect_whispercpp_logs_to=None, **options)

    def transcribe(self, audio: np.ndarray) -> str:
        level = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        if level < _SILENT:
            return ""
        # Whisper wants at least a second of audio to work with; pad the end with silence.
        floor = 16000
        if audio.size < floor:
            audio = np.concatenate([audio, np.zeros(floor - audio.size, dtype=np.float32)])
        segments = self._model.transcribe(audio.astype(np.float32, copy=False))
        return clean(" ".join(segment.text for segment in segments), level)
