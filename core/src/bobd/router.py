"""Routing: a small, fast model returns a typed Route for every request."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from bob_sdk.manifest import SpecialistSpec
from bob_sdk.ollama import ModelSpec, OllamaClient, OllamaError
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

GENERAL = "general"


@dataclass(frozen=True)
class Specialist:
    """A specialist contributed by an enabled plugin."""

    id: str  # the plugin id; also the route domain
    name: str
    description: str
    spec: SpecialistSpec
    prompt: str
    servers: tuple[str, ...]  # plugin ids whose tools it sees, its own first


class Route(BaseModel):
    domain: str
    needs_tools: bool = True
    risk: Literal["low", "medium", "high"] = "low"
    # Set when you explicitly ask Bob to remember something.
    memory_fact: str | None = None
    # A greeting, thanks or goodbye: answered at once by the router model, no tools.
    small_talk: bool = False
    routed_by: Literal["model", "keywords", "default", "quick", "cache"] = "model"


SYSTEM_PROMPT = """You route one request to the specialist that should handle it.

Specialists:
{specialists}

Rules:
- domain: the id of the best specialist. Use "{general}" when none fits.
- needs_tools: false only for small talk or questions answerable without looking anything up.
- risk: "high" if the request sends, deletes or changes something outside this computer; \
"medium" if it creates or changes the user's own data; otherwise "low".
- memory_fact: only when the user explicitly asks to remember something, the fact to store \
as one sentence. Otherwise null."""

_WORD = re.compile(r"[a-z0-9]+")

# Short messages that are only a greeting, thanks or goodbye. Anything with a request in
# it ("hi, what's on my calendar?") is longer or contains other words, and is routed.
_SMALL_TALK = re.compile(
    r"^(?:(?:hi|hello|hey|hiya|yo|howdy|hola|namaste|good (?:morning|afternoon|evening|night)"
    r"|thanks|thank you|thank u|thx|ty|cheers|great|awesome|nice|cool|perfect|ok|okay|got it"
    r"|bye|goodbye|see you|see ya|later|how are you|how s it going|how are things"
    r"|what s up|whats up|sup)(?: (?:bob|there|so much|a lot|again|doing|today|man|mate))*)+$"
)


def is_small_talk(text: str) -> bool:
    """True for a message that is only a greeting, thanks or goodbye."""
    words = _WORD.findall(text.lower().replace("'", " "))
    return 0 < len(words) <= 8 and _SMALL_TALK.match(" ".join(words)) is not None


_KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("thanks", re.compile(r"\b(?:thanks|thank you|thank u|thx|ty|cheers)\b")),
    ("bye", re.compile(r"\b(?:bye|goodbye|see you|see ya|later|good night)\b")),
    (
        "how",
        re.compile(r"\b(?:how are you|how s it going|how are things|what s up|whats up|sup)\b"),
    ),
    ("ack", re.compile(r"^(?:great|awesome|nice|cool|perfect|ok|okay|got it)\b")),
]
_REPLIES = {
    "hello": [
        "Hi! What can I do for you?",
        "Hello! What are we working on?",
        "Hey! What do you need?",
    ],
    "thanks": ["You're welcome!", "Any time.", "Glad that helped."],
    "bye": ["See you later!", "Bye! I'll be here when you need me.", "Talk soon."],
    "how": [
        "All good here, and ready when you are. What do you need?",
        "Doing well, thanks! What can I help with?",
    ],
    "ack": ["Great. Anything else?", "Okay. Just ask when you need something."],
}


def small_talk_reply(text: str, abilities: list[str], *, first: bool, seed: int = 0) -> str:
    """A reply to a greeting, thanks or goodbye, without a model: it takes no time at all."""
    words = " ".join(_WORD.findall(text.lower().replace("'", " ")))
    kind = next((name for name, pattern in _KINDS if pattern.search(words)), "hello")
    options = _REPLIES[kind]
    reply = options[seed % len(options)]
    if first and kind in ("hello", "how") and abilities:
        shown = abilities[:4]
        listed = ", ".join(shown[:-1]) + (" and " if len(shown) > 1 else "") + shown[-1]
        reply += f" I can help with {listed}."
    return reply


def _word_forms(text: str) -> set[str]:
    """The words in a request, plus their singular forms: "emails" also matches "email"."""
    forms: set[str] = set()
    for word in _WORD.findall(text.lower()):
        forms.add(word)
        if len(word) > 3 and word.endswith("es"):
            forms.add(word[:-2])
        if len(word) > 3 and word.endswith("s"):
            forms.add(word[:-1])
    return forms


def match_routes(text: str, candidates: list[tuple[str, list[str]]]) -> str | None:
    """The id whose route hints best match the request, or None."""
    lowered = text.lower()
    words = _word_forms(text)
    best: tuple[int, str] | None = None
    for candidate_id, hints in candidates:
        score = 0
        for hint in hints:
            hint = hint.lower()
            if (" " in hint and hint in lowered) or hint in words:
                score += 2 if " " in hint else 1
        if score and (best is None or score > best[0]):
            best = (score, candidate_id)
    return best[1] if best else None


def _schema(domains: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "enum": domains},
            "needs_tools": {"type": "boolean"},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "memory_fact": {"type": ["string", "null"]},
        },
        "required": ["domain", "needs_tools", "risk", "memory_fact"],
    }


def keyword_route(text: str, specialists: list[Specialist]) -> Route:
    """Fallback when the router model is unavailable: match the manifests' route hints."""
    found = match_routes(text, [(s.id, list(s.spec.routes)) for s in specialists])
    if found is None:
        return Route(domain=GENERAL, routed_by="default")
    return Route(domain=found, routed_by="keywords")


class Router:
    def __init__(self, ollama: OllamaClient) -> None:
        self._ollama = ollama

    async def route(self, text: str, specialists: list[Specialist], spec: ModelSpec) -> Route:
        if not specialists:
            return Route(domain=GENERAL, routed_by="default")
        domains = [s.id for s in specialists]
        if GENERAL not in domains:
            domains.append(GENERAL)
        listing = "\n".join(
            f'- "{s.id}": {s.description or s.name}. Typical topics: {", ".join(s.spec.routes)}'
            for s in specialists
        )
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(specialists=listing, general=GENERAL),
            },
            {"role": "user", "content": text[:4000]},
        ]
        try:
            raw = await self._ollama.chat(spec, messages, schema=_schema(domains), think=False)
            route = Route.model_validate(json.loads(raw))
        except (OllamaError, ValueError, ValidationError) as exc:
            log.warning("router model failed (%s); using keyword routing", exc)
            return keyword_route(text, specialists)
        if route.domain not in domains:
            return keyword_route(text, specialists)
        fact = (route.memory_fact or "").strip()
        route.memory_fact = fact if fact and fact.lower() not in {"null", "none"} else None
        return route
