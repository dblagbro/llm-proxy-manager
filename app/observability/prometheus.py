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

# v3.9.10 — caller-memory (#267) observability. Counters cover the four
# operations the memory system runs per request: injection, extract
# write-back, provider-flush, back-pressure recovery. Plus silent-degrade
# counters so a Redis/SQLite outage doesn't go unnoticed in /metrics.
MEMORY_OPERATIONS_TOTAL = Counter(
    "llm_proxy_memory_operations_total",
    "Caller-memory operations by kind (inject/extract/flush/recover) and outcome.",
    ["operation", "outcome"],
)

# v4.4.15 (F-OBS-003) — caller-memory is gated on the inbound
# ``X-Conversation-Id`` header. The feature flag has been ON
# cluster-wide since 2026-05-15 but ``caller_memory`` has had 0
# production writes — because no consumer is sending the header yet.
# This counter makes the gating header VISIBLE: it increments on
# every /v1/messages + /v1/chat/completions request, labeled by
# whether the header was present + which endpoint. The moment a
# consumer (e.g. DevinGPT) starts sending X-Conversation-Id, the
# ``has_conversation_id="true"`` series climbs and the operator
# knows caller-memory write-back has begun — without having to
# diff the caller_memory table or read consumer-side logs.
CONVERSATION_ID_REQUESTS_TOTAL = Counter(
    "llm_proxy_conversation_id_requests_total",
    "Requests to /v1/messages + /v1/chat/completions, labeled by whether "
    "the X-Conversation-Id header (the caller-memory write-back gate) was "
    "present.",
    ["endpoint", "has_conversation_id"],
)

# v3.9.10 — DB connection pool snapshot as Prometheus gauges, mirroring
# the /health.dbPool fields. Background sampler writes these on a 30s
# interval so dashboards can chart pool depth without polling /health.
DB_POOL_CHECKED_OUT = Gauge(
    "llm_proxy_db_pool_checked_out",
    "Active SQLAlchemy connections currently held by request handlers.",
)
DB_POOL_OVERFLOW = Gauge(
    "llm_proxy_db_pool_overflow",
    "QueuePool overflow count — positive when above the base pool_size.",
)
DB_POOL_SIZE = Gauge(
    "llm_proxy_db_pool_size",
    "Configured base SQLAlchemy pool_size (capacity excluding overflow).",
)

# v3.9.10 — ExternalUsageSnapshot freshness. Gauge per provider_id holding
# seconds since the last successful capture. Lets operators alert when a
# scrape stalls (e.g. cookies expired) without grepping logs.
SCRAPE_FRESHNESS_SECONDS = Gauge(
    "llm_proxy_scrape_freshness_seconds",
    "Age in seconds of the most-recent ExternalUsageSnapshot per provider.",
    ["provider_id", "provider_name", "source"],
)

# v3.10.15 BUG-032 — infrastructure-level errors (ASGI exceptions, DB
# connection-pool faults) tapped from the logging system. They used to
# log as bare stdlib ERRORs that never reached activity_log, so the
# v3.10.4 error-rate alert was blind to them. ``fault_class`` separates
# benign client-disconnects ("disconnect") from genuine faults ("fault")
# so a pool-exhaustion / ASGI-crash incident is distinguishable from
# routine client churn.
INFRA_ERRORS_TOTAL = Counter(
    "llm_proxy_infra_errors_total",
    "ASGI / DB-pool errors tapped from logging, classified by fault_class.",
    ["source", "fault_class"],  # source: pool|asgi ; fault_class: disconnect|fault
)

# v5.3.4 — openai-python client transparent-retry counter. The
# openai-python http layer retries on its own (default 2 retries) and
# absorbs the resulting transient upstream errors before they reach
# ``litellm``'s response object. From our app's view a request that
# took 14s with 3 underlying upstream errors looks identical to a
# successful 200 in 14s. This counter taps the
# ``INFO:openai._base_client:Retrying request to <endpoint> in N
# seconds`` log line so we can spot rising upstream flakiness BEFORE
# the openai client exhausts its retries and returns a 500 to us.
# Today's c1conv finding: 14 retries clustered in a 13-min burst on
# 2026-06-09 04:22-04:35 UTC, invisible to activity_log because
# everything returned 200 eventually.
OPENAI_RETRIES_TOTAL = Counter(
    "llm_proxy_openai_retries_total",
    "openai-python client transparent retries, tapped from its INFO logger.",
    ["endpoint"],  # /chat/completions | /messages | other
)

# v5.23 — local accelerator telemetry (read-only). Resource gauges, not
# MCP capability-signalling back-pressure.
LOCAL_VRAM_USED_BYTES = Gauge(
    "llmp_local_vram_used_bytes",
    "Observed VRAM used bytes per local accelerator (0 when unknown).",
    ["accelerator"],
)
LOCAL_RAM_AVAILABLE_BYTES = Gauge(
    "llmp_local_ram_available_bytes",
    "Host RAM available bytes from the local accelerator probe.",
)

SERVICE_INFO = Info("llm_proxy_service", "Service metadata.")

_CB_STATE_MAP = {"closed": 0, "half-open": 1, "open": 2}


def observe_infra_error(source: str, fault_class: str) -> None:
    """Increment the infra-error counter. Never raises — called from a
    logging handler, which must not fail."""
    try:
        INFRA_ERRORS_TOTAL.labels(source=source, fault_class=fault_class).inc()
    except Exception:
        pass


def observe_openai_retry(endpoint: str) -> None:
    """Increment the openai-python retry counter for ``endpoint``
    (e.g. ``"/chat/completions"``). Never raises — called from a
    logging handler, which must not fail. ``endpoint`` collapses to
    ``"other"`` when unparseable so the cardinality stays bounded."""
    try:
        OPENAI_RETRIES_TOTAL.labels(endpoint=endpoint or "other").inc()
    except Exception:
        pass


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


def observe_memory_operation(operation: str, outcome: str) -> None:
    """v3.9.10 — increment the caller-memory ops counter.

    operation: 'inject' | 'extract' | 'flush' | 'recover'
    outcome:   'applied' | 'skipped' | 'degraded'  (degraded = silent
               degrade from any path — store error, redis outage, etc.)
    """
    MEMORY_OPERATIONS_TOTAL.labels(operation=operation, outcome=outcome).inc()


def observe_db_pool_snapshot(size: int, checked_out: int, overflow: int) -> None:
    """v3.9.10 — sampled pool depth, called from background ticker."""
    DB_POOL_SIZE.set(size)
    DB_POOL_CHECKED_OUT.set(checked_out)
    DB_POOL_OVERFLOW.set(overflow)


def observe_local_accelerator(snap) -> None:
    """Publish VRAM/RAM gauges from a HostSnapshot. Never raises."""
    try:
        if not getattr(snap, "enabled", False):
            return
        for gpu in getattr(snap, "gpus", []) or []:
            used_mb = getattr(gpu, "vram_used_mb", None)
            if used_mb is None:
                continue
            LOCAL_VRAM_USED_BYTES.labels(
                accelerator=getattr(gpu, "accelerator_id", "local-gpu-0"),
            ).set(int(used_mb) * 1024 * 1024)
        avail = getattr(snap, "ram_available_mb", None)
        if avail is not None:
            LOCAL_RAM_AVAILABLE_BYTES.set(int(avail) * 1024 * 1024)
    except Exception:
        pass


def observe_scrape_freshness(provider_id: str, provider_name: str, source: str, age_sec: float) -> None:
    """v3.9.10 — emit one gauge sample per provider per scrape pass."""
    SCRAPE_FRESHNESS_SECONDS.labels(
        provider_id=provider_id,
        provider_name=provider_name,
        source=source,
    ).set(age_sec)


async def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
