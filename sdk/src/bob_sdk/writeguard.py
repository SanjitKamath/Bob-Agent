"""What a tool that reads or changes your files may never touch, whatever you approved.

Approval says "this folder". These rules say "but never these", and no approval, grant or
instruction from a model can lift them:

* credentials and keys are never read or written;
* a git repository is never changed (reading one is fine): its code is yours to edit and push;
* system folders, your home folder itself, hidden folders in your home (``~/.ssh``, ``~/.zshrc``,
  ``~/.config``), ``~/Library`` and Bob's own folders are never changed.

The core applies them before a call reaches a plugin, and plugins apply them again.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .output import find_git_root, is_within
from .paths import is_secret

# Folders nothing may be changed under, on any Mac.
SYSTEM_FOLDERS: tuple[str, ...] = (
    "/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/private",
    "/var", "/opt", "/dev", "/cores", "/Network",
)  # fmt: skip


class Refused(ValueError):
    """The path is off limits. The message is safe to show to a model and to you."""


def resolve(path: str | os.PathLike[str]) -> Path:
    """An absolute path with ``~`` expanded and links followed; it need not exist yet."""
    raw = os.fspath(path).strip()
    if not raw:
        raise Refused("a path is required")
    return Path(os.path.expanduser(raw)).resolve()


def _under_home_dot_folder(path: Path, home: Path) -> bool:
    try:
        first = path.relative_to(home).parts[0]
    except (ValueError, IndexError):
        return False
    return first.startswith(".") or first == "Library"


def refusal_for_reading(path: Path) -> str | None:
    """Why ``path`` may not be read, or None."""
    if is_secret(path):
        return f"{path} holds credentials or keys and is never read"
    return None


def refusal_for_granting(path: Path, protected: Iterable[Path] = ()) -> str | None:
    """Why ``path`` may not be handed to Bob as a working folder at all, or None.

    A repository can be granted (to be read); changing anything inside one is refused later.
    """
    reason = refusal_for_reading(path)
    if reason:
        return reason.replace("never read", "never given to Bob")
    home = Path.home().resolve()
    if path == home or path == Path(path.anchor):
        return f"{path} is too broad: name a folder inside it"
    for system in SYSTEM_FOLDERS:
        if is_within(path, Path(system)) and not any(
            is_within(path, Path(ok)) for ok in ("/private/tmp", "/private/var/folders")
        ):
            return f"{path} is a system folder and is never changed"
    if _under_home_dot_folder(path, home):
        return f"{path} is a hidden or system folder in your home and is never changed"
    for root in protected:
        if is_within(path, root):
            return f"{path} is inside {root}, which is Bob's own or protected and is never changed"
    return None


def refusal_for_changing(path: Path, protected: Iterable[Path] = ()) -> str | None:
    """Why ``path`` may not be changed, or None."""
    reason = refusal_for_granting(path, protected)
    if reason:
        return reason.replace("never given to Bob", "never changed")
    repo = find_git_root(path)
    if repo is not None:
        return f"{path} is inside the git repository at {repo}: Bob never changes a repository"
    return None


def check_grant(path: str | os.PathLike[str], protected: Iterable[Path] = ()) -> Path:
    """The resolved folder or file to grant, or :class:`Refused`."""
    resolved = resolve(path)
    reason = refusal_for_granting(resolved, [resolve(p) for p in protected])
    if reason:
        raise Refused(reason)
    return resolved


def check_read(path: str | os.PathLike[str]) -> Path:
    """The resolved path, or :class:`Refused`."""
    resolved = resolve(path)
    reason = refusal_for_reading(resolved)
    if reason:
        raise Refused(reason)
    return resolved


def check_write(path: str | os.PathLike[str], protected: Iterable[Path] = ()) -> Path:
    """The resolved path, or :class:`Refused`."""
    resolved = resolve(path)
    reason = refusal_for_changing(resolved, [resolve(p) for p in protected])
    if reason:
        raise Refused(reason)
    return resolved
