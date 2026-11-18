from __future__ import annotations

import copy

import pytest
from bob_sdk.manifest import ManifestError, Tier, coerce_setting, load_manifest, parse_manifest

AUTHOR = {
    "id": "form-author",
    "name": "Form Author",
    "version": "0.1.0",
    "mcp": "python -m form_author.server",
    "specialist": {
        "prompt": "prompts/specialist.md",
        "model_role": "coder",
        "routes": ["form", "template", "form builder"],
    },
    "tools": {"find_examples": "read", "scaffold": "write_output", "validate": "read"},
    "knowledge": {
        "provides": [{"id": "forms", "parser": "form_author.parsers:FormParser"}],
        "uses": ["forms"],
    },
    "settings": {
        "output_dir": {"type": "path", "default": "~/bob_output/forms", "outside_git": True},
        "default_client": {"type": "string", "default": "acme"},
    },
    "permissions": {"network": "none", "write": ["settings.output_dir"]},
}


def variant(**changes):
    data = copy.deepcopy(AUTHOR)
    data.update(changes)
    return data


def test_design_example_manifest_is_valid():
    manifest = parse_manifest(AUTHOR)
    assert manifest.tools["scaffold"] is Tier.WRITE_OUTPUT
    assert manifest.command == ["python", "-m", "form_author.server"]
    assert manifest.write_setting_keys == ["output_dir"]
    assert manifest.knowledge.may_read("forms") and not manifest.knowledge.may_read("contracts")
    assert manifest.default_settings() == {
        "output_dir": "~/bob_output/forms",
        "default_client": "acme",
    }
    schema = manifest.settings_json_schema()
    assert schema["properties"]["output_dir"] == {
        "type": "string",
        "title": "Output dir",
        "default": "~/bob_output/forms",
        "format": "path",
    }


@pytest.mark.parametrize("tier", ["write_code", "commit", "push", "load", "admin", "shell", ""])
def test_any_tier_outside_the_vocabulary_is_rejected(tier):
    with pytest.raises(ManifestError, match="tools"):
        parse_manifest(variant(tools={"do_it": tier}))


def test_only_external_needs_confirmation():
    assert [tier for tier in Tier if tier.needs_confirmation] == [Tier.EXTERNAL]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"id": "Bad Id"}, "id"),
        ({"mcp": "  "}, "mcp"),
        ({"surprise": 1}, "surprise"),
        ({"tools": {"BadName": "read"}}, "snake_case"),
        ({"permissions": {"write": ["output_dir"]}}, "settings.<name>"),
        ({"permissions": {"write": ["settings.default_client"]}}, "not a path setting"),
        ({"permissions": {"network": "none", "write": []}}, "write_output tool needs a folder"),
        ({"admin_tools": ["nope"]}, "undeclared tool"),
        (
            {"routines": [{"id": "r", "tool": "nope", "every_minutes": 5}]},
            "undeclared tool",
        ),
        (
            {
                "routines": [
                    {"id": "r", "tool": "validate", "cron": "0 2 * * *", "every_minutes": 5}
                ]
            },
            "exactly one of cron",
        ),
        ({"routines": [{"id": "r", "tool": "validate", "cron": "0 2 * *"}]}, "five fields"),
        (
            {"routines": [{"id": "r", "tool": "validate", "prompt": "x", "cron": "0 2 * * *"}]},
            "exactly one of tool, prompt",
        ),
        (
            {"widget_card": {"title": "t", "source": "scaffold"}},
            "read-tier",
        ),
        ({"widget_card": {"title": "t", "source": "validate", "refresh": "hourly"}}, "refresh"),
        (
            {
                "widget_card": {
                    "title": "t",
                    "source": "validate",
                    "buttons": [{"label": "x", "tool": "mail.triage"}],
                }
            },
            "undeclared tool",
        ),
    ],
)
def test_invalid_manifests_are_rejected(changes, message):
    with pytest.raises(ManifestError, match=message):
        parse_manifest(variant(**changes))


def test_output_folder_setting_must_be_outside_git():
    settings = copy.deepcopy(AUTHOR["settings"])
    settings["output_dir"]["outside_git"] = False
    with pytest.raises(ManifestError, match="outside_git"):
        parse_manifest(variant(settings=settings))


def test_routines_cannot_call_external_tools():
    data = variant(
        tools={**AUTHOR["tools"], "send": "external"},
        routines=[{"id": "auto-send", "tool": "send", "every_minutes": 5}],
    )
    with pytest.raises(ManifestError, match="external-tier"):
        parse_manifest(data)


def test_admin_tools_are_not_model_tools():
    manifest = parse_manifest(variant(admin_tools=["validate"]))
    assert set(manifest.model_tools()) == {"find_examples", "scaffold"}


def test_load_manifest_reports_missing_and_broken_files(tmp_path):
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest(tmp_path)
    (tmp_path / "plugin.yaml").write_text("id: [unclosed")
    with pytest.raises(ManifestError, match="invalid YAML"):
        load_manifest(tmp_path)
    (tmp_path / "plugin.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ManifestError, match="must be a mapping"):
        load_manifest(tmp_path)


def test_coerce_setting_checks_types():
    spec = parse_manifest(
        variant(
            settings={
                **AUTHOR["settings"],
                "count": {"type": "integer", "default": 1},
                "mode": {"type": "enum", "choices": ["a", "b"], "default": "a"},
                "flag": {"type": "boolean", "default": False},
            }
        )
    ).settings
    assert coerce_setting("count", spec["count"], 3) == 3
    assert coerce_setting("mode", spec["mode"], "b") == "b"
    for key, bad in (("count", True), ("count", "3"), ("mode", "c"), ("flag", 1)):
        with pytest.raises(ManifestError):
            coerce_setting(key, spec[key], bad)


def test_widget_card_matches_the_design_example():
    manifest = parse_manifest(
        variant(
            widget_card={
                "title": "AUTHOR",
                "source": "validate",
                "value": "counts.urgent",
                "status": "{needs_reply} need a reply",
                "buttons": [{"label": "Find", "tool": "form-author.find_examples"}],
                "refresh": "every 5m",
            }
        )
    )
    card = manifest.widget_card
    assert (
        card.refresh_seconds == 300
        and manifest.button_tool(card.buttons[0].tool) == "find_examples"
    )
    assert (
        parse_manifest(
            variant(widget_card={"title": "t", "source": "validate"})
        ).widget_card.refresh_seconds
        is None
    )


def test_list_settings_are_cleaned_and_served_as_arrays():
    from bob_sdk.manifest import ManifestError, SettingSpec, coerce_setting

    spec = SettingSpec(type="list", default=[])
    assert coerce_setting("k", spec, ["  news   letter ", "", "News Letter", "[JIRA]"]) == [
        "news letter",
        "[JIRA]",
    ]
    for bad in ("one string", [1, 2], ["x" * 201]):
        with pytest.raises(ManifestError):
            coerce_setting("k", spec, bad)
    with pytest.raises(ManifestError):
        coerce_setting("k", spec, [f"w{i}" for i in range(201)])


def test_granted_tiers_need_a_named_folder_and_no_network():
    from bob_sdk.manifest import Tier

    assert Tier.READ_GRANTED.needs_grant and Tier.WRITE_GRANTED.needs_grant
    assert not Tier.READ.needs_grant and not Tier.EXTERNAL.needs_grant
    base = {"id": "ed", "name": "Ed", "version": "0.1.0", "mcp": "python s.py"}
    ok = parse_manifest(
        {**base, "tools": {"open": "read_granted"}, "permissions": {"network": "none"}}
    )
    assert ok.tools["open"] is Tier.READ_GRANTED
    with pytest.raises(ManifestError, match="may not use the network"):
        parse_manifest(
            {**base, "tools": {"open": "write_granted"}, "permissions": {"network": ["x.com"]}}
        )
    with pytest.raises(ManifestError, match="needs your approval"):
        parse_manifest(
            {
                **base,
                "tools": {"open": "write_granted"},
                "routines": [{"id": "r", "title": "t", "tool": "open", "every_minutes": 5}],
            }
        )
