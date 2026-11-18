"""The Whisper models Bob can use, and getting one onto this Mac safely.

A model is one file. It is only ever fetched from the address below and is checked against the
SHA-256 listed here (the one Hugging Face shows for the file), so a wrong or tampered download is
refused. The shared build carries ``base.en`` inside the app, so it downloads nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import ssl
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"


@dataclass(frozen=True)
class Model:
    name: str
    file: str
    sha256: str
    megabytes: int


MODELS: dict[str, Model] = {
    m.name: m
    for m in (
        Model("base.en", "ggml-base.en.bin", "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002", 148),
        Model("small.en", "ggml-small.en.bin", "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d", 488),
        Model("base", "ggml-base.bin", "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe", 148),
        Model("small", "ggml-small.bin", "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b", 488),
    )
}  # fmt: skip

SIZES = ("base", "small")


class ModelError(Exception):
    """The model could not be found, fetched or verified. The message is for the user."""


def language_code(locale: str) -> str:
    """en-US -> en."""
    return (locale or "en").split("-")[0].split("_")[0].lower() or "en"


def pick(locale: str, size: str) -> Model:
    """English gets the English-only model of that size, anything else the multilingual one."""
    size = size if size in SIZES else "base"
    return MODELS[f"{size}.en" if language_code(locale) == "en" else size]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(path: Path, model: Model) -> bool:
    """The file is the model it claims to be. A good check is remembered, keyed by size and time."""
    if not path.is_file():
        return False
    stat = path.stat()
    marker = path.with_name(path.name + ".ok")
    stamp = f"{model.sha256} {stat.st_size} {int(stat.st_mtime)}"
    try:
        if marker.read_text() == stamp:
            return True
    except OSError:
        pass
    if sha256_of(path) != model.sha256:
        return False
    with contextlib.suppress(OSError):  # a read-only folder (the app bundle) is checked again
        marker.write_text(stamp)
    return True


def _context() -> ssl.SSLContext:
    """TLS with certifi's certificates: a bundled Python may have none of its own."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def ensure(
    model: Model,
    models_dir: Path,
    bundled_dir: Path | None = None,
    progress: Callable[[float], None] | None = None,
    *,
    base_url: str = BASE_URL,
) -> Path:
    """The path of a verified copy of the model, fetching it first if needed."""
    if bundled_dir is not None and verified(bundled_dir / model.file, model):
        return bundled_dir / model.file
    target = models_dir / model.file
    if verified(target, model):
        return target

    models_dir.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    url = base_url + model.file
    secure = url.startswith("https://")
    if not secure and not url.startswith("http://127.0.0.1"):
        raise ModelError("the speech model can only be fetched over https")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Bob"})
        context = _context() if secure else None
        with (
            urllib.request.urlopen(request, timeout=30, context=context) as response,
            part.open("wb") as out,
        ):
            total = int(response.headers.get("Content-Length") or model.megabytes * 1_000_000)
            done = 0
            last = -1.0
            while block := response.read(1 << 20):
                out.write(block)
                digest.update(block)
                done += len(block)
                fraction = min(1.0, done / total)
                if progress and fraction - last >= 0.01:
                    last = fraction
                    progress(fraction)
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        raise ModelError(
            f"The speech model ({model.megabytes} MB) could not be downloaded: {exc}. "
            "Check the internet connection and try again."
        ) from exc
    if digest.hexdigest() != model.sha256:
        part.unlink(missing_ok=True)
        raise ModelError(
            "The downloaded speech model did not match its checksum, so it was discarded."
        )
    os.replace(part, target)
    return target
