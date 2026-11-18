"""Proving the read-only guarantee for code.

Each check tries to do something Bob must never be able to do. Every attempt
has to fail. If one succeeds, bobd shuts itself down.

The checks map to the enforcement levels in the design:

* Tools:  no tool outside the tier vocabulary, nothing undeclared, no shell tool.
* Git:    the wrapper refuses write commands, and a real push from a clone fails.
* macOS:  the service cannot write into your code folders.
* LYIK:   the environment holds no LYIK server credentials.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bob_sdk.manifest import Tier

from .config import Config
from .plugins import PluginManager

# The design's fixed allow-list. Checked against the Knowledge plugin's wrapper.
GIT_ALLOW_LIST = frozenset(
    {"clone", "fetch", "ls-remote", "rev-parse", "sparse-checkout", "reset", "clean"}
)
_SHELL_LIKE = re.compile(r"(^|_)(shell|exec|execute|run_command|bash|terminal|eval)($|_)")
_CODE_WRITE = re.compile(r"(^|_)(commit|push|merge|rebase|deploy|load_form|load_to_server)($|_)")
_LYIK_SECRET = re.compile(r"LYIK.*(TOKEN|PASSWORD|SECRET|KEY|CREDENTIAL)", re.IGNORECASE)


@dataclass(frozen=True)
class Check:
    name: str
    level: str  # tools | git | macos | lyik
    passed: bool
    detail: str
    advisory: bool = False  # reported, but does not shut bobd down

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "passed": self.passed,
            "detail": self.detail,
            "advisory": self.advisory,
        }


@dataclass(frozen=True)
class ConformanceReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        """True when no enforced check let a write through."""
        return all(check.passed or check.advisory for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed and not check.advisory]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.as_dict() for check in self.checks]}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "bob-conformance",
        "GIT_AUTHOR_EMAIL": "bob@localhost",
        "GIT_COMMITTER_NAME": "bob-conformance",
        "GIT_COMMITTER_EMAIL": "bob@localhost",
    }
    return subprocess.run(  # noqa: S603 - fixed binary and arguments
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False
    )


def check_tools(plugins: PluginManager) -> list[Check]:
    checks: list[Check] = []
    vocabulary = {tier.value for tier in Tier}
    bad_tier, undeclared, shell_like, code_write = [], [], [], []
    for plugin in plugins.plugins.values():
        for tool, tier in plugin.manifest.tools.items():
            if tier.value not in vocabulary:
                bad_tier.append(f"{plugin.id}.{tool}")
            if _SHELL_LIKE.search(tool):
                shell_like.append(f"{plugin.id}.{tool}")
            if _CODE_WRITE.search(tool):
                code_write.append(f"{plugin.id}.{tool}")
        if plugin.running:
            undeclared += [
                f"{plugin.id}.{name}"
                for name in plugin.mcp_tools
                if name not in plugin.manifest.tools
            ]
    checks.append(
        Check(
            "tiers come from the fixed vocabulary",
            "tools",
            not bad_tier,
            ", ".join(bad_tier) or "ok",
        )
    )
    checks.append(
        Check(
            "no undeclared tool is registered",
            "tools",
            not undeclared,
            ", ".join(undeclared) or "ok",
        )
    )
    checks.append(
        Check(
            "no shell or run-command tool exists",
            "tools",
            not shell_like,
            ", ".join(shell_like) or "ok",
        )
    )
    checks.append(
        Check(
            "no commit, push or load tool exists",
            "tools",
            not code_write,
            ", ".join(code_write) or "ok",
        )
    )
    return checks


def check_git(knowledge_root: Path) -> list[Check]:
    """Try to commit and push through the wrapper, then push with plain git."""
    try:
        from bob_knowledge.git import ALLOWED_COMMANDS, GitNotAllowed, GitWrapper
    except ImportError:
        return [
            Check("git wrapper is present", "git", True, "Knowledge plugin not installed", True)
        ]

    checks = [
        Check(
            "git allow-list matches the design",
            "git",
            set(ALLOWED_COMMANDS) == set(GIT_ALLOW_LIST),
            ", ".join(sorted(ALLOWED_COMMANDS)),
        )
    ]
    knowledge_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".conformance-", dir=knowledge_root))
    upstream = Path(tempfile.mkdtemp(prefix="bob-conformance-upstream-"))
    try:
        _git(["init", "-q", "-b", "main"], upstream)
        (upstream / "README.md").write_text("conformance\n", encoding="utf-8")
        _git(["add", "-A"], upstream)
        _git(["commit", "-q", "-m", "init"], upstream)
        _git(["config", "uploadpack.allowFilter", "true"], upstream)

        wrapper = GitWrapper(knowledge_root)
        clone = work / "clone"
        wrapper.clone(f"file://{upstream}", clone, branch="main", sparse=["**"])
        wrapper.checkout_exact(clone, wrapper.fetch(clone, "main"))

        for command in ("commit", "push", "add", "merge"):
            try:
                wrapper.run([command], cwd=clone)
                refused = False
            except GitNotAllowed:
                refused = True
            checks.append(
                Check(
                    f"wrapper refuses git {command}",
                    "git",
                    refused,
                    "refused" if refused else "RAN",
                )
            )
        try:
            wrapper.run(["rev-parse", "HEAD"], cwd=upstream)
            confined = False
        except GitNotAllowed:
            confined = True
        checks.append(
            Check(
                "wrapper only runs inside the knowledge root", "git", confined, str(knowledge_root)
            )
        )

        # Bypass the wrapper entirely: plain git must still be unable to push.
        (clone / "evil.txt").write_text("x", encoding="utf-8")
        _git(["add", "-A"], clone)
        _git(["commit", "-q", "-m", "evil"], clone)
        pushed = _git(["push", "origin", "HEAD:refs/heads/conformance-evil"], clone)
        landed = (
            "conformance-evil" in _git(["branch", "--list", "conformance-evil"], upstream).stdout
        )
        checks.append(
            Check(
                "a push from a clone fails",
                "git",
                pushed.returncode != 0 and not landed,
                "push rejected" if pushed.returncode != 0 else "PUSH SUCCEEDED",
            )
        )
        # The hook alone must also stop a push when the push URL is restored.
        _git(["config", "remote.origin.pushurl", f"file://{upstream}"], clone)
        hooked = _git(["push", "origin", "HEAD:refs/heads/conformance-evil"], clone)
        landed = (
            "conformance-evil" in _git(["branch", "--list", "conformance-evil"], upstream).stdout
        )
        checks.append(
            Check(
                "the pre-push hook alone stops a push",
                "git",
                hooked.returncode != 0 and not landed,
                "hook rejected" if hooked.returncode != 0 else "PUSH SUCCEEDED",
            )
        )
    except Exception as exc:
        checks.append(
            Check("git conformance could run", "git", False, f"{exc.__class__.__name__}: {exc}")
        )
    finally:
        for folder in (work, upstream):
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o644)
            shutil.rmtree(folder, ignore_errors=True)
    return checks


def check_filesystem(config: Config) -> list[Check]:
    """Try to write a file in each protected folder. The OS must refuse."""
    advisory = config.security.os_isolation != "required"
    checks: list[Check] = []
    for root in config.security.protected_roots:
        folder = Path(os.path.expanduser(root))
        if not folder.is_dir():
            checks.append(Check(f"cannot write in {root}", "macos", True, "folder does not exist"))
            continue
        probe = folder / f".bob-conformance-{uuid.uuid4().hex}"
        try:
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            checks.append(
                Check(f"cannot write in {root}", "macos", True, f"refused: {exc.strerror}")
            )
            continue
        os.close(descriptor)
        probe.unlink(missing_ok=True)
        detail = "WRITE SUCCEEDED: bobd is not running as the separate 'bob' user"
        checks.append(Check(f"cannot write in {root}", "macos", False, detail, advisory))
    return checks


def check_lyik_credentials() -> list[Check]:
    leaked = sorted(name for name in os.environ if _LYIK_SECRET.search(name))
    return [
        Check(
            "no LYIK server credentials in the environment",
            "lyik",
            not leaked,
            ", ".join(leaked) or "nothing to log in with",
        )
    ]


def run_conformance(config: Config, plugins: PluginManager) -> ConformanceReport:
    return ConformanceReport(
        [
            *check_tools(plugins),
            *check_git(config.knowledge_root),
            *check_filesystem(config),
            *check_lyik_credentials(),
        ]
    )
