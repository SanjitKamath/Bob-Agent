"""Adding a plugin from a Python wheel file.

A wheel is code that will run with Bob's access, so adding one is two steps: *inspect* reads
the file without running any of it and says what it is and what it asks for; *install* unpacks
it, but only for the exact file (by SHA-256) that was inspected. Nothing in a wheel is imported
or executed here, and installing never starts the plugin: it appears switched off, and
switching it on shows the usual first-time consent.

Wheels are unpacked into the user plugin folder (``paths.user_site``), never into Bob itself.
Dependencies are not fetched: a wheel whose requirements are missing is refused.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

from bob_sdk.manifest import MANIFEST_FILENAME, ManifestError, load_manifest

MAX_WHEEL_BYTES = 25 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_FILES = 5000
REGISTRY = ".bob-plugins"  # in the user plugin folder: one JSON file per installed wheel
ENTRY_POINT_GROUP = "bob.plugins"
# Files that run code merely by being present in a site folder.
_RUNS_AT_STARTUP = (".pth",)
_NATIVE = (".so", ".dylib", ".pyd")
_NAME = re.compile(r"[^A-Za-z0-9]+")


class WheelError(Exception):
    """The wheel cannot be used; the message says why in plain words."""


@dataclass
class Inspection:
    """What a wheel is and asks for, read without running any of it."""

    dist: str
    version: str
    sha256: str
    plugin_id: str = ""
    title: str = ""
    description: str = ""
    network: Any = "none"
    write: list[str] = field(default_factory=list)
    tools: list[dict[str, str]] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items()}


def canonical(name: str) -> str:
    return _NAME.sub("-", name).strip("-").lower()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Every file in the wheel, refusing anything that could land outside the folder."""
    members = archive.infolist()
    if len(members) > MAX_FILES:
        raise WheelError(f"the wheel has more than {MAX_FILES} files")
    total = 0
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in path.parts[0]:
            raise WheelError(f"the wheel has an unsafe path: {name}")
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise WheelError(f"the wheel contains a link, which is not allowed: {name}")
        if name.lower().endswith(_RUNS_AT_STARTUP):
            raise WheelError(f"the wheel has a {path.suffix} file, which runs code at startup")
        total += member.file_size
    if total > MAX_UNPACKED_BYTES:
        raise WheelError("the wheel is too large once unpacked")
    return members


def _dist_info(members: list[zipfile.ZipInfo]) -> str:
    folders = {
        m.filename.split("/")[0] for m in members if m.filename.split("/")[0].endswith(".dist-info")
    }
    if len(folders) != 1:
        raise WheelError("this is not a wheel: it needs exactly one .dist-info folder")
    return folders.pop()


def _entry_points(text: str) -> dict[str, str]:
    """The ``bob.plugins`` section of entry_points.txt as {name: module}."""
    found: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
        elif section == ENTRY_POINT_GROUP and "=" in line:
            name, _, value = line.partition("=")
            found[name.strip()] = value.strip().split(":")[0].strip()
    return found


def _requirement_name(requirement: str) -> str | None:
    """The package a ``Requires-Dist`` line names; None for extras and other platforms' lines."""
    head, _, marker = requirement.partition(";")
    if "extra" in marker:
        return None
    return canonical(re.split(r"[\s<>=!~\[(]", head.strip(), maxsplit=1)[0])


def _missing(requirements: list[str]) -> list[str]:
    missing = []
    for requirement in requirements:
        name = _requirement_name(requirement)
        if not name:
            continue
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement.split(";")[0].strip())
    return missing


def inspect_wheel(path: Path, *, taken: set[str] | None = None) -> Inspection:
    """Read a wheel's name, plugin manifest and permissions. Nothing in it is run.

    ``taken`` are plugin ids already provided by something else; a wheel may not replace one.
    """
    size = path.stat().st_size
    if size > MAX_WHEEL_BYTES:
        raise WheelError(f"the file is larger than {MAX_WHEEL_BYTES // (1024 * 1024)} MB")
    if path.suffix != ".whl":
        raise WheelError("a plugin is added from a .whl file")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise WheelError("the file is not a valid wheel (it is not a zip file)") from exc
    with archive:
        members = _safe_members(archive)
        info = _dist_info(members)
        names = {m.filename for m in members}
        if f"{info}/METADATA" not in names:
            raise WheelError("the wheel has no METADATA")
        metadata = Parser().parsestr(archive.read(f"{info}/METADATA").decode("utf-8", "replace"))
        entries = {}
        if f"{info}/entry_points.txt" in names:
            entries = _entry_points(
                archive.read(f"{info}/entry_points.txt").decode("utf-8", "replace")
            )
        if not entries:
            raise WheelError(
                "this wheel does not declare a Bob plugin (no 'bob.plugins' entry point)"
            )
        module = next(iter(entries.values()))
        manifest_path = f"{module.replace('.', '/')}/{MANIFEST_FILENAME}"
        if manifest_path not in names:
            raise WheelError(f"the plugin has no {MANIFEST_FILENAME} ({manifest_path})")
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / MANIFEST_FILENAME
            target.write_bytes(archive.read(manifest_path))
            try:
                manifest = load_manifest(target)
            except ManifestError as exc:
                raise WheelError(f"the plugin's {MANIFEST_FILENAME} is not valid: {exc}") from exc
        result = Inspection(
            dist=canonical(metadata.get("Name", "")) or canonical(path.stem.split("-")[0]),
            version=metadata.get("Version", ""),
            sha256=sha256_of(path),
            plugin_id=manifest.id,
            title=manifest.name,
            description=manifest.description.strip(),
            network=manifest.permissions.network,
            write=list(manifest.permissions.write),
            tools=[
                {"name": name, "tier": str(tier.value)} for name, tier in manifest.tools.items()
            ],
            unmet=_missing(metadata.get_all("Requires-Dist") or []),
        )
        if taken and manifest.id in taken:
            raise WheelError(f"a plugin called {manifest.id!r} is already part of Bob")
        if any(name.lower().endswith(_NATIVE) for name in names):
            result.warnings.append("it contains compiled code (.so or .dylib files)")
        if result.network != "none":
            result.warnings.append("it can reach the internet: " + ", ".join(result.network))
        if any(tool["tier"] in ("read_granted", "write_granted") for tool in result.tools):
            result.warnings.append(
                "it reads or changes files in folders you name in a chat; each folder asks first"
            )
        if any(tool["tier"] == "external" for tool in result.tools):
            result.warnings.append("it has tools that act outside your Mac; each asks first")
    return result


def install_wheel(
    path: Path, user_site: Path, *, sha256: str, taken: set[str] | None = None
) -> Inspection:
    """Unpack the wheel that was inspected. Refuses if the file is not the one confirmed."""
    result = inspect_wheel(path, taken=taken)
    if result.sha256 != sha256:
        raise WheelError("this is not the file you confirmed; add it again")
    if result.unmet:
        raise WheelError(
            "it needs packages Bob does not have: "
            + ", ".join(result.unmet)
            + ". Bob does not download dependencies; use a wheel that bundles them."
        )
    user_site.mkdir(parents=True, exist_ok=True)
    remove_plugin(result.dist, user_site, missing_ok=True)  # an update replaces the old copy
    written: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for member in _safe_members(archive):
            destination = (user_site / member.filename).resolve()
            if not destination.is_relative_to(user_site.resolve()):
                raise WheelError(f"the wheel has an unsafe path: {member.filename}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            written.append(member.filename)
    registry = user_site / REGISTRY
    registry.mkdir(exist_ok=True)
    (registry / f"{result.dist}.json").write_text(
        json.dumps(
            {
                "plugin": result.plugin_id,
                "version": result.version,
                "sha256": result.sha256,
                "files": written,
            }
        ),
        encoding="utf-8",
    )
    return result


def installed(user_site: Path) -> list[dict[str, Any]]:
    """The plugins added from wheels."""
    registry = user_site / REGISTRY
    if not registry.is_dir():
        return []
    found = []
    for file in sorted(registry.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found.append({"dist": file.stem, "plugin": data.get("plugin", ""),
                      "version": data.get("version", "")})  # fmt: skip
    return found


def remove_plugin(dist: str, user_site: Path, *, missing_ok: bool = False) -> bool:
    """Delete the files a wheel put in the user plugin folder, and nothing else."""
    record = user_site / REGISTRY / f"{canonical(dist)}.json"
    if not record.is_file():
        if missing_ok:
            return False
        raise WheelError(f"no plugin {dist!r} was added from a wheel")
    root = user_site.resolve()
    files = json.loads(record.read_text(encoding="utf-8")).get("files", [])
    for name in files:
        target = (user_site / name).resolve()
        if target.is_relative_to(root) and target.is_file():
            target.unlink()
    for folder in sorted({(user_site / n).parent for n in files}, key=lambda p: -len(p.parts)):
        if folder.resolve() != root and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    record.unlink()
    return True
