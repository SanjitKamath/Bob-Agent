"""Traces (OpenTelemetry) and metrics (Prometheus) for every model and tool call."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from .config import TelemetryConfig

log = logging.getLogger(__name__)

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


class Telemetry:
    def __init__(self, config: TelemetryConfig, *, exporter: SpanExporter | None = None) -> None:
        self.registry = CollectorRegistry()
        self._provider = TracerProvider(
            resource=Resource.create({"service.name": config.service_name})
        )
        if exporter is None and config.otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(
                endpoint=f"{config.otlp_endpoint.rstrip('/')}/v1/traces", timeout=5
            )
        if exporter is not None:
            self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self.tracer = self._provider.get_tracer("bobd")

        def counter(name: str, doc: str, labels: list[str]) -> Counter:
            return Counter(name, doc, labels, registry=self.registry)

        def histogram(name: str, doc: str, labels: list[str]) -> Histogram:
            return Histogram(name, doc, labels, registry=self.registry, buckets=_LATENCY_BUCKETS)

        self._requests = counter(
            "bob_requests_total", "Requests handled", ["specialist", "outcome"]
        )
        self._request_seconds = histogram("bob_request_seconds", "Request latency", ["specialist"])
        self._tool_calls = counter(
            "bob_tool_calls_total", "Tool calls", ["plugin", "tool", "outcome"]
        )
        self._tool_seconds = histogram("bob_tool_seconds", "Tool latency", ["plugin", "tool"])
        self._tokens = counter("bob_model_tokens_total", "Model tokens", ["role", "direction"])
        self._model_seconds = histogram("bob_model_seconds", "Model call latency", ["role"])
        self._routine_runs = counter(
            "bob_routine_runs_total", "Routine runs", ["routine", "outcome"]
        )

    @property
    def provider(self) -> TracerProvider:
        return self._provider

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[trace.Span]:
        with self.tracer.start_as_current_span(name, attributes=attributes or {}) as span:
            yield span

    def request(self, specialist: str, outcome: str, seconds: float) -> None:
        self._requests.labels(specialist, outcome).inc()
        self._request_seconds.labels(specialist).observe(seconds)

    def tool_call(self, plugin: str, tool: str, outcome: str, seconds: float) -> None:
        self._tool_calls.labels(plugin, tool, outcome).inc()
        self._tool_seconds.labels(plugin, tool).observe(seconds)

    def model_call(self, role: str, seconds: float, input_tokens: int, output_tokens: int) -> None:
        self._model_seconds.labels(role).observe(seconds)
        self._tokens.labels(role, "input").inc(max(0, input_tokens))
        self._tokens.labels(role, "output").inc(max(0, output_tokens))

    def routine_run(self, routine: str, outcome: str) -> None:
        self._routine_runs.labels(routine, outcome).inc()

    def metrics_text(self) -> bytes:
        return generate_latest(self.registry)

    def shutdown(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:  # exporting on shutdown must never block exit
            log.debug("telemetry shutdown failed", exc_info=True)
