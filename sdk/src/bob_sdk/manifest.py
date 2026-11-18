"""The plugin manifest (``plugin.yaml``) schema.

A manifest is the single source of truth for what a plugin may do. The core
refuses to load a plugin whose manifest fails this schema, so the permission
vocabulary below is enforced before any plugin code runs.
"""

from __future__ import annotations

import re
import shlex
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MANIFEST_FILENAME = "plugin.yaml"

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SETTING_REF_RE = re.compile(r"^settings\.([a-z][a-z0-9_]*)$")


class ManifestError(ValueError):
    """Raised when a manifest is missing, unreadable or invalid."""


class Tier(StrEnum):
    """The fixed permission vocabulary.

    There is deliberately no tier for changing code, committing, pushing or
    loading into a server. Anything outside this enum fails validation.
    """

    READ = "read"
    DRAFT = "draft"  # Bob's own data only
    WRITE_OUTPUT = "write_output"  # only folders the plugin declares, never in a git repo
    # Files inside a folder you named in the chat and approved for that chat only. Reading never
    # touches credentials; changing never touches a git repository or a system folder.
    READ_GRANTED = "read_granted"
    WRITE_GRANTED = "write_granted"
    EXTERNAL = "external"  # acts outside the machine; always asks first

    @property
    def needs_confirmation(self) -> bool:
        return self is Tier.EXTERNAL

    @property
    def needs_grant(self) -> bool:
        """The call names a path, and the core checks it against the folders granted to the chat."""
        return self in (Tier.READ_GRANTED, Tier.WRITE_GRANTED)


ModelRole = Literal["router", "main", "coder", "vision"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpecialistSpec(_Strict):
    """A prompt, a model role and routing hints, so the router can send work here."""

    name: str | None = None
    description: str = ""
    prompt: str
    model_role: ModelRole = "main"
    routes: list[str] = Field(min_length=1)
    # MCP servers (plugin ids) whose tools this specialist sees. Its own plugin
    # is always included.
    servers: list[str] = Field(default_factory=list)


class CollectionSpec(_Strict):
    id: str
    parser: str | None = None  # "package.module:ClassName"

    @field_validator("parser")
    @classmethod
    def _parser_ref(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[\w.]+:[A-Za-z_]\w*", value):
            raise ValueError("parser must look like 'package.module:ClassName'")
        return value


class KnowledgeSpec(_Strict):
    provides: list[CollectionSpec] = Field(default_factory=list)
    # Collections this plugin may search. "*" means every collection.
    uses: list[str] = Field(default_factory=list)

    def may_read(self, collection: str) -> bool:
        return "*" in self.uses or collection in self.uses


class SettingSpec(_Strict):
    # "list" is a list of short strings, edited one per line in the app.
    type: Literal["path", "string", "integer", "number", "boolean", "enum", "list"]
    default: Any = None
    title: str | None = None
    description: str = ""
    choices: list[str] | None = None
    outside_git: bool = False
    secret: bool = False

    @model_validator(mode="after")
    def _check(self) -> SettingSpec:
        if self.type == "enum" and not self.choices:
            raise ValueError("an enum setting needs 'choices'")
        if self.outside_git and self.type != "path":
            raise ValueError("'outside_git' only applies to path settings")
        if self.secret and self.default is not None:
            raise ValueError("a secret setting cannot carry a default")
        if self.type == "list" and self.secret:
            raise ValueError("a list setting cannot be a secret")
        return self


class RoutineSpec(_Strict):
    """A scheduled or event-driven job.

    It either calls one of the plugin's tools, or sends ``prompt`` to the
    plugin's specialist, so a routine runs the same agents without you asking.
    """

    id: str
    title: str = ""
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = None
    card: str | None = None  # prompt routines: the kind of panel card the answer becomes
    cron: str | None = None  # five-field crontab expression, local time
    every_minutes: int | None = Field(default=None, ge=1)
    between: tuple[str, str] | None = None  # ("08:00", "20:00") window for every_minutes
    on_event: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _one_trigger(self) -> RoutineSpec:
        if (self.tool is None) == (self.prompt is None):
            raise ValueError("a routine needs exactly one of tool, prompt")
        if self.prompt is not None and self.args:
            raise ValueError("'args' only applies to tool routines")
        triggers = [self.cron, self.every_minutes, self.on_event]
        if sum(t is not None for t in triggers) != 1:
            raise ValueError("a routine needs exactly one of cron, every_minutes, on_event")
        if self.cron is not None and len(self.cron.split()) != 5:
            raise ValueError("cron must have five fields: minute hour day month weekday")
        if self.between is not None and self.every_minutes is None:
            raise ValueError("'between' only applies to every_minutes routines")
        if self.between is not None:
            for value in self.between:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
                    raise ValueError("'between' times must be HH:MM")
        return self


class WidgetButton(_Strict):
    label: str
    tool: str  # one of the plugin's own tools: "triage" or "mail.triage"
    args: dict[str, Any] = Field(default_factory=dict)


class WidgetCard(_Strict):
    """A card for the desktop widget and the menu bar dropdown.

    widget_card:
      title: Mail
      value: counts.urgent              # big number
      status: "{needs_reply} need a reply"
      buttons:
        - {label: Triage now, tool: mail.triage}
      refresh: on_event                 # or: every 5m
    """

    title: str
    # Read-tier tool whose result feeds the card, e.g. {"counts": {"urgent": 3}}.
    source: str = "summary"
    value: str | None = None  # dotted path into the source tool's result
    status: str = ""  # text with {name} placeholders, filled from the result's counts
    buttons: list[WidgetButton] = Field(default_factory=list)
    refresh: str = "on_event"

    @field_validator("refresh")
    @classmethod
    def _refresh(cls, value: str) -> str:
        if value != "on_event" and not re.fullmatch(r"every [1-9]\d*[smh]", value):
            raise ValueError("refresh must be 'on_event' or like 'every 5m'")
        return value

    @property
    def refresh_seconds(self) -> int | None:
        """Seconds between refreshes, or None when the card refreshes on events."""
        if self.refresh == "on_event":
            return None
        amount, unit = int(self.refresh[6:-1]), self.refresh[-1]
        return amount * {"s": 1, "m": 60, "h": 3600}[unit]


class Permissions(_Strict):
    # "none", or the list of hosts the plugin may reach.
    network: Literal["none"] | list[str] = "none"
    # References to path settings, e.g. "settings.output_dir".
    write: list[str] = Field(default_factory=list)

    @field_validator("write")
    @classmethod
    def _write_refs(cls, value: list[str]) -> list[str]:
        for ref in value:
            if not _SETTING_REF_RE.match(ref):
                raise ValueError(f"write entry {ref!r} must look like 'settings.<name>'")
        return value


class Manifest(_Strict):
    id: str
    name: str
    version: str
    description: str = ""
    mcp: str  # command that starts the plugin's MCP server over stdio
    specialist: SpecialistSpec | None = None
    tools: dict[str, Tier] = Field(default_factory=dict)
    # Tools only you, the app and routines may call. They are never offered to a model.
    admin_tools: list[str] = Field(default_factory=list)
    knowledge: KnowledgeSpec = Field(default_factory=KnowledgeSpec)
    # Other plugins' tools this plugin's server may call through the core, as
    # "plugin.tool". The core refuses anything not listed here.
    calls: list[str] = Field(default_factory=list)
    routines: list[RoutineSpec] = Field(default_factory=list)
    settings: dict[str, SettingSpec] = Field(default_factory=dict)
    widget_card: WidgetCard | None = None
    permissions: Permissions = Field(default_factory=Permissions)
    default_enabled: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id must be lower-case words joined by hyphens")
        return value

    @field_validator("mcp")
    @classmethod
    def _mcp(cls, value: str) -> str:
        if not shlex.split(value):
            raise ValueError("mcp command is empty")
        return value

    @field_validator("tools")
    @classmethod
    def _tool_names(cls, value: dict[str, Tier]) -> dict[str, Tier]:
        for name in value:
            if not _TOOL_RE.match(name):
                raise ValueError(f"tool name {name!r} must be snake_case")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> Manifest:
        for ref in self.permissions.write:
            key = _SETTING_REF_RE.match(ref).group(1)  # type: ignore[union-attr]
            spec = self.settings.get(key)
            if spec is None or spec.type != "path":
                raise ValueError(
                    f"permissions.write refers to {ref!r}, which is not a path setting"
                )
            if not spec.outside_git:
                raise ValueError(f"output folder setting {key!r} must set outside_git: true")
        if Tier.WRITE_OUTPUT in self.tools.values() and not self.permissions.write:
            raise ValueError("a write_output tool needs a folder under permissions.write")
        if (
            any(tier.needs_grant for tier in self.tools.values())
            and self.permissions.network != "none"
        ):
            raise ValueError(
                "a plugin whose tools read or change your files may not use the network"
            )
        for target in self.calls:
            if not re.fullmatch(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*", target):
                raise ValueError(f"calls entry {target!r} must look like 'plugin.tool'")
        for name in self.admin_tools:
            if name not in self.tools:
                raise ValueError(f"admin_tools lists undeclared tool {name!r}")
        for routine in self.routines:
            if routine.prompt is not None:
                if self.specialist is None:
                    raise ValueError(f"routine {routine.id!r} has a prompt but no specialist")
                continue
            if routine.tool not in self.tools:
                raise ValueError(f"routine {routine.id!r} calls undeclared tool {routine.tool!r}")
            if self.tools[routine.tool].needs_confirmation:
                raise ValueError(f"routine {routine.id!r} may not call an external-tier tool")
            if self.tools[routine.tool].needs_grant:
                raise ValueError(
                    f"routine {routine.id!r} may not call a tool that needs your approval"
                )
        routine_ids = [routine.id for routine in self.routines]
        if len(routine_ids) != len(set(routine_ids)):
            raise ValueError("two routines share an id")
        if self.widget_card is not None:
            if self.tools.get(self.widget_card.source) is not Tier.READ:
                raise ValueError("widget_card.source must be a declared read-tier tool")
            for button in self.widget_card.buttons:
                if self.button_tool(button.tool) not in self.tools:
                    raise ValueError(f"widget_card button calls undeclared tool {button.tool!r}")
                if self.tools.get(self.button_tool(button.tool), Tier.READ).needs_grant:
                    raise ValueError(
                        f"widget_card button {button.tool!r} may not call a tool that reads or "
                        "changes files in a folder you name"
                    )
        ids = [c.id for c in self.knowledge.provides]
        if len(ids) != len(set(ids)):
            raise ValueError("knowledge.provides lists a collection twice")
        return self

    def button_tool(self, reference: str) -> str:
        """The tool a widget button names; "mail.triage" and "triage" mean the same here."""
        prefix = f"{self.id}."
        return reference[len(prefix) :] if reference.startswith(prefix) else reference

    def model_tools(self) -> dict[str, Tier]:
        """Tools a model may be offered: everything except admin tools."""
        return {name: tier for name, tier in self.tools.items() if name not in self.admin_tools}

    @property
    def command(self) -> list[str]:
        return shlex.split(self.mcp)

    @property
    def write_setting_keys(self) -> list[str]:
        return [ref.split(".", 1)[1] for ref in self.permissions.write]

    def default_settings(self) -> dict[str, Any]:
        return {
            key: (list(spec.default or []) if spec.type == "list" else spec.default)
            for key, spec in self.settings.items()
        }

    def settings_json_schema(self) -> dict[str, Any]:
        """JSON Schema the app turns into a settings form, with no Swift changes."""
        json_types = {
            "path": "string",
            "string": "string",
            "enum": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "list": "array",
        }
        properties: dict[str, Any] = {}
        for key, spec in self.settings.items():
            prop: dict[str, Any] = {
                "type": json_types[spec.type],
                "title": spec.title or key.replace("_", " ").capitalize(),
            }
            if spec.description:
                prop["description"] = spec.description
            if spec.default is not None:
                prop["default"] = spec.default
            if spec.type == "path":
                prop["format"] = "path"
            if spec.type == "enum":
                prop["enum"] = list(spec.choices or [])
            if spec.type == "list":
                prop["items"] = {"type": "string"}
            if spec.secret:
                prop["writeOnly"] = True
            properties[key] = prop
        return {"type": "object", "properties": properties, "additionalProperties": False}


def parse_manifest(data: Any, *, source: str = "<manifest>") -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: manifest must be a mapping")
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ManifestError(f"{source}: {problems}") from exc


def load_manifest(path: Path) -> Manifest:
    """Load ``plugin.yaml`` from a file, or from the plugin folder containing it."""
    file = path / MANIFEST_FILENAME if path.is_dir() else path
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"{file}: cannot read manifest ({exc.strerror})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{file}: invalid YAML ({exc})") from exc
    return parse_manifest(data, source=str(file))


MAX_LIST_ENTRIES = 200
MAX_LIST_ENTRY = 200


def coerce_setting(key: str, spec: SettingSpec, value: Any) -> Any:
    """Validate one settings value against its spec and return the normalised value."""
    if value is None:
        return None
    match spec.type:
        case "path" | "string":
            if not isinstance(value, str):
                raise ManifestError(f"setting {key!r} must be a string")
            return value
        case "enum":
            if value not in (spec.choices or []):
                raise ManifestError(f"setting {key!r} must be one of {spec.choices}")
            return value
        case "boolean":
            if not isinstance(value, bool):
                raise ManifestError(f"setting {key!r} must be true or false")
            return value
        case "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ManifestError(f"setting {key!r} must be an integer")
            return value
        case "number":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ManifestError(f"setting {key!r} must be a number")
            return float(value)
        case "list":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ManifestError(f"setting {key!r} must be a list of text entries")
            entries: list[str] = []
            for item in value:
                item = " ".join(item.split())
                if len(item) > MAX_LIST_ENTRY:
                    raise ManifestError(
                        f"setting {key!r}: entries can be at most {MAX_LIST_ENTRY} characters"
                    )
                if item and item.casefold() not in {e.casefold() for e in entries}:
                    entries.append(item)
            if len(entries) > MAX_LIST_ENTRIES:
                raise ManifestError(f"setting {key!r} can hold at most {MAX_LIST_ENTRIES} entries")
            return entries
    raise ManifestError(f"setting {key!r} has unknown type {spec.type!r}")
