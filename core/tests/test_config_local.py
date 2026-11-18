"""config.local.yaml: your own settings, laid over config.yaml and kept out of git."""

from __future__ import annotations

from bobd.config import load_config, local_override_for, merge_config

BASE = """
paths: {data: data, plugins: plugins}
plugins:
  settings:
    files: {read_roots: "~/Documents"}
knowledge:
  collections:
    - id: reading
      sources: [{files: {}}]
"""
LOCAL = """
plugins:
  settings:
    files: {read_roots: "~/Documents:~/dev"}
    mine: {output_dir: ~/out}
knowledge:
  collections:
    - id: private
      profile: some_pkg.parsers:Parser
      sources: [{folder: {path: ~/dev/meta}}]
"""


def test_the_local_file_is_found_beside_the_config(tmp_path):
    assert local_override_for(tmp_path / "config.yaml") == tmp_path / "config.local.yaml"


def test_local_settings_are_laid_over_the_shared_ones(tmp_path):
    (tmp_path / "config.yaml").write_text(BASE)
    (tmp_path / "config.local.yaml").write_text(LOCAL)
    config = load_config(tmp_path / "config.yaml")
    assert config.plugins.settings["files"]["read_roots"] == "~/Documents:~/dev"
    assert config.plugins.settings["mine"] == {"output_dir": "~/out"}
    assert [c.id for c in config.knowledge.collections] == ["reading", "private"]
    assert config.knowledge.collections[1].profile == "some_pkg.parsers:Parser"


def test_without_a_local_file_nothing_changes(tmp_path):
    (tmp_path / "config.yaml").write_text(BASE)
    config = load_config(tmp_path / "config.yaml")
    assert [c.id for c in config.knowledge.collections] == ["reading"]
    assert "mine" not in config.plugins.settings


def test_a_collection_with_the_same_id_is_merged_not_duplicated():
    merged = merge_config(
        {"c": [{"id": "a", "profile": "x", "sources": [1]}]},
        {"c": [{"id": "a", "sources": [2]}, {"id": "b"}]},
    )
    assert merged == {"c": [{"id": "a", "profile": "x", "sources": [2]}, {"id": "b"}]}


def test_other_lists_are_replaced():
    assert merge_config({"x": [1, 2]}, {"x": [3]}) == {"x": [3]}
