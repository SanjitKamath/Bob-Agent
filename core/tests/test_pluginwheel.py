from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from bobd.pluginwheel import (
    WheelError,
    inspect_wheel,
    install_wheel,
    installed,
    remove_plugin,
    sha256_of,
)

MANIFEST = """\
id: hello
name: Hello
version: 0.1.0
description: Says hello.
mcp: python -m hello_plugin.server
tools:
  greet: read
permissions:
  network: none
"""


def make_wheel(tmp_path: Path, *, extra: dict[str, str] | None = None, entry: bool = True,
               manifest: str | None = MANIFEST, requires: str = "") -> Path:  # fmt: skip
    path = tmp_path / "hello_plugin-0.1.0-py3-none-any.whl"
    files = {
        "hello_plugin/__init__.py": "",
        "hello_plugin/server.py": "print('never run here')\n",
        "hello_plugin-0.1.0.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: hello-plugin\nVersion: 0.1.0\n{requires}"
        ),
        "hello_plugin-0.1.0.dist-info/WHEEL": "Wheel-Version: 1.0\n",
        **(extra or {}),
    }
    if manifest is not None:
        files["hello_plugin/plugin.yaml"] = manifest
    if entry:
        files["hello_plugin-0.1.0.dist-info/entry_points.txt"] = (
            "[bob.plugins]\nhello = hello_plugin\n"
        )
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def test_inspect_says_what_the_plugin_is_and_asks_for(tmp_path):
    found = inspect_wheel(make_wheel(tmp_path))
    assert (found.dist, found.version, found.plugin_id) == ("hello-plugin", "0.1.0", "hello")
    assert found.tools == [{"name": "greet", "tier": "read"}]
    assert found.network == "none" and found.unmet == [] and found.warnings == []
    assert len(found.sha256) == 64


@pytest.mark.parametrize(
    ("build", "words"),
    [
        (dict(entry=False), "does not declare a Bob plugin"),
        (dict(manifest=None), "has no plugin.yaml"),
        (dict(manifest="id: x\n"), "not valid"),
        (dict(extra={"evil.pth": "import os"}), "runs code at startup"),
        (dict(extra={"../escape.py": "x"}), "unsafe path"),
        (dict(extra={"/abs.py": "x"}), "unsafe path"),
    ],
)
def test_a_wheel_that_is_unsafe_or_not_a_plugin_is_refused_without_running_it(
    tmp_path, build, words
):
    with pytest.raises(WheelError, match=words):
        inspect_wheel(make_wheel(tmp_path, **build))


def test_only_wheel_files_and_real_zips_are_read(tmp_path):
    other = tmp_path / "x.txt"
    other.write_text("hi")
    with pytest.raises(WheelError, match=r"\.whl"):
        inspect_wheel(other)
    broken = tmp_path / "x.whl"
    broken.write_text("not a zip")
    with pytest.raises(WheelError, match="not a zip"):
        inspect_wheel(broken)


def test_a_plugin_cannot_replace_one_that_ships_with_bob(tmp_path):
    with pytest.raises(WheelError, match="already part of Bob"):
        inspect_wheel(make_wheel(tmp_path), taken={"hello"})


def test_install_needs_the_exact_file_that_was_confirmed(tmp_path):
    wheel = make_wheel(tmp_path)
    site = tmp_path / "site"
    with pytest.raises(WheelError, match="not the file you confirmed"):
        install_wheel(wheel, site, sha256="0" * 64)
    assert not site.exists() or not any(site.iterdir())


def test_install_unpacks_lists_and_removes_cleanly(tmp_path):
    wheel = make_wheel(tmp_path)
    site = tmp_path / "site"
    install_wheel(wheel, site, sha256=sha256_of(wheel))
    assert (site / "hello_plugin" / "plugin.yaml").is_file()
    assert installed(site) == [{"dist": "hello-plugin", "plugin": "hello", "version": "0.1.0"}]
    # Adding it again replaces it rather than stacking up.
    install_wheel(wheel, site, sha256=sha256_of(wheel))
    assert len(installed(site)) == 1
    assert remove_plugin("hello-plugin", site) is True
    assert installed(site) == []
    assert not (site / "hello_plugin").exists()
    with pytest.raises(WheelError, match="no plugin"):
        remove_plugin("hello-plugin", site)


def test_missing_requirements_stop_the_install_because_bob_downloads_nothing(tmp_path):
    wheel = make_wheel(tmp_path, requires="Requires-Dist: surely-not-installed-xyz>=1\n")
    found = inspect_wheel(wheel)
    assert found.unmet == ["surely-not-installed-xyz>=1"]
    with pytest.raises(WheelError, match="does not download dependencies"):
        install_wheel(wheel, tmp_path / "site", sha256=found.sha256)


def test_warnings_name_network_native_code_and_external_tools(tmp_path):
    manifest = MANIFEST.replace("greet: read", "send: external").replace(
        "network: none", "network: [api.example.com]"
    )
    wheel = make_wheel(tmp_path, manifest=manifest, extra={"hello_plugin/fast.so": "x"})
    warnings = " | ".join(inspect_wheel(wheel).warnings)
    assert (
        "compiled code" in warnings
        and "api.example.com" in warnings
        and "outside your Mac" in warnings
    )


async def test_inspect_install_then_the_plugin_is_found_but_off(bob, tmp_path, monkeypatch):
    import base64
    import sys

    import httpx
    from bobd.api import create_api

    site = tmp_path / "user_site"
    bob.config.paths.user_site = str(site)
    bob.config.plugins.discover_installed = True
    monkeypatch.setattr(sys, "path", list(sys.path))  # undone after the test
    wheel = make_wheel(tmp_path)
    transport = httpx.ASGITransport(app=create_api(bob))
    headers = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://bobd", headers=headers) as c:
        body = {"filename": wheel.name, "data_b64": base64.b64encode(wheel.read_bytes()).decode()}
        looked = (await c.post("/v1/plugins/wheel/inspect", json=body)).json()
        assert looked["plugin_id"] == "hello" and looked["tools"] == [
            {"name": "greet", "tier": "read"}
        ]
        assert not site.exists() or not any(site.glob("hello_plugin"))  # nothing unpacked yet
        wrong = await c.post("/v1/plugins/wheel/install", json={"sha256": "0" * 64})
        assert wrong.status_code == 404
        done = await c.post("/v1/plugins/wheel/install", json={"sha256": looked["sha256"]})
        assert done.status_code == 200, done.text
        ids = {p["id"]: p for p in done.json()["plugins"]["plugins"]}
        assert "hello" in ids and ids["hello"]["enabled"] is False  # added, not switched on
        assert (await c.get("/v1/plugins/wheel/installed")).json()["plugins"][0][
            "plugin"
        ] == "hello"
        removed = await c.delete("/v1/plugins/wheel/hello-plugin")
        assert removed.json() == {"ok": True, "restart_needed": True}
        assert not (site / "hello_plugin").exists()
