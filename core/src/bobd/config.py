"""Loading and validating ``config.yaml``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from bob_sdk.ollama import ModelSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CONFIG_ENV = "BOB_CONFIG"
DEFAULT_CONFIG_NAME = "config.yaml"

Role = Literal["router", "main", "coder", "vision", "embeddings"]
CHAT_ROLES: tuple[str, ...] = ("router", "main", "coder", "vision")


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OllamaConfig(_Strict):
    base_url: str = "http://127.0.0.1:11434"
    request_timeout_s: float = 300.0
    # How long a model stays loaded after Bob uses it. Loading gpt-oss:20b from disk
    # takes several seconds, so a short value makes the first reply after a pause slow.
    keep_alive: str = "30m"
    # Model roles bobd keeps loaded while it runs, so replies never wait for a load.
    # Each costs memory (main ~14 GB, router ~6 GB). Empty turns this off.
    keep_warm: list[str] = Field(default_factory=lambda: ["router", "main"])

    @field_validator("base_url")
    @classmethod
    def _local_only(cls, value: str) -> str:
        from urllib.parse import urlparse

        host = urlparse(value).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama must be reached on the loopback interface (local by default)")
        return value.rstrip("/")


ModelConfig = ModelSpec


def _default_models() -> dict[str, ModelConfig]:
    return {
        "router": ModelConfig(model="gemma4:e4b", num_ctx=8192, temperature=0.0),
        "main": ModelConfig(model="gpt-oss:20b", num_ctx=32768),
        "coder": ModelConfig(model="gpt-oss:20b", num_ctx=32768),
        "vision": ModelConfig(model="gemma4:e4b", num_ctx=8192),
        "embeddings": ModelConfig(model="qwen3-embedding:0.6b", num_ctx=16384, dimensions=1024),
    }


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("bobd binds to the loopback interface only")
        return value

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class PathsConfig(_Strict):
    data: str = "data"
    plugins: str = "plugins"
    # Where plugins added from wheel files are unpacked. Empty: adding plugins this way is off.
    user_site: str = ""


class PluginsConfig(_Strict):
    enabled: list[str] = Field(default_factory=list)
    # Also load plugins from installed packages that register a `bob.plugins` entry point.
    discover_installed: bool = True
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    disabled_tools: dict[str, list[str]] = Field(default_factory=dict)


class GitHubSource(_Strict):
    repo: str
    branch: str = "main"
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=list)
    authority: Literal["spec", "examples"] = "examples"

    @field_validator("repo")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("GitHub sources use HTTPS only, never SSH")
        return value

    @property
    def name(self) -> str:
        return self.repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


class FolderSource(_Strict):
    path: str
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=list)
    authority: Literal["spec", "examples"] = "examples"


class FilesSource(_Strict):
    authority: Literal["spec", "examples"] = "examples"


class SourceConfig(_Strict):
    """Exactly one connector per source entry."""

    github: GitHubSource | None = None
    folder: FolderSource | None = None
    files: FilesSource | None = None

    @model_validator(mode="after")
    def _one_connector(self) -> SourceConfig:
        if sum(x is not None for x in (self.github, self.folder, self.files)) != 1:
            raise ValueError("a source needs exactly one of github, folder, files")
        return self


class CollectionConfig(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    profile: str | None = None  # plugin parser, "package.module:ClassName"
    sources: list[SourceConfig] = Field(default_factory=list)


class GitHubAuth(_Strict):
    auth: Literal["gh", "token"] = "token"


class KnowledgeConfig(_Strict):
    root: str = "~/knowledge_base"
    github: GitHubAuth = Field(default_factory=GitHubAuth)
    poll_minutes: int = Field(default=15, ge=1)
    rerank_threshold: float = 0.0
    collections: list[CollectionConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> KnowledgeConfig:
        ids = [c.id for c in self.collections]
        if len(ids) != len(set(ids)):
            raise ValueError("knowledge.collections lists a collection id twice")
        return self


class SecurityConfig(_Strict):
    # "required": the OS-level read-only checks must pass or bobd shuts down.
    # "warn": report them, for development before the `bob` macOS user exists.
    os_isolation: Literal["required", "warn"] = "warn"
    protected_roots: list[str] = Field(default_factory=lambda: ["~/dev"])


class TelemetryConfig(_Strict):
    otlp_endpoint: str | None = None  # e.g. http://127.0.0.1:4318
    service_name: str = "bobd"


class CacheConfig(_Strict):
    """Answers reused for repeated questions. Only answers that cannot go stale are kept:
    no tools, or only knowledge searches whose collections have not changed since."""

    enabled: bool = True
    # 1.0 reuses only the same question (ignoring case and punctuation). Lower values
    # also reuse answers to questions that mean the same, by embedding similarity.
    similarity: float = Field(default=0.93, ge=0.5, le=1.0)
    max_age_hours: float = Field(default=24.0, gt=0)
    max_entries: int = Field(default=2000, ge=10)


class AgentConfig(_Strict):
    max_steps: int = Field(default=8, ge=1, le=32)
    max_tools_per_turn: int = Field(default=8, ge=1)
    history_turns: int = Field(default=8, ge=0)
    confirm_timeout_s: float = Field(default=300.0, gt=0)
    # Greetings and thanks skip routing and tools and are answered by the router model.
    quick_small_talk: bool = True
    # instant: greetings and thanks get a fixed reply at once; model: the router model writes it.
    small_talk: Literal["instant", "model"] = "instant"
    cache: CacheConfig = Field(default_factory=CacheConfig)


class Config(_Strict):
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    models: dict[str, ModelConfig] = Field(default_factory=_default_models)
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    routines: dict[str, dict[str, Any]] = Field(default_factory=dict)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    # Set by load_config; the folder config.yaml lives in.
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)

    @field_validator("models")
    @classmethod
    def _roles(cls, value: dict[str, ModelConfig]) -> dict[str, ModelConfig]:
        merged = _default_models()
        unknown = set(value) - set(merged)
        if unknown:
            raise ValueError(f"unknown model roles: {sorted(unknown)}")
        merged.update(value)
        return merged

    def resolve(self, path: str) -> Path:
        """Resolve a configured path against the folder holding config.yaml."""
        expanded = Path(os.path.expanduser(path))
        return (expanded if expanded.is_absolute() else self.base_dir / expanded).resolve()

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.paths.data)

    @property
    def plugins_dir(self) -> Path:
        return self.resolve(self.paths.plugins)

    @property
    def user_site_dir(self) -> Path | None:
        return self.resolve(self.paths.user_site) if self.paths.user_site else None

    @property
    def knowledge_root(self) -> Path:
        return self.resolve(self.knowledge.root)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bob.sqlite"


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate config.yaml: explicit path, $BOB_CONFIG, then upwards from the cwd."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        candidate = folder / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no {DEFAULT_CONFIG_NAME} found from {here} upwards; set ${CONFIG_ENV} or pass --config"
    )


def local_override_for(file: Path) -> Path:
    """``config.yaml`` -> ``config.local.yaml``: your own settings, kept out of git."""
    return file.with_name(f"{file.stem}.local{file.suffix}")


def _read_mapping(file: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"{file}: cannot read ({exc.strerror})") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file}: invalid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file}: the top level must be a mapping")
    return raw


def _by_id(items: list[Any]) -> bool:
    return bool(items) and all(isinstance(item, dict) and "id" in item for item in items)


def merge_config(base: Any, over: Any) -> Any:
    """Lay ``over`` on ``base``: mappings merge, lists of ``id`` entries merge by id, anything
    else is replaced. This is how ``config.local.yaml`` adds to ``config.yaml``."""
    if isinstance(base, dict) and isinstance(over, dict):
        merged = dict(base)
        for key, value in over.items():
            merged[key] = merge_config(base[key], value) if key in base else value
        return merged
    if isinstance(base, list) and isinstance(over, list) and _by_id(base) and _by_id(over):
        merged_list = list(base)
        positions = {item["id"]: index for index, item in enumerate(merged_list)}
        for item in over:
            if item["id"] in positions:
                merged_list[positions[item["id"]]] = merge_config(
                    merged_list[positions[item["id"]]], item
                )
            else:
                merged_list.append(item)
        return merged_list
    return over


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    file = find_config(path)
    raw = _read_mapping(file)
    local = local_override_for(file)
    if local.is_file():
        raw = merge_config(raw, _read_mapping(local))
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{file}: {problems}") from exc
    config.base_dir = file.parent
    return config
