"""Observability — Prometheus metrics + OpenTelemetry GenAI spans.

Wave 1 #1. Two layers:
- `prometheus.py`: /metrics endpoint with request/token/cost/TTFT/CB counters
- `otel.py`: GenAI semconv spans, no-op when OTLP endpoint unset

Both are always importable; exporting is gated on env vars so dev/tests
work without any collector infrastructure.
"""

from app.observability.otel import (
    NOOP_SPAN,
    init_tracer,
    llm_span,
)
from app.observability.prometheus import (
    metrics_response,
    observe_cache_lookup,
    observe_cache_tokens,
    observe_circuit_breaker_state,
    observe_cot_iterations,
    observe_request,
    observe_ttft,
)

__all__ = [
    "metrics_response",
    "observe_request",
    "observe_ttft",
    "observe_circuit_breaker_state",
    "observe_cot_iterations",
    "observe_cache_lookup",
    "observe_cache_tokens",
    "init_tracer",
    "llm_span",
    "NOOP_SPAN",
]
