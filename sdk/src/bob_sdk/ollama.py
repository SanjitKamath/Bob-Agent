"""A thin client for Ollama's native API.

Used where the OpenAI-compatible endpoint is not enough: JSON-schema
constrained output (the router, the quarantined summariser), embeddings with
explicit ``num_ctx`` and dimensions, and model management.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class ModelSpec(BaseModel):
    """One model role: the Ollama tag and the options sent with every call."""

    model_config = ConfigDict(extra="forbid")

    model: str
    num_ctx: int = Field(default=8192, ge=1024)
    dimensions: int | None = Field(default=None, ge=32)  # embeddings only
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    @field_validator("model")
    @classmethod
    def _no_cloud(cls, value: str) -> str:
        # ":cloud" tags route requests off the machine.
        if "cloud" in value.rsplit(":", 1)[-1].split("-"):
            raise ValueError("cloud model tags are not allowed")
        return value


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns an error."""


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        keep_alive: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # How long Ollama keeps a model loaded after a request from this client
        # (e.g. "30m"). None leaves Ollama's own default, five minutes.
        self.keep_alive = keep_alive
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.base_url}: {exc.__class__.__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("error", response.text)
            except ValueError:
                message = response.text
            raise OllamaError(f"Ollama {path} failed ({response.status_code}): {message}")
        return response.json()

    # -- models -----------------------------------------------------------

    async def version(self) -> str:
        return str((await self._request("GET", "/api/version")).get("version", ""))

    async def installed_models(self) -> list[str]:
        data = await self._request("GET", "/api/tags")
        return sorted(item["name"] for item in data.get("models", []))

    async def loaded_models(self) -> list[str]:
        data = await self._request("GET", "/api/ps")
        return sorted(item["name"] for item in data.get("models", []))

    async def create_alias(self, name: str, base: str, parameters: dict[str, Any]) -> None:
        """Create a derived model with baked-in parameters (such as ``num_ctx``).

        The OpenAI-compatible endpoint ignores per-request options, so each chat
        role gets an alias whose context size is part of the model itself.
        """
        await self._request(
            "POST",
            "/api/create",
            json={"model": name, "from": base, "parameters": parameters, "stream": False},
        )

    # -- chat -------------------------------------------------------------

    async def chat(
        self,
        model: ModelSpec,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        think: bool | None = None,
    ) -> str:
        options: dict[str, Any] = {"num_ctx": model.num_ctx}
        if model.temperature is not None:
            options["temperature"] = model.temperature
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if schema is not None:
            payload["format"] = schema
        if think is not None:
            payload["think"] = think
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        data = await self._request("POST", "/api/chat", json=payload)
        return str(data.get("message", {}).get("content", ""))

    async def chat_structured(
        self,
        model: ModelSpec,
        messages: list[dict[str, Any]],
        output_type: type[T],
        *,
        attempts: int = 2,
    ) -> T:
        """Chat with output forced to ``output_type``'s JSON schema, then validate it."""
        schema = output_type.model_json_schema()
        last_error: Exception | None = None
        for _ in range(max(1, attempts)):
            text = await self.chat(model, messages, schema=schema, think=False)
            try:
                return output_type.model_validate(json.loads(text))
            except (ValueError, ValidationError) as exc:
                last_error = exc
        raise OllamaError(
            f"{model.model} did not return valid {output_type.__name__}: {last_error}"
        )

    async def warm(self, name: str, options: dict[str, Any] | None = None) -> None:
        """Load a chat model (or refresh how long it stays loaded) without generating.

        ``options`` must match what later requests send: a different ``num_ctx``
        makes Ollama load the model again.
        """
        payload: dict[str, Any] = {"model": name, "prompt": "", "stream": False}
        if options:
            payload["options"] = options
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        await self._request("POST", "/api/generate", json=payload)

    # -- embeddings -------------------------------------------------------

    async def embed(self, model: ModelSpec, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": model.model,
            "input": texts,
            "truncate": True,
            "options": {"num_ctx": model.num_ctx},
        }
        if model.dimensions:
            payload["dimensions"] = model.dimensions
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        data = await self._request("POST", "/api/embed", json=payload)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("Ollama returned a different number of embeddings than inputs")
        return vectors
