"""The API bearer token, generated at install and kept in the macOS Keychain."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

KEYRING_SERVICE = "bob"
KEYRING_API_TOKEN = "api-token"
TOKEN_ENV = "BOB_API_TOKEN"


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        return None if isinstance(keyring.get_keyring(), FailKeyring) else keyring
    except Exception:  # no usable backend
        return None


def get_secret(name: str) -> str | None:
    backend = _keyring()
    if backend is None:
        return None
    try:
        return backend.get_password(KEYRING_SERVICE, name)
    except Exception as exc:
        log.warning("keychain read of %s failed: %s", name, exc.__class__.__name__)
        return None


def set_secret(name: str, value: str) -> bool:
    backend = _keyring()
    if backend is None:
        return False
    try:
        backend.set_password(KEYRING_SERVICE, name, value)
    except Exception as exc:
        log.warning("keychain write of %s failed: %s", name, exc.__class__.__name__)
        return False
    return True


def delete_secret(name: str) -> None:
    backend = _keyring()
    if backend is not None:
        try:
            backend.delete_password(KEYRING_SERVICE, name)
        except Exception:  # already gone
            return


def load_or_create_token(data_dir: Path) -> str:
    """Return the API token, creating it on first run.

    Order: ``$BOB_API_TOKEN`` (tests, CI), the Keychain, then a ``0600`` file in
    the data folder for machines where no Keychain is available to the service.
    """
    env = os.environ.get(TOKEN_ENV)
    if env:
        return env
    stored = get_secret(KEYRING_API_TOKEN)
    if stored:
        return stored
    fallback = data_dir / "api-token"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    if not set_secret(KEYRING_API_TOKEN, token):
        data_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(fallback, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
        log.warning("no Keychain available; API token stored in %s", fallback)
    return token


def read_token(data_dir: Path) -> str | None:
    """Read the token without creating one (for the CLI)."""
    env = os.environ.get(TOKEN_ENV)
    if env:
        return env
    stored = get_secret(KEYRING_API_TOKEN)
    if stored:
        return stored
    fallback = data_dir / "api-token"
    return fallback.read_text(encoding="utf-8").strip() if fallback.is_file() else None


def tokens_match(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())
