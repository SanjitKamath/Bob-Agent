"""Which files a read-only tool may open.

Tools that read your files go through :class:`ReadGuard`. It keeps reads
inside the folders you allowed and away from credentials, whatever a model
asks for.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

# Never read, wherever they are.
DENIED_NAMES: tuple[str, ...] = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keychain", "*.keychain-db",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".netrc", ".npmrc", ".pypirc", "credentials",
    "credentials.json", "*.kdbx", "api-token",
)  # fmt: skip
DENIED_FOLDERS: tuple[str, ...] = (
    "secrets", "lyik_secrets", ".ssh", ".gnupg", ".aws", ".kube", "Keychains", ".git",
)  # fmt: skip


def is_secret(path: Path) -> bool:
    name = path.name
    if any(fnmatch.fnmatch(name, pattern) for pattern in DENIED_NAMES):
        return True
    return any(part in DENIED_FOLDERS for part in path.parts)


def parse_roots(value: str | Iterable[str] | None) -> list[Path]:
    """Read roots from a setting: a ``:``-separated string or a list."""
    if not value:
        return []
    items = value.split(os.pathsep) if isinstance(value, str) else list(value)
    return [Path(os.path.expanduser(item.strip())).resolve() for item in items if item.strip()]


class ReadGuard:
    def __init__(self, roots: str | Iterable[str] | None) -> None:
        self.roots = parse_roots(roots)

    def allows(self, path: Path) -> bool:
        resolved = path.resolve()
        inside = any(resolved == root or root in resolved.parents for root in self.roots)
        return inside and not is_secret(resolved)

    def check(self, path: str | os.PathLike[str], *, must_exist: bool = True) -> Path:
        """Resolve ``path`` and insist it is readable under the rules."""
        raw = os.fspath(path).strip()
        if not raw:
            raise ToolError("a path is required")
        resolved = Path(os.path.expanduser(raw)).resolve()
        if is_secret(resolved):
            raise ToolError(f"{resolved} holds credentials and is never read")
        if not any(resolved == root or root in resolved.parents for root in self.roots):
            allowed = ", ".join(str(root) for root in self.roots) or "none"
            raise ToolError(f"{resolved} is outside the folders Bob may read ({allowed})")
        if must_exist and not resolved.exists():
            raise ToolError(f"{resolved} does not exist")
        return resolved
