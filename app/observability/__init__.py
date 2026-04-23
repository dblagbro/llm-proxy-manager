"""Observability — Prometheus metrics + OpenTelemetry GenAI spans.

Wave 1 #1. Two layers:
- `prometheus.py`: /metrics endpoint with request/token/cost/TTFT/CB counters
- `otel.py`: GenAI semconv spans, no-op when OTLP endpoint unset

Both are always importable; exporting is gated on env vars so dev/tests
work without any collector infrastructure.
"""
from app.observability.prometheus import (
    metrics_response,
    observe_request,
    observe_ttft,
    observe_circuit_breaker_state,
    observe_cot_iterations,
)
from app.observability.otel import (
    init_tracer,
    llm_span,
    NOOP_SPAN,
)

__all__ = [
    "metrics_response",
    "observe_request",
    "observe_ttft",
    "observe_circuit_breaker_state",
    "observe_cot_iterations",
    "init_tracer",
    "llm_span",
    "NOOP_SPAN",
]
