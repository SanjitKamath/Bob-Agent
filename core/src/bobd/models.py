"""Model roles: which Ollama model does which job, and how to reach it."""

from __future__ import annotations

import logging
import re
from typing import Any

from bob_sdk.ollama import ModelSpec, OllamaClient, OllamaError

from .config import CHAT_ROLES, Config
from .db import Database
from .modelqueue import DEFAULT_LIMITS, MAX_LIMIT, MIN_LIMIT

log = logging.getLogger(__name__)

_SCOPE = "models"
_LIMITS_SCOPE, _LIMITS_KEY = "scheduler", "limits"
_UNSAFE = re.compile(r"[^a-z0-9]+")


class ModelRegistry:
    """Resolves a role (router, main, coder, vision, embeddings) to a model.

    ``config.yaml`` gives the defaults; choices made in Settings are stored in
    the database and win.
    """

    def __init__(self, config: Config, db: Database, ollama: OllamaClient) -> None:
        self._config = config
        self._db = db
        self.ollama = ollama
        self._aliases: dict[str, str] = {}
        # Set when the models bobd keeps warm do not all fit in memory together.
        self.memory_note = ""

    def roles(self) -> dict[str, ModelSpec]:
        overrides = self._db.get_settings(_SCOPE)
        resolved: dict[str, ModelSpec] = {}
        for role, spec in self._config.models.items():
            data = spec.model_dump()
            data.update(overrides.get(role, {}))
            resolved[role] = ModelSpec.model_validate(data)
        return resolved

    def spec(self, role: str) -> ModelSpec:
        try:
            return self.roles()[role]
        except KeyError:
            raise KeyError(f"unknown model role {role!r}") from None

    async def set_role(self, role: str, model: str) -> ModelSpec:
        """Point a role at an installed model. Changes apply immediately."""
        if role not in self._config.models:
            raise ValueError(f"unknown model role {role!r}")
        installed = await self.ollama.installed_models()
        if model not in installed:
            raise ValueError(f"{model!r} is not installed in Ollama (run: ollama pull {model})")
        updated = ModelSpec.model_validate({**self.spec(role).model_dump(), "model": model})
        self._db.set_setting(_SCOPE, role, {"model": updated.model})
        self._aliases.pop(role, None)
        return updated

    def alias_name(self, role: str, spec: ModelSpec) -> str:
        return f"bob-{role}-{_UNSAFE.sub('-', spec.model.lower()).strip('-')}-{spec.num_ctx}"

    async def chat_model_name(self, role: str) -> str:
        """The Ollama model to use for a chat role through the OpenAI-compatible API.

        That API ignores per-request ``num_ctx``, and Ollama's small default
        context silently truncates long prompts. So each role gets a derived
        model with its context size baked in, created on first use.
        """
        if role not in CHAT_ROLES:
            raise ValueError(f"{role!r} is not a chat role")
        spec = self.spec(role)
        alias = self.alias_name(role, spec)
        if self._aliases.get(role) == alias:
            return alias
        try:
            installed = await self.ollama.installed_models()
            if alias not in installed and f"{alias}:latest" not in installed:
                parameters: dict[str, Any] = {"num_ctx": spec.num_ctx}
                if spec.temperature is not None:
                    parameters["temperature"] = spec.temperature
                await self.ollama.create_alias(alias, spec.model, parameters)
                log.info("created model alias %s (num_ctx=%d)", alias, spec.num_ctx)
        except OllamaError as exc:
            raise OllamaError(f"cannot prepare the {role} model {spec.model!r}: {exc}") from exc
        self._aliases[role] = alias
        return alias

    # -- how many requests each model runs at once ----------------------------------

    def limits(self) -> dict[str, int]:
        stored = self._db.get_setting(_LIMITS_SCOPE, _LIMITS_KEY, {}) or {}
        return {
            role: max(MIN_LIMIT, min(MAX_LIMIT, int(stored.get(role, DEFAULT_LIMITS.get(role, 2)))))
            for role in self._config.models
        }

    def set_limits(self, changes: dict[str, int]) -> dict[str, int]:
        current = self.limits()
        for role, value in changes.items():
            if role not in current:
                raise ValueError(f"unknown model role {role!r}")
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"the limit for {role} must be a whole number")
            if not MIN_LIMIT <= value <= MAX_LIMIT:
                raise ValueError(f"the limit for {role} must be from {MIN_LIMIT} to {MAX_LIMIT}")
            current[role] = value
        self._db.set_setting(_LIMITS_SCOPE, _LIMITS_KEY, current)
        return current

    def queue_for(self, model: str) -> tuple[str, int]:
        """The queue a request to ``model`` waits in, and that queue's limit.

        Roles that use the same Ollama model share one queue (main and coder are both
        gpt-oss:20b, through their own aliases), and its limit is the largest of theirs.
        """
        wanted = {model, model.removesuffix(":latest")}
        roles = self.roles()
        matched = [
            (role, spec)
            for role, spec in roles.items()
            if wanted
            & {spec.model, spec.model.removesuffix(":latest"), self.alias_name(role, spec)}
        ]
        if not matched:
            return model.removesuffix(":latest"), DEFAULT_LIMITS["main"]
        limits = self.limits()
        base = matched[0][1].model
        # Every role on that base model shares its queue, not just the one that matched.
        sharing = [role for role, spec in roles.items() if spec.model == base]
        return base, max(limits[role] for role in sharing)

    async def health(self) -> dict[str, Any]:
        """What the menu bar shows: which role models are installed and loaded."""
        try:
            installed = await self.ollama.installed_models()
            loaded = await self.ollama.loaded_models()
        except OllamaError as exc:
            return {"ok": False, "error": str(exc), "roles": {}}
        roles = {
            role: {
                "model": spec.model,
                "num_ctx": spec.num_ctx,
                "installed": spec.model in installed,
            }
            for role, spec in self.roles().items()
        }
        limits = self.limits()
        for role, entry in roles.items():
            entry["parallel"] = limits.get(role, 1)
        return {
            "ok": all(entry["installed"] for entry in roles.values()),
            "roles": roles,
            "installed": installed,
            "loaded": loaded,
            "memory": self.memory_note or "the models bobd keeps warm fit in memory together",
        }
