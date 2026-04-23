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

CACHE_TOKENS_TOTAL = Counter(
    "llm_proxy_cache_tokens_total",
    "Prompt cache tokens by kind (creation=write, read=hit).",
    ["provider", "model", "kind"],
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

CACHE_LOOKUPS_TOTAL = Counter(
    "llm_proxy_cache_lookups_total",
    "Semantic cache lookups by status.",
    ["status", "endpoint"],  # status: hit, miss, bypass
)

CACHE_SIMILARITY = Histogram(
    "llm_proxy_cache_similarity",
    "Cosine similarity score for cache hits.",
    buckets=(0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.98, 1.0),
)

HEDGE_ATTEMPTS = Counter(
    "llm_proxy_hedge_attempts_total",
    "Times a backup request was fired because the primary exceeded p95 TTFT.",
    ["primary_provider", "backup_provider"],
)

HEDGE_WINS = Counter(
    "llm_proxy_hedge_wins_total",
    "Hedge races by which side won.",
    ["winner"],  # primary | backup
)

HEDGE_BUCKET_REJECTS = Counter(
    "llm_proxy_hedge_bucket_rejects_total",
    "Hedges skipped because the global token bucket was empty.",
)

VERIFY_EXECUTIONS = Counter(
    "llm_proxy_verify_executions_total",
    "Verification steps executed (not skipped) by pass/fail/error status.",
    ["status"],
)

SHADOW_SIMILARITY = Histogram(
    "llm_proxy_shadow_similarity",
    "Embedding-cosine similarity between primary and shadow-candidate responses.",
    ["primary_model", "shadow_model"],
    buckets=(0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.93, 0.96, 0.98, 0.99, 1.0),
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


def observe_cache_tokens(provider: str, model: str, creation: int, read: int) -> None:
    if creation > 0:
        CACHE_TOKENS_TOTAL.labels(provider=provider, model=model, kind="creation").inc(creation)
    if read > 0:
        CACHE_TOKENS_TOTAL.labels(provider=provider, model=model, kind="read").inc(read)


def observe_circuit_breaker_state(provider: str, state: str) -> None:
    CIRCUIT_BREAKER_STATE.labels(provider=provider).set(_CB_STATE_MAP.get(state, 0))


def observe_cot_iterations(model: str, iterations: int) -> None:
    COT_ITERATIONS.labels(model=model).observe(iterations)


def observe_cache_lookup(status: str, endpoint: str, similarity: float = 0.0) -> None:
    CACHE_LOOKUPS_TOTAL.labels(status=status, endpoint=endpoint).inc()
    if status == "hit" and similarity > 0:
        CACHE_SIMILARITY.observe(similarity)


def observe_hedge_attempt(primary: str, backup: str) -> None:
    HEDGE_ATTEMPTS.labels(primary_provider=primary, backup_provider=backup).inc()


def observe_hedge_win(winner: str) -> None:
    HEDGE_WINS.labels(winner=winner).inc()


def observe_hedge_bucket_reject() -> None:
    HEDGE_BUCKET_REJECTS.inc()


def observe_verify_execution(status: str) -> None:
    VERIFY_EXECUTIONS.labels(status=status).inc()


def observe_shadow_similarity(primary_model: str, shadow_model: str, similarity: float) -> None:
    SHADOW_SIMILARITY.labels(primary_model=primary_model, shadow_model=shadow_model).observe(similarity)


async def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
