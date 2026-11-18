"""Cuts a stream of audio into spoken messages and says when to transcribe.

Whisper reads a clip, not a stream. So the audio is cut into utterances by a simple level
detector, the clip so far is transcribed every so often while someone is talking (a ``partial``
that the next one replaces), and once more at the end of the utterance (a ``final``).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .protocol import SAMPLE_RATE


@dataclass(frozen=True)
class Settings:
    #: RMS level (0 to 1) above which audio counts as speech.
    speech_level: float = 0.012
    #: Silence after speech that ends an utterance.
    end_silence_s: float = 0.7
    #: How often, while someone is talking, the clip so far is transcribed again.
    partial_every_s: float = 0.9
    #: An utterance this long is settled and a new one begins.
    max_utterance_s: float = 25.0
    #: Less speech than this is a cough, not words.
    min_speech_s: float = 0.25
    #: Audio kept from just before speech starts, so the first word is not clipped.
    pre_roll_s: float = 0.3


@dataclass(frozen=True)
class Action:
    kind: str  # "partial" or "final"
    audio: np.ndarray  # float32, 16 kHz, mono


def to_float(pcm: bytes) -> np.ndarray:
    """16-bit little-endian samples as floats from -1 to 1."""
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class Tracker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._pre: deque[np.ndarray] = deque()
        self._pre_samples = 0
        self._reset()

    def _reset(self) -> None:
        self._frames: list[np.ndarray] = []
        self._active = False
        self._speech = 0.0
        self._silence = 0.0
        self._duration = 0.0
        self._since_partial = 0.0

    @property
    def active(self) -> bool:
        return self._active

    def push(self, samples: np.ndarray) -> list[Action]:
        """Feed one frame of audio; returns what to transcribe now, if anything."""
        if samples.size == 0:
            return []
        s = self.settings
        seconds = samples.size / SAMPLE_RATE
        level = float(np.sqrt(np.mean(np.square(samples))))
        voiced = level >= s.speech_level
        if not self._active:
            if not voiced:
                self._remember(samples)
                return []
            self._frames = list(self._pre) + [samples]
            self._pre.clear()
            self._pre_samples = 0
            self._active = True
            self._speech = seconds
            self._duration = sum(f.size for f in self._frames) / SAMPLE_RATE
            return []

        self._frames.append(samples)
        self._duration += seconds
        self._since_partial += seconds
        if voiced:
            self._speech += seconds
            self._silence = 0.0
        else:
            self._silence += seconds

        if self._silence >= s.end_silence_s or self._duration >= s.max_utterance_s:
            return self._settle()
        if self._since_partial >= s.partial_every_s and self._speech >= s.min_speech_s:
            self._since_partial = 0.0
            return [Action("partial", np.concatenate(self._frames))]
        return []

    def flush(self) -> list[Action]:
        """The audio stopped: settle what is there."""
        return self._settle() if self._active else []

    def _settle(self) -> list[Action]:
        actions: list[Action] = []
        if self._speech >= self.settings.min_speech_s:
            actions.append(Action("final", np.concatenate(self._frames)))
        self._reset()
        return actions

    def _remember(self, samples: np.ndarray) -> None:
        self._pre.append(samples)
        self._pre_samples += samples.size
        limit = int(self.settings.pre_roll_s * SAMPLE_RATE)
        while self._pre and self._pre_samples - self._pre[0].size >= limit:
            self._pre_samples -= self._pre.popleft().size
