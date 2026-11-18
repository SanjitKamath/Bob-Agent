from __future__ import annotations

from pathlib import Path

import pytest
from bob_sdk.writeguard import Refused, check_grant, check_read, check_write


def test_credentials_are_never_read_or_written(tmp_path):
    for name in (".env", "id_rsa", "server.pem"):
        (tmp_path / name).write_text("x")
        with pytest.raises(Refused, match="credentials"):
            check_read(tmp_path / name)
        with pytest.raises(Refused, match="credentials"):
            check_write(tmp_path / name)


def test_a_repository_can_be_read_and_never_changed(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    check_read(tmp_path / "src" / "x.py")
    check_grant(tmp_path)
    with pytest.raises(Refused, match="git repository"):
        check_write(tmp_path / "src" / "x.py")
    with pytest.raises(Refused, match="credentials"):
        check_read(tmp_path / ".git" / "config")


def test_home_system_and_hidden_folders_are_off_limits():
    home = Path.home()
    for path in (
        home,
        Path("/"),
        Path("/etc/hosts"),
        Path("/System/x"),
        home / ".ssh" / "x",
        home / ".zshrc",
        home / "Library" / "x",
    ):
        with pytest.raises(Refused):
            check_write(path)
    check_write(home / "Documents" / "notes" / "a.txt")


def test_protected_roots_hold_even_inside_a_granted_folder(tmp_path):
    (tmp_path / "bob").mkdir()
    with pytest.raises(Refused, match="Bob's own"):
        check_write(tmp_path / "bob" / "x", [tmp_path / "bob"])
    check_write(tmp_path / "other", [tmp_path / "bob"])


def test_links_cannot_lead_out_of_a_folder(tmp_path):
    (tmp_path / ".git").mkdir()
    link = tmp_path.parent / (tmp_path.name + "-link")
    link.symlink_to(tmp_path)
    try:
        with pytest.raises(Refused, match="git repository"):
            check_write(link / "x.txt")
    finally:
        link.unlink()


def test_an_empty_path_is_refused():
    with pytest.raises(Refused):
        check_read("  ")
