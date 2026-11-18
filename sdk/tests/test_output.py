from __future__ import annotations

from datetime import datetime

import pytest
from bob_sdk.output import OutputFolder, OutputFolderError, find_git_root, validate_output_dir


def test_refuses_a_folder_inside_a_git_repository(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_git_root(nested) == repo.resolve()
    with pytest.raises(OutputFolderError, match="inside the git repository"):
        validate_output_dir(nested, protected=[])
    with pytest.raises(OutputFolderError, match="inside the git repository"):
        validate_output_dir(repo / "not" / "yet" / "created", protected=[])


def test_refuses_protected_roots_home_and_root(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    with pytest.raises(OutputFolderError, match="protected folder"):
        validate_output_dir(dev / "out", protected=[dev])
    with pytest.raises(OutputFolderError, match="home folder"):
        validate_output_dir("~", protected=[])
    with pytest.raises(OutputFolderError, match="filesystem root"):
        validate_output_dir("/", protected=[])


def test_each_run_gets_a_new_folder_and_nothing_is_overwritten(tmp_path):
    folder = OutputFolder(tmp_path / "out", protected=[])
    now = datetime(2026, 11, 13, 9, 5)
    first = folder.new_run_dir("NriAccountOpening", now=now)
    second = folder.new_run_dir("NriAccountOpening", now=now)
    assert first.name == "NriAccountOpening_2026-11-13_0905"
    assert second.name == "NriAccountOpening_2026-11-13_0905_2"

    written = folder.write_new(first / "form.py", "x = 1\n")
    assert written.read_text() == "x = 1\n"
    with pytest.raises(OutputFolderError, match="already exists"):
        folder.write_new(first / "form.py", "x = 2\n")
    assert written.read_text() == "x = 1\n"


def test_overrides_must_stay_inside_the_folder(tmp_path):
    folder = OutputFolder(tmp_path / "out", protected=[])
    assert folder.new_run_dir("f", subfolder="acme").parent == (tmp_path / "out" / "acme").resolve()
    for escape in ("../elsewhere", str(tmp_path / "other"), "acme/../../x"):
        with pytest.raises(OutputFolderError, match="outside the output folder"):
            folder.new_run_dir("f", subfolder=escape)
        with pytest.raises(OutputFolderError, match="outside the output folder"):
            folder.write_new(f"{escape}/file.py", "x")


def test_a_repository_created_later_is_noticed(tmp_path):
    folder = OutputFolder(tmp_path / "out", protected=[])
    folder.new_run_dir("ok")
    (tmp_path / "out" / ".git").mkdir()
    with pytest.raises(OutputFolderError, match="git repository"):
        folder.new_run_dir("blocked")
