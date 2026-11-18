"""The parser contract shared by the Knowledge plugin and plugins that contribute parsers.

A plugin never imports the Knowledge plugin. It subclasses :class:`ProfileParser`
from here, and the Knowledge plugin loads it by name.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

Authority = Literal["spec", "examples"]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalise_text(text: str) -> str:
    """Normalise text so cosmetic changes do not change its hash."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE.sub(" ", line).rstrip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def content_hash(text: str) -> str:
    return sha256_bytes(normalise_text(text).encode("utf-8"))


_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, limit: int = 80) -> str:
    slug = _SLUG.sub("-", value.lower()).strip("-")
    return slug[:limit].strip("-") or "section"


@dataclass(frozen=True)
class SourceFile:
    """One file of a source, as handed to a parser."""

    collection: str
    source: str  # source id, e.g. the repo name
    path: str  # POSIX path relative to the source root
    abs_path: Path
    authority: Authority = "examples"

    def read_bytes(self) -> bytes:
        return self.abs_path.read_bytes()

    def read_text(self) -> str:
        data = self.read_bytes()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    @property
    def suffix(self) -> str:
        return Path(self.path).suffix.lower()


@dataclass
class Draft:
    """What a parser returns for one chunk, before ids, headers and hashes are added."""

    kind: str  # e.g. "section", "function", "class", "table", "slide", "image"
    title: str  # heading or symbol; becomes part of the stable id
    text: str
    line: int = 1
    symbols: list[str] = field(default_factory=list)  # exact names this chunk defines
    meta: dict[str, Any] = field(default_factory=dict)
    valid: bool = True  # False keeps the chunk out of retrieval
    invalid_reason: str = ""


@dataclass(frozen=True)
class Chunk:
    id: str  # stable: "<source>:<path>:<symbol-or-heading>"
    collection: str
    source: str
    path: str
    kind: str
    title: str
    text: str
    header: str  # "collection / source / path / heading-or-symbol"
    line: int
    hash: str  # hash of the normalised text; decides whether it needs embedding
    authority: Authority
    version: str  # commit SHA or file hash
    symbols: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    valid: bool = True
    invalid_reason: str = ""

    @property
    def embedding_text(self) -> str:
        return f"{self.header}\n\n{self.text}"

    @property
    def citation(self) -> str:
        return f"{self.source}:{self.path}:{self.line}@{self.version[:12]}"


def build_chunks(file: SourceFile, drafts: list[Draft], version: str) -> list[Chunk]:
    """Turn a file's drafts into chunks with stable, unique ids and context headers."""
    chunks: list[Chunk] = []
    seen: dict[str, int] = {}
    for draft in drafts:
        text = draft.text.strip()
        if not text:
            continue
        key = draft.title.strip() if draft.symbols else slugify(draft.title)
        key = key or "section"
        seen[key] = seen.get(key, 0) + 1
        suffix = "" if seen[key] == 1 else f"#{seen[key]}"
        chunks.append(
            Chunk(
                id=f"{file.source}:{file.path}:{key}{suffix}",
                collection=file.collection,
                source=file.source,
                path=file.path,
                kind=draft.kind,
                title=draft.title.strip(),
                text=text,
                header=" / ".join(
                    part for part in (file.collection, file.source, file.path, draft.title) if part
                ),
                line=max(1, draft.line),
                hash=content_hash(text),
                authority=file.authority,
                version=version,
                symbols=tuple(dict.fromkeys(s for s in draft.symbols if s)),
                meta=dict(draft.meta),
                valid=draft.valid,
                invalid_reason=draft.invalid_reason,
            )
        )
    return chunks


class ParseError(Exception):
    """A file could not be parsed. It is logged and skipped, never indexed half-read."""


@runtime_checkable
class Parser(Protocol):
    def parse(self, file: SourceFile) -> list[Draft]:
        """Split one file into chunk drafts. Raise :class:`ParseError` on failure."""
        ...


class ProfileParser:
    """Base class for a plugin parser named in a collection's ``profile``.

    A profile can customise three pipeline steps for its own collection
    without touching the pipeline:

    * ``parse``     - step 2; return ``None`` to fall back to the default parser
    * ``enrich``    - step 3; add structure such as an import graph
    * ``validate``  - step 4; flag chunks that fail a domain check

    ``coverage_gaps`` backs the "full coverage" hard check.
    """

    #: Bump when the chunking rules change, to force a full rebuild.
    version: str = "1"

    def parse(self, file: SourceFile) -> list[Draft] | None:
        return None

    def enrich(self, chunks: list[Chunk], all_chunks: Sequence[Chunk]) -> list[Chunk]:
        """Return ``chunks`` (the changed ones) with extra structure added.

        ``all_chunks`` is every live chunk of the collection after this run.
        """
        return chunks

    def validate(self, chunk: Chunk) -> tuple[bool, str]:
        """Return ``(valid, reason)``. Invalid chunks are never used as examples."""
        return True, ""

    def coverage_gaps(self, all_chunks: Sequence[Chunk]) -> list[str]:
        """Names that should have a chunk but do not. Any gap blocks the snapshot."""
        return []


def split_long(text: str, *, limit: int = 6000, overlap: int = 200) -> list[str]:
    """Split text that is too long for one chunk at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(paragraph) > limit:
            parts.append(paragraph[:limit])
            paragraph = paragraph[limit - overlap :]
        current = paragraph
    if current:
        parts.append(current)
    return parts
