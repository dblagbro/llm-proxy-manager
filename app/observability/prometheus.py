"""Prometheus metrics layer.

Low-cardinality labels only: provider (bounded by configured set), model
(bounded by provider model list), endpoint (two values: messages, completions),
status (success/failure), direction (input/output).

`/metrics` endpoint is unauthenticated — standard Prometheus convention;
protect at the nginx layer if the proxy is on a public network.
"""
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

REQUESTS_TOTAL = Counter(
    "llm_proxy_requests_total",
    "Completed LLM requests by provider, model, endpoint, and outcome.",
    ["provider", "model", "endpoint", "status"],
)

REQUEST_DURATION = Histogram(
    "llm_proxy_request_duration_seconds",
    "Wall-clock duration of LLM requests (excluding stream read time).",
    ["provider", "model", "endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

TTFT = Histogram(
    "llm_proxy_ttft_seconds",
    "Time to first token for streaming responses.",
    ["provider", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

TOKENS_TOTAL = Counter(
    "llm_proxy_tokens_total",
    "Tokens consumed by direction.",
    ["provider", "model", "direction"],
)

COST_USD_TOTAL = Counter(
    "llm_proxy_cost_usd_total",
    "Cost accumulated in USD.",
    ["provider", "model"],
)

CIRCUIT_BREAKER_STATE = Gauge(
    "llm_proxy_circuit_breaker_state",
    "Per-provider CB state: 0=closed, 1=half-open, 2=open.",
    ["provider"],
)

COT_ITERATIONS = Histogram(
    "llm_proxy_cot_iterations",
    "Refinement rounds actually used by the CoT-E pipeline.",
    ["model"],
    buckets=(0, 1, 2, 3, 4, 5),
)

SERVICE_INFO = Info("llm_proxy_service", "Service metadata.")

_CB_STATE_MAP = {"closed": 0, "half-open": 1, "open": 2}


def set_service_info(version: str, node_id: str) -> None:
    SERVICE_INFO.info({"version": version, "node_id": node_id or ""})


def observe_request(
    *,
    provider: str,
    model: str,
    endpoint: str,
    success: bool,
    duration_sec: float,
    in_tokens: int,
    out_tokens: int,
    cost_usd: float,
) -> None:
    status = "success" if success else "failure"
    REQUESTS_TOTAL.labels(provider=provider, model=model, endpoint=endpoint, status=status).inc()
    if duration_sec > 0:
        REQUEST_DURATION.labels(provider=provider, model=model, endpoint=endpoint).observe(duration_sec)
    if in_tokens > 0:
        TOKENS_TOTAL.labels(provider=provider, model=model, direction="input").inc(in_tokens)
    if out_tokens > 0:
        TOKENS_TOTAL.labels(provider=provider, model=model, direction="output").inc(out_tokens)
    if cost_usd > 0:
        COST_USD_TOTAL.labels(provider=provider, model=model).inc(cost_usd)


def observe_ttft(provider: str, model: str, ttft_sec: float) -> None:
    if ttft_sec > 0:
        TTFT.labels(provider=provider, model=model).observe(ttft_sec)


def observe_circuit_breaker_state(provider: str, state: str) -> None:
    CIRCUIT_BREAKER_STATE.labels(provider=provider).set(_CB_STATE_MAP.get(state, 0))


def observe_cot_iterations(model: str, iterations: int) -> None:
    COT_ITERATIONS.labels(model=model).observe(iterations)


async def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
