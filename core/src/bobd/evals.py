"""Small golden sets that catch regressions when a model, prompt or chunking changes.

Golden sets are YAML files in ``evals/golden``. Each run is saved, so a later
run can be compared with the previous one and a drop raises an alert.

    # retrieval: evals/golden/retrieval_<collection>.yaml
    kind: retrieval
    collection: docs
    target: {recall_at_5: 0.9}
    cases:
      - question: which function returns the first non-empty value?
        expect: ["example_repo:functions.py:FIRST_OF"]     # chunk ids that answer it

    # tool choice: evals/golden/tool_choice.yaml
    kind: tool_choice
    target: {accuracy: 0.9}
    cases:
      - prompt: add buy milk to my list
        server: tasks
        tool: create
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.toolsets.abstract import ToolsetTool

from .agent import BrokerToolset, TurnState
from .broker import Caller, ToolDenied, ToolFailed
from .db import utcnow

if TYPE_CHECKING:
    from .app import Bob

log = logging.getLogger(__name__)

REGRESSION_POINTS = 0.03  # a drop of more than 3 points raises an alert


class EvalError(Exception):
    """A golden set is missing or malformed, or the eval could not run."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalCase(_Strict):
    question: str
    expect: list[str] = Field(min_length=1)


class ToolChoiceCase(_Strict):
    prompt: str
    server: str
    tool: str


class GoldenSet(_Strict):
    kind: Literal["retrieval", "tool_choice"]
    collection: str | None = None
    target: dict[str, float] = Field(default_factory=dict)
    cases: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class EvalResult:
    eval_id: str
    kind: str
    metrics: dict[str, float]
    target: dict[str, float]
    cases: int
    failures: list[dict[str, Any]] = field(default_factory=list)
    previous: dict[str, float] | None = None

    @property
    def passed(self) -> bool:
        return all(self.metrics.get(name, 0.0) >= value for name, value in self.target.items())

    @property
    def regressions(self) -> dict[str, float]:
        """Metrics that dropped by more than three points since the previous run."""
        if not self.previous:
            return {}
        return {
            name: round(self.previous[name] - value, 4)
            for name, value in self.metrics.items()
            if name in self.previous and self.previous[name] - value > REGRESSION_POINTS
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "eval": self.eval_id,
            "kind": self.kind,
            "cases": self.cases,
            "metrics": self.metrics,
            "target": self.target,
            "passed": self.passed,
            "previous": self.previous,
            "regressions": self.regressions,
            "failures": self.failures[:25],
        }


def golden_dir(bob: Bob) -> Path:
    return bob.config.base_dir / "evals" / "golden"


def available(bob: Bob) -> list[str]:
    folder = golden_dir(bob)
    return sorted(path.stem for path in folder.glob("*.yaml")) if folder.is_dir() else []


def load_golden(bob: Bob, eval_id: str) -> GoldenSet:
    path = golden_dir(bob) / f"{eval_id}.yaml"
    if not path.is_file():
        known = ", ".join(available(bob)) or "none"
        raise EvalError(f"no golden set {eval_id!r} (available: {known})")
    try:
        return GoldenSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as exc:
        raise EvalError(f"{path}: {exc}") from exc


# -- retrieval: recall@5 and MRR ------------------------------------------------


def score_retrieval(ranked: list[list[str]], expected: list[list[str]]) -> dict[str, float]:
    """recall@5 (any expected chunk in the top 5) and mean reciprocal rank."""
    if not ranked:
        return {"recall_at_5": 0.0, "mrr": 0.0}
    hits, reciprocal = 0, 0.0
    for results, wanted in zip(ranked, expected, strict=True):
        rank = next((i for i, chunk_id in enumerate(results[:5], 1) if chunk_id in wanted), None)
        if rank is not None:
            hits += 1
            reciprocal += 1.0 / rank
    return {"recall_at_5": round(hits / len(ranked), 4), "mrr": round(reciprocal / len(ranked), 4)}


async def run_retrieval(
    bob: Bob, golden: GoldenSet
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    if not golden.collection:
        raise EvalError("a retrieval golden set needs a 'collection'")
    cases = [RetrievalCase.model_validate(case) for case in golden.cases]
    ranked: list[list[str]] = []
    failures: list[dict[str, Any]] = []
    caller = Caller(kind="routine", name="eval", plugin="knowledge")
    for case in cases:
        try:
            data = await bob.broker.call(
                "knowledge", "search",
                {"collection": golden.collection, "query": case.question, "limit": 5}, caller,
            )  # fmt: skip
        except (ToolDenied, ToolFailed) as exc:
            raise EvalError(f"search failed: {exc}") from exc
        ids = [item["chunk_id"] for item in data.get("results", [])]
        ranked.append(ids)
        if not set(ids) & set(case.expect):
            failures.append({"question": case.question, "expected": case.expect, "got": ids})
    return score_retrieval(ranked, [case.expect for case in cases]), failures


# -- tool choice: the first tool the specialist would call ---------------------------


class _DryRunToolset(BrokerToolset):
    """Records the first tool call instead of running it."""

    def __init__(self, inner: BrokerToolset) -> None:
        super().__init__(inner._broker, inner._tools)
        self.first: tuple[str, str] | None = None

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[TurnState],
        tool: ToolsetTool[TurnState],
    ) -> Any:
        if self.first is None:
            plugin_id, tool_name, _ = self._tools[name]
            self.first = (plugin_id, tool_name)
        return json.dumps({"dry_run": True, "note": "evaluation only; nothing was changed"})


async def first_tool(bob: Bob, prompt: str) -> tuple[str, tuple[str, str] | None]:
    """Route a prompt and return (specialist, first tool the model picks), running nothing."""
    orchestrator = bob.orchestrator
    specialists = orchestrator.specialists()
    route = await bob.router.route(prompt, specialists, bob.models.spec("router"))
    specialist = orchestrator._pick(route, specialists)
    if specialist is None:
        return route.domain, None
    toolset = _DryRunToolset(orchestrator._toolset(specialist, prompt))
    agent: Agent[TurnState, str] = Agent(
        await orchestrator._model_factory(specialist.spec.model_role),
        instructions=specialist.prompt,
        toolsets=[toolset],
        deps_type=TurnState,
    )
    state = TurnState(caller=Caller(kind="model", name=specialist.id, plugin=specialist.id))
    # One tool call is all the eval needs, so the request limit is expected to trip.
    with contextlib.suppress(UsageLimitExceeded):
        await agent.run(prompt, deps=state, usage_limits=UsageLimits(request_limit=2))
    return specialist.id, toolset.first


async def run_tool_choice(
    bob: Bob, golden: GoldenSet
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    cases = [ToolChoiceCase.model_validate(case) for case in golden.cases]
    correct = 0
    failures: list[dict[str, Any]] = []
    for case in cases:
        try:
            specialist, picked = await first_tool(bob, case.prompt)
        except Exception as exc:
            raise EvalError(f"the model call failed: {exc}") from exc
        if picked == (case.server, case.tool):
            correct += 1
        else:
            failures.append(
                {
                    "prompt": case.prompt,
                    "expected": f"{case.server}.{case.tool}",
                    "got": ".".join(picked) if picked else "no tool",
                    "specialist": specialist,
                }
            )
    return {"accuracy": round(correct / len(cases), 4)}, failures


# -- running and saving -------------------------------------------------------------------


def previous_metrics(bob: Bob, eval_id: str) -> dict[str, float] | None:
    row = bob.db.query_one(
        "SELECT metrics FROM eval_runs WHERE eval_id=? ORDER BY id DESC LIMIT 1", (eval_id,)
    )
    return json.loads(row["metrics"]) if row else None


async def run_eval(bob: Bob, eval_id: str) -> EvalResult:
    golden = load_golden(bob, eval_id)
    if not golden.cases:
        raise EvalError(f"golden set {eval_id!r} has no cases yet; add some to run it")
    try:
        if golden.kind == "retrieval":
            metrics, failures = await run_retrieval(bob, golden)
        else:
            metrics, failures = await run_tool_choice(bob, golden)
    except ValidationError as exc:
        raise EvalError(f"golden set {eval_id!r}: {exc}") from exc
    result = EvalResult(
        eval_id=eval_id,
        kind=golden.kind,
        metrics=metrics,
        target=golden.target,
        cases=len(golden.cases),
        failures=failures,
        previous=previous_metrics(bob, eval_id),
    )
    bob.db.execute(
        "INSERT INTO eval_runs(eval_id, at, metrics, passed, detail) VALUES (?, ?, ?, ?, ?)",
        (eval_id, utcnow(), json.dumps(metrics), int(result.passed), json.dumps(failures[:25])),
    )
    return result


async def run_all(bob: Bob) -> dict[str, Any]:
    """The nightly job: run every golden set that has cases, and alert on regressions."""
    results: list[dict[str, Any]] = []
    alerts: list[str] = []
    for eval_id in available(bob):
        try:
            result = await run_eval(bob, eval_id)
        except EvalError as exc:
            results.append({"eval": eval_id, "skipped": str(exc)})
            continue
        results.append(result.as_dict())
        for name, drop in result.regressions.items():
            alerts.append(f"{eval_id}: {name} dropped {drop * 100:.1f} points")
        if not result.passed:
            alerts.append(f"{eval_id}: below target ({result.metrics})")
    if alerts:
        await bob.notify("Bob evals regressed", "; ".join(alerts)[:380])
    return {"ok": not alerts, "detail": "; ".join(alerts), "results": results}


def history(bob: Bob, eval_id: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
    sql, params = "SELECT id, eval_id, at, metrics, passed FROM eval_runs", []
    if eval_id:
        sql += " WHERE eval_id=?"
        params.append(eval_id)
    rows = bob.db.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
    return [
        {**dict(row), "metrics": json.loads(row["metrics"]), "passed": bool(row["passed"])}
        for row in rows
    ]
