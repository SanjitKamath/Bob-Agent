from __future__ import annotations

import http.server
import io
import json
import threading
import wave
from pathlib import Path

import numpy as np
import pytest
from bob_voice import cli, models
from bob_voice.engine import clean
from bob_voice.protocol import AUDIO, COMMAND, SAMPLE_RATE, Writer, frame, read_frames
from bob_voice.segmenter import Settings, Tracker

FRAME = 640


def tone(seconds: float, level: float = 0.1) -> list[np.ndarray]:
    n = int(seconds * SAMPLE_RATE / FRAME)
    wave_ = (np.sin(np.arange(FRAME) / 5.0) * level * 1.414).astype(np.float32)
    return [wave_.copy() for _ in range(n)]


def quiet(seconds: float) -> list[np.ndarray]:
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(int(seconds * SAMPLE_RATE / FRAME))]


def run(tracker: Tracker, frames: list[np.ndarray]):
    actions = []
    for f in frames:
        actions += tracker.push(f)
    return actions


# -- wire format -----------------------------------------------------------------------------------


def test_frames_round_trip_and_stop_at_a_broken_stream():
    stream = io.BytesIO(
        frame(COMMAND, b'{"cmd":"stop"}') + frame(AUDIO, b"\x01\x02") + b"\x02\xff\xff\xff\xff"
    )
    assert list(read_frames(stream)) == [(COMMAND, b'{"cmd":"stop"}'), (AUDIO, b"\x01\x02")]
    assert list(read_frames(io.BytesIO(b"\x02\x05\x00\x00\x00ab"))) == []  # cut short


def test_events_are_json_lines():
    out = io.StringIO()
    Writer(out)({"type": "final", "text": "héllo"})
    assert json.loads(out.getvalue()) == {"type": "final", "text": "héllo"}


# -- cutting speech into messages ------------------------------------------------------------------


def test_one_utterance_gives_partials_then_a_final_after_the_pause():
    tracker = Tracker(Settings(partial_every_s=0.8, end_silence_s=0.7))
    actions = run(tracker, quiet(0.5) + tone(2.0) + quiet(1.0))
    kinds = [a.kind for a in actions]
    assert kinds.count("final") == 1 and kinds[-1] == "final"
    assert 1 <= kinds.count("partial") <= 3
    final = actions[-1].audio
    assert final.size >= 2.0 * SAMPLE_RATE  # the whole utterance, and a little lead-in


def test_silence_and_coughs_produce_nothing():
    tracker = Tracker()
    assert run(tracker, quiet(5)) == []
    assert run(tracker, tone(0.1) + quiet(2)) == []  # shorter than a word
    assert not tracker.active


def test_two_utterances_are_separate_and_a_long_one_is_cut():
    tracker = Tracker(Settings(max_utterance_s=3.0))
    actions = run(tracker, tone(1.0) + quiet(1.0) + tone(1.0) + quiet(1.0))
    assert [a.kind for a in actions if a.kind == "final"] == ["final", "final"]
    long = run(Tracker(Settings(max_utterance_s=3.0)), tone(7.0))
    assert [a.kind for a in long].count("final") >= 2


def test_flush_settles_what_is_being_said():
    tracker = Tracker()
    run(tracker, tone(1.0))
    assert [a.kind for a in tracker.flush()] == ["final"]
    assert tracker.flush() == []


# -- cleaning Whisper's output ---------------------------------------------------------------------


def test_sound_tags_and_invented_closings_are_dropped():
    assert clean(" [BLANK_AUDIO] what is (music) on my list ♪") == "what is on my list"
    assert clean("Thank you.", audio_level=0.01) == ""
    assert clean("Thank you.", audio_level=0.2) == "Thank you."
    assert clean("anything", audio_level=0.001) == ""


# -- models ----------------------------------------------------------------------------------------


def test_english_gets_the_english_model_and_other_languages_the_multilingual_one():
    assert models.pick("en-GB", "base").name == "base.en"
    assert models.pick("en-US", "small").name == "small.en"
    assert models.pick("hi-IN", "small").name == "small"
    assert models.pick("es-ES", "nonsense").name == "base"
    assert all(len(m.sha256) == 64 for m in models.MODELS.values())


def serve_file(directory: Path):
    handler = type("H", (http.server.SimpleHTTPRequestHandler,), {
        "__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(self, *a, directory=str(directory), **k),
        "log_message": lambda *a: None,
    })  # fmt: skip
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/"


def test_a_model_is_fetched_verified_and_then_reused(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    body = b"pretend this is a whisper model" * 1000
    (served / "ggml-tiny.bin").write_bytes(body)
    model = models.Model("tiny", "ggml-tiny.bin", models.hashlib.sha256(body).hexdigest(), 1)
    server, url = serve_file(served)
    seen: list[float] = []
    try:
        path = models.ensure(model, tmp_path / "models", None, seen.append, base_url=url)
        assert path.read_bytes() == body and seen and seen[-1] == 1.0
        server.shutdown()
        # Second time: no network needed.
        assert (
            models.ensure(model, tmp_path / "models", None, base_url="http://127.0.0.1:1/") == path
        )
    finally:
        server.shutdown()


def test_a_download_that_does_not_match_its_checksum_is_discarded(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "ggml-tiny.bin").write_bytes(b"tampered")
    model = models.Model("tiny", "ggml-tiny.bin", "0" * 64, 1)
    server, url = serve_file(served)
    try:
        with pytest.raises(models.ModelError, match="checksum"):
            models.ensure(model, tmp_path / "models", None, base_url=url)
    finally:
        server.shutdown()
    assert list((tmp_path / "models").iterdir()) == []


def test_only_https_or_the_local_machine_is_allowed(tmp_path):
    model = models.Model("tiny", "x.bin", "0" * 64, 1)
    with pytest.raises(models.ModelError, match="https"):
        models.ensure(model, tmp_path, None, base_url="http://example.com/")


def test_a_bundled_model_is_used_without_downloading(tmp_path):
    body = b"bundled"
    model = models.Model("tiny", "ggml-tiny.bin", models.hashlib.sha256(body).hexdigest(), 1)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / model.file).write_bytes(body)
    assert (
        models.ensure(model, tmp_path / "m", bundled, base_url="http://127.0.0.1:1/")
        == bundled / model.file
    )


# -- the helper's loop, with a stand-in engine -----------------------------------------------------


def events_of(frames, load=None):
    out: list[dict] = []
    code = cli.serve(
        frames,
        out.append,
        load or (lambda command, progress: lambda audio: f"{audio.size / SAMPLE_RATE:.0f}s"),
    )
    return code, out


def pcm(frames_: list[np.ndarray]) -> bytes:
    return (np.concatenate(frames_) * 32767).astype("<i2").tobytes()


def stream(*parts: bytes | tuple[int, bytes]):
    out = []
    for p in parts:
        out.append(p if isinstance(p, tuple) else (AUDIO, p))
    return out


START = (COMMAND, b'{"cmd":"start","locale":"en-US","model":"base"}')
STOP = (COMMAND, b'{"cmd":"stop"}')
CANCEL = (COMMAND, b'{"cmd":"cancel"}')


def test_a_session_hears_settles_and_closes():
    audio = pcm(quiet(0.3) + tone(2.0) + quiet(1.0) + tone(1.0))
    chunks = [audio[i : i + 1280] for i in range(0, len(audio), 1280)]
    code, events = events_of([START, *stream(*chunks), STOP])
    types = [e["type"] for e in events]
    assert code == 0 and types[0] == "progress" and "ready" in types and types[-1] == "closed"
    finals = [e["text"] for e in events if e["type"] == "final"]
    assert len(finals) == 2  # the pause ended the first; stop settled the second
    assert events[types.index("ready")]["engine"] == "whisper"


def test_cancel_drops_what_was_heard():
    audio = pcm(tone(2.0))
    code, events = events_of([START, *stream(audio[:1280], audio[1280:]), CANCEL])
    assert code == 0 and not any(e["type"] == "final" for e in events)
    assert events[-1] == {"type": "closed"}


def test_a_model_that_will_not_load_is_reported():
    def broken(command, progress):
        raise models.ModelError("no internet")

    code, events = events_of([START], broken)
    assert code == 1 and events[-1] == {"type": "error", "code": "failed", "message": "no internet"}

    def crash(command, progress):
        raise RuntimeError("boom")

    code, events = events_of([START], crash)
    assert code == 1 and "boom" in events[-1]["message"]


def test_audio_before_start_is_ignored_and_the_end_of_input_closes_quietly():
    code, events = events_of([(AUDIO, b"\x00" * 1280)])
    assert code == 0 and events == []
    code, events = events_of([START, (AUDIO, b"\x00\x01" * 640)])
    assert code == 0 and not any(e["type"] == "final" for e in events)


def test_progress_from_a_download_is_passed_on():
    def slow(command, progress):
        progress(0.5)
        return lambda audio: ""

    _, events = events_of([START, STOP], slow)
    assert {"type": "progress", "what": "downloading", "fraction": 0.5} in events


def test_a_wav_file_is_read_and_resampled(tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes((np.sin(np.arange(48000) / 9.0) * 8000).astype("<i2").repeat(2).tobytes())
    audio = cli.read_wav(str(path))
    assert abs(audio.size - SAMPLE_RATE) < 5 and audio.dtype == np.float32
    frames = list(cli.wav_frames(str(path), "en-US", "base"))
    assert frames[0][0] == COMMAND and frames[-1][0] == COMMAND
