"""Rules for plugin output folders.

Every plugin that writes files goes through :class:`OutputFolder`. The four
rules from the design are enforced here and nowhere else:

1. The configured folder is the only place the plugin can write.
2. It is never inside a git repository or another protected folder.
3. Nothing is ever overwritten: each run gets a new subfolder.
4. Overrides are accepted only for a subfolder of the configured folder.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


class OutputFolderError(ValueError):
    """Raised when an output folder or a write into it breaks a rule."""


def expand(path: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and resolve symlinks without requiring the path to exist."""
    return Path(os.path.expanduser(os.fspath(path))).resolve()


def find_git_root(path: Path) -> Path | None:
    """Return the nearest folder at or above ``path`` that contains ``.git``."""
    resolved = expand(path)
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def is_within(path: Path, parent: Path) -> bool:
    try:
        expand(path).relative_to(expand(parent))
    except ValueError:
        return False
    return True


def default_protected_roots() -> list[Path]:
    """Folders an output folder may never live inside."""
    roots = [Path("~/dev"), Path("~/knowledge_base")]
    extra = os.environ.get("BOB_PROTECTED_ROOTS", "")
    roots.extend(Path(item) for item in extra.split(os.pathsep) if item)
    return [expand(root) for root in roots]


def validate_output_dir(path: str | Path, protected: Iterable[Path] | None = None) -> Path:
    """Check an output folder setting and return its resolved path.

    Refuses a folder that is inside a git repository (a ``.git`` in the folder
    or any parent) or inside a protected root such as ``~/dev``.
    """
    resolved = expand(path)
    if resolved == Path(resolved.anchor):
        raise OutputFolderError("the output folder cannot be the filesystem root")
    if resolved == expand("~"):
        raise OutputFolderError("the output folder cannot be your home folder")
    roots = default_protected_roots() if protected is None else [expand(p) for p in protected]
    for root in roots:
        if is_within(resolved, root):
            raise OutputFolderError(f"{resolved} is inside the protected folder {root}")
    git_root = find_git_root(resolved)
    if git_root is not None:
        raise OutputFolderError(f"{resolved} is inside the git repository at {git_root}")
    return resolved


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name).strip("._")
    if not cleaned:
        raise OutputFolderError(f"{name!r} is not a usable file or folder name")
    return cleaned


class OutputFolder:
    """A validated output folder that only ever creates new files."""

    def __init__(self, root: str | Path, protected: Iterable[Path] | None = None) -> None:
        self._protected = None if protected is None else list(protected)
        self.root = validate_output_dir(root, self._protected)

    def resolve_inside(self, relative: str | Path) -> Path:
        """Resolve ``relative`` and insist that it stays inside the folder."""
        candidate = Path(os.path.expanduser(os.fspath(relative)))
        target = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if target != self.root and not is_within(target, self.root):
            raise OutputFolderError(f"{target} is outside the output folder {self.root}")
        return target

    def new_run_dir(
        self, label: str, *, subfolder: str | None = None, now: datetime | None = None
    ) -> Path:
        """Create and return a fresh ``<label>_YYYY-MM-DD_HHMM`` folder for one run.

        ``subfolder`` is the ``--out`` override; it must stay inside the folder.
        """
        # Re-check on every run: a repository may have been created since startup.
        validate_output_dir(self.root, self._protected)
        base = self.resolve_inside(subfolder) if subfolder else self.root
        stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M")
        stem = f"{safe_name(label)}_{stamp}"
        base.mkdir(parents=True, exist_ok=True)
        for suffix in ("", *(f"_{n}" for n in range(2, 100))):
            run_dir = base / f"{stem}{suffix}"
            try:
                run_dir.mkdir()
            except FileExistsError:
                continue
            return run_dir
        raise OutputFolderError(f"could not create a new run folder under {base}")

    def write_new(self, path: str | Path, content: str | bytes) -> Path:
        """Write a new file. Fails if the file exists; never overwrites."""
        target = self.resolve_inside(path)
        if target == self.root:
            raise OutputFolderError("a file name is required")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        try:
            # "x" makes creation exclusive, so an existing file stops the run.
            with open(target, "xb") as handle:
                handle.write(data)
        except FileExistsError as exc:
            raise OutputFolderError(f"{target} already exists; refusing to overwrite") from exc
        return target
