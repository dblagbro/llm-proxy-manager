from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Server
    port: int = Field(3000, alias="PORT")
    log_level: str = Field("info", alias="LOG_LEVEL")
    secret_key: str = Field("change-me-in-production", alias="SECRET_KEY")

    # Database
    database_url: str = Field(
        "sqlite+aiosqlite:////app/data/llmproxy.db", alias="DATABASE_URL"
    )

    # v3.10.2 (ARCH-A) — DB connection-pool checkout tracer. When True,
    # every pool checkout records an acquisition stack; the trace is
    # exposed at GET /cluster/db-pool-trace (admin). Default OFF —
    # traceback capture per checkout has overhead. Enable on ONE node
    # while hunting the latent pool leak, then recreate that container.
    db_pool_trace: bool = Field(False, alias="DB_POOL_TRACE")

    # v3.10.4 — aggregate error-rate alert. The v3.10.1 severity
    # taxonomy made operator-actionable failures log as severity=error;
    # this turns that into an alert so a sustained error spike pages
    # instead of running unnoticed (the v3.10.0 translation bug went
    # ~3 weeks unalerted). Checked every ~5 min by observability_sampler.
    # Fires when, within the window, err >= min_count AND error-rate >=
    # threshold_pct. min_count is the low-traffic noise floor.
    error_rate_alert_enabled: bool = Field(True, alias="ERROR_RATE_ALERT_ENABLED")
    error_rate_alert_window_min: int = Field(15, alias="ERROR_RATE_ALERT_WINDOW_MIN")
    error_rate_alert_threshold_pct: float = Field(10.0, alias="ERROR_RATE_ALERT_THRESHOLD_PCT")
    error_rate_alert_min_count: int = Field(10, alias="ERROR_RATE_ALERT_MIN_COUNT")

    # Redis (optional — in-memory fallback when not set)
    redis_url: Optional[str] = Field(None, alias="REDIS_URL")

    # Circuit breaker defaults
    circuit_breaker_threshold: int = Field(3, alias="CIRCUIT_BREAKER_THRESHOLD")
    circuit_breaker_timeout_sec: int = Field(60, alias="CIRCUIT_BREAKER_TIMEOUT_SEC")
    circuit_breaker_halfopen_sec: int = Field(30, alias="CIRCUIT_BREAKER_HALFOPEN_SEC")
    circuit_breaker_success_needed: int = Field(2, alias="CIRCUIT_BREAKER_SUCCESS_NEEDED")

    # Hold-down timer (seconds to suppress a provider after failure)
    hold_down_sec: int = Field(120, alias="HOLD_DOWN_SEC")

    # LMRH v2 — bidirectional metrics feedback channel.
    # Operator-approved 2026-05-09; default-off until per-node flip
    # after SDK reference impl proven (operator decision #6).
    lmrh_v2_enabled: bool = Field(False, alias="LMRH_V2_ENABLED")
    # v3.7.18 — operator answer to LMRHv2 Q6: per-NODE control,
    # one at a time. The lmrh_v2_enabled flag above is cluster-synced
    # via SystemSetting and propagates to all peers within ~60s, so it
    # doesn't allow per-node-only enablement. This env var overrides
    # that: when set explicitly to "on" or "off", THIS node uses the
    # override regardless of cluster setting. Default "auto" means
    # follow the SystemSetting (legacy behavior). Operators flipping
    # one-node-at-a-time should `LMRH_V2_NODE_OVERRIDE=on` on the
    # target node, verify, then propagate cluster-wide via the
    # SystemSetting before clearing the env var.
    lmrh_v2_node_override: str = Field("auto", alias="LMRH_V2_NODE_OVERRIDE")

    # CoT-E pipeline
    cot_enabled: bool = Field(True, alias="COT_ENABLED")
    cot_max_iterations: int = Field(1, alias="COT_MAX_ITERATIONS")
    cot_quality_threshold: int = Field(6, alias="COT_QUALITY_THRESHOLD")
    cot_critique_max_tokens: int = Field(200, alias="COT_CRITIQUE_MAX_TOKENS")
    cot_plan_max_tokens: int = Field(400, alias="COT_PLAN_MAX_TOKENS")
    cot_session_ttl_sec: int = Field(1800, alias="COT_SESSION_TTL_SEC")
    cot_session_max_analyses: int = Field(3, alias="COT_SESSION_MAX_ANALYSES")
    # Skip critique/refinement when the initial draft exceeds this token count;
    # 0 = always refine. Avoids wasted calls on already-thorough long answers.
    cot_min_tokens_skip: int = Field(800, alias="COT_MIN_TOKENS_SKIP")
    # Native reasoning — injected into requests routed to thinking-capable providers.
    # budget_tokens applies to Gemini 2.5 and is passed through for Anthropic thinking requests.
    # reasoning_effort applies to OpenAI o-series (low / medium / high).
    native_thinking_budget_tokens: int = Field(8192, alias="NATIVE_THINKING_BUDGET_TOKENS")
    native_reasoning_effort: str = Field("medium", alias="NATIVE_REASONING_EFFORT")

    # Verification pass — generates "what commands verify this answer?" after refinement.
    # Disabled by default (adds one extra LLM call). Enable globally or per-request
    # via X-Cot-Verify: true.
    cot_verify_enabled: bool = Field(False, alias="COT_VERIFY_ENABLED")
    cot_verify_max_tokens: int = Field(400, alias="COT_VERIFY_MAX_TOKENS")
    # When True, only verify answers that contain shell code blocks or infra CLI tools.
    # When False, verify every CoT response (use with care — adds latency to all requests).
    cot_verify_auto_detect: bool = Field(True, alias="COT_VERIFY_AUTO_DETECT")
    # Cross-provider critique (Wave 2 #8): route the critique pass to a DIFFERENT
    # provider than the one producing the draft. Eliminates ~5-15% self-preference
    # bias documented in 2024-25 LLM-as-Judge surveys.
    cot_cross_provider_critique: bool = Field(True, alias="COT_CROSS_PROVIDER_CRITIQUE")
    # Wave 2 #9: actually execute verify steps (HTTP/DNS/TCP only, 5s each).
    # Off by default; flip on once operators are comfortable that only the
    # network-safe subset ever executes in-process. Unsafe commands are
    # always emitted as structured SSE verify_step events for client-side exec.
    cot_verify_execute: bool = Field(False, alias="COT_VERIFY_EXECUTE")
    cot_verify_step_timeout_sec: float = Field(5.0, alias="COT_VERIFY_STEP_TIMEOUT_SEC")
    # Wave 2 #12: Chain-of-Draft plan compression (~5-word mini-steps).
    # Published 78% token cut, 76% TTFT cut, <5pp quality drop vs verbose plan.
    cot_plan_compact: bool = Field(True, alias="COT_PLAN_COMPACT")
    # Wave 3 #17: Ordered fallback across ranked providers (non-streaming only).
    fallback_enabled: bool = Field(True, alias="FALLBACK_ENABLED")
    fallback_max_providers: int = Field(3, alias="FALLBACK_MAX_PROVIDERS")
    # Wave 3 #15: auto-classify task= hint via text-embedding-3-small cosine.
    # Adds ~40ms for one embedding API call per classified request.
    task_auto_detect_enabled: bool = Field(False, alias="TASK_AUTO_DETECT_ENABLED")
    # Wave 3 #16: shadow traffic — mirror a fraction of requests to a candidate
    # provider async; measure quality-diff vs primary. No user impact.
    shadow_traffic_rate: float = Field(0.0, alias="SHADOW_TRAFFIC_RATE")
    shadow_candidate_provider_id: str = Field("", alias="SHADOW_CANDIDATE_PROVIDER_ID")
    # Wave 5 #24: structured-output validation + repair loop
    structured_output_enabled: bool = Field(True, alias="STRUCTURED_OUTPUT_ENABLED")
    structured_output_max_repairs: int = Field(2, alias="STRUCTURED_OUTPUT_MAX_REPAIRS")
    # Wave 5 #25: when vision-stripping is about to fire, attempt to route
    # images through a vision-capable provider instead and inject captions.
    vision_route_enabled: bool = Field(True, alias="VISION_ROUTE_ENABLED")

    # Semantic cache (Wave 1 #3). Requires Redis-Stack / RediSearch.
    semantic_cache_enabled: bool = Field(True, alias="SEMANTIC_CACHE_ENABLED")
    semantic_cache_threshold: float = Field(0.88, alias="SEMANTIC_CACHE_THRESHOLD")
    semantic_cache_ttl_sec: int = Field(86400, alias="SEMANTIC_CACHE_TTL_SEC")
    semantic_cache_embedding_model: str = Field(
        "text-embedding-3-small", alias="SEMANTIC_CACHE_EMBEDDING_MODEL"
    )
    # Matryoshka-truncated dimensions — 512 keeps ~98% quality at 33% size
    semantic_cache_embedding_dims: int = Field(512, alias="SEMANTIC_CACHE_EMBEDDING_DIMS")
    # v3.0.67: when set, route semantic-cache embeddings through this
    # specific proxy provider (using its api_key + base_url + litellm
    # prefix) instead of letting litellm pick implicitly from the model
    # name. Honors operator priority intent — e.g. set to a Google
    # provider id to route embeddings to Gemini instead of OpenAI.
    semantic_cache_provider_id: str = Field("", alias="SEMANTIC_CACHE_PROVIDER_ID")
    # Minimum response length (chars) to be worth caching — filters refusals, errors
    semantic_cache_min_response_chars: int = Field(200, alias="SEMANTIC_CACHE_MIN_RESPONSE_CHARS")

    # Hedged requests (Wave 1 #4)
    hedge_enabled: bool = Field(True, alias="HEDGE_ENABLED")
    hedge_max_per_sec: float = Field(5.0, alias="HEDGE_MAX_PER_SEC")

    # Run runtime (v3.0)
    runs_max_turns_ceiling: int = Field(50, alias="RUNS_MAX_TURNS_CEILING")
    runs_max_model_calls_per_minute: int = Field(5, alias="RUNS_MAX_MODEL_CALLS_PER_MINUTE")

    # v3.0.2: keep-alive probes — 0 disables
    keepalive_probe_interval_sec: int = Field(300, alias="KEEPALIVE_PROBE_INTERVAL_SEC")
    # v3.0.56: by default skip probes on per-call providers (Cohere,
    # OpenAI, Vertex, etc.) since they burn real $ on synthetic traffic.
    # Subscription-tier providers (claude-oauth, codex-oauth) keep
    # probing — cost is $0. Set True to restore the pre-v3.0.56
    # behavior of probing everything.
    keepalive_probe_per_call_providers: bool = Field(
        False, alias="KEEPALIVE_PROBE_PER_CALL_PROVIDERS"
    )
    # v3.3.3: probe back-off after rate_limit (HTTP 429) — see audit on
    # 2026-05-09 for the pattern this addresses (probes spaced exactly
    # 5min apart kept hammering grok.com during throttle windows; 11
    # probe-warnings/day all rate_limit class). On a rate_limit failure
    # we double the next-probe delay starting from interval_sec, capped
    # at backoff_max_sec; reset on first success. 0 disables back-off
    # (pre-v3.3.3 behavior).
    keepalive_probe_rate_limit_backoff_max_sec: int = Field(
        1800, alias="KEEPALIVE_PROBE_RATE_LIMIT_BACKOFF_MAX_SEC"
    )
    keepalive_probe_rate_limit_backoff_factor: float = Field(
        2.0, alias="KEEPALIVE_PROBE_RATE_LIMIT_BACKOFF_FACTOR"
    )

    # v3.3.3: outer ceiling on user-traffic grok-web requests. Bridge
    # tail latency outliers (15s observed 2026-05-09) hurt p99 — better
    # to fail fast and let the router fall through to OpenRouter than
    # let a user wait 60s. Probes still use _PROBE_TIMEOUT_SEC (15s).
    # v3.4.0: tightened 30→20s after a day of v3.3.3+ telemetry showed
    # p95 ~7s and only 2 outliers >10s in 24h. 20s is still 3× headroom
    # over real p95 while cutting tail-latency damage to user requests.
    grok_web_user_timeout_sec: int = Field(
        20, alias="GROK_WEB_USER_TIMEOUT_SEC"
    )

    # v5.1.0 / Batch B1 — ApiKey tombstone retention window. After a
    # soft-delete via DELETE /api/keys/{id}, the row stays in the table
    # with deleted_at set for this many days. The admin UI exposes a
    # Trash tab and a Restore endpoint during this window; after, the
    # next prune sweep hard-deletes the row.
    api_key_tombstone_retention_days: int = Field(90, alias="API_KEY_TOMBSTONE_RETENTION_DAYS")

    # v3.0.7: activity_log + provider_metrics + run_events retention (days)
    activity_log_retention_days: int = Field(30, alias="ACTIVITY_LOG_RETENTION_DAYS")
    # v3.7.22 (#254): severity-tiered retention. info events are >99% of volume
    # and lose diagnostic value within ~30d; warning/error events are rare and
    # post-mortem-valuable for much longer. Defaults: warning 1y, error 5y.
    activity_log_warning_retention_days: int = Field(365, alias="ACTIVITY_LOG_WARNING_RETENTION_DAYS")
    activity_log_error_retention_days: int = Field(1825, alias="ACTIVITY_LOG_ERROR_RETENTION_DAYS")
    # v3.7.24 (#258): minimum gap between Anthropic billing scrapes for the
    # same provider. Defaults to interval/2 in the worker if left at 0.
    # Operator can pin this to override the heuristic (e.g. set to 7200 to
    # enforce "no scrapes more often than every 2h regardless of interval").
    anthropic_billing_min_scrape_gap_sec: int = Field(0, alias="ANTHROPIC_BILLING_MIN_SCRAPE_GAP_SEC")

    # v3.7.27 (#245): ChatGPT Plus / Codex Cloud usage scrape — same
    # behavior as the Anthropic billing scraper but against an
    # operator-supplied chatgpt.com analytics endpoint URL.
    codex_billing_scrape_interval_sec: int = Field(14400, alias="CODEX_BILLING_SCRAPE_INTERVAL_SEC")
    codex_billing_min_scrape_gap_sec: int = Field(0, alias="CODEX_BILLING_MIN_SCRAPE_GAP_SEC")

    # v3.7.30 (#252 phase 3): AI provider supervisor — provider-side
    # mirror of the v3.7.10 AI rate limiter. Default OFF; opt-in per
    # node via ``ai_provider_supervisor_enabled=True``. Auto-apply is
    # gated by a separate flag so operators can run in suggest-only
    # mode for an observation period first.
    ai_provider_supervisor_enabled: bool = Field(False, alias="AI_PROVIDER_SUPERVISOR_ENABLED")
    ai_provider_supervisor_auto_apply: bool = Field(False, alias="AI_PROVIDER_SUPERVISOR_AUTO_APPLY")
    ai_provider_supervisor_interval_sec: int = Field(1800, alias="AI_PROVIDER_SUPERVISOR_INTERVAL_SEC")        # 30 min
    ai_provider_supervisor_short_window_min: int = Field(30, alias="AI_PROVIDER_SUPERVISOR_SHORT_WINDOW_MIN")
    ai_provider_supervisor_trend_window_days: int = Field(1, alias="AI_PROVIDER_SUPERVISOR_TREND_WINDOW_DAYS") # operator-locked 2026-05-13
    ai_provider_supervisor_model: str = Field("claude-haiku-4-5-20251001", alias="AI_PROVIDER_SUPERVISOR_MODEL")
    ai_provider_supervisor_internal_api_key: str = Field("", alias="AI_PROVIDER_SUPERVISOR_INTERNAL_API_KEY")
    ai_provider_supervisor_max_priority_delta: int = Field(2, alias="AI_PROVIDER_SUPERVISOR_MAX_PRIORITY_DELTA")
    ai_provider_supervisor_max_auto_skip_hours: int = Field(24, alias="AI_PROVIDER_SUPERVISOR_MAX_AUTO_SKIP_HOURS")

    # v5.7.15 — burst-trigger force-open. Independent of the LLM
    # supervisor: a cheap DB-only sweep that fires every
    # ``empty_success_burst_interval_sec`` (default 60s) and force-opens
    # the CB on any provider with >= ``empty_success_burst_threshold``
    # ``streaming.empty_success_failover`` rows in
    # ``empty_success_burst_window_sec``. Closes the 30-min "supervisor
    # hasn't swept yet" gap that the 2026-06-17 c1conv incident sat in.
    # Default ON — the gap it closes is the operator-escalated one.
    empty_success_burst_trigger_enabled: bool = Field(True, alias="EMPTY_SUCCESS_BURST_TRIGGER_ENABLED")
    empty_success_burst_interval_sec: int = Field(60, alias="EMPTY_SUCCESS_BURST_INTERVAL_SEC")
    empty_success_burst_window_sec: int = Field(300, alias="EMPTY_SUCCESS_BURST_WINDOW_SEC")
    empty_success_burst_threshold: int = Field(3, alias="EMPTY_SUCCESS_BURST_THRESHOLD")

    # v5.14.2 (#492) — cluster-sync 403-rate escalation trigger. The
    # tmrwww02-peer misconfig produces a known ~50% baseline; this monitor
    # fires an activity_log warning ONLY when the rolling-1h rate climbs
    # above ``cluster_sync_403_alert_threshold_pct`` (default 70%), staying
    # quiet at the baseline but catching any regression. ``min_attempts``
    # gates noise from single-403-in-idle-window cases.
    cluster_sync_403_monitor_enabled: bool = Field(True, alias="CLUSTER_SYNC_403_MONITOR_ENABLED")
    cluster_sync_403_monitor_interval_sec: int = Field(300, alias="CLUSTER_SYNC_403_MONITOR_INTERVAL_SEC")
    cluster_sync_403_alert_threshold_pct: float = Field(70.0, alias="CLUSTER_SYNC_403_ALERT_THRESHOLD_PCT")
    cluster_sync_403_alert_min_attempts: int = Field(4, alias="CLUSTER_SYNC_403_ALERT_MIN_ATTEMPTS")
    cluster_sync_403_alert_cooldown_sec: int = Field(3600, alias="CLUSTER_SYNC_403_ALERT_COOLDOWN_SEC")

    # v5.15.0 Phase 1 (#508) — per-account OAuth fan-out. Kill-switch on the
    # feature: when False, dispatch will always fall back to legacy
    # Provider.api_key even after v5.15.1 wires the account-picker. Safe-revert
    # for surprise behavior. In Phase 1 (schema + admin endpoints only)
    # this setting doesn't gate anything yet — reserved for Phase 2.
    oauth_account_fanout_enabled: bool = Field(True, alias="OAUTH_ACCOUNT_FANOUT_ENABLED")
    # Default account-pick strategy for providers whose oauth_account_strategy
    # column is NULL. Operator confirmed 2026-06-30: 'least_utilized' — Cursor's
    # pain is utilization-driven. Values: least_utilized | round_robin |
    # least_recently_used.
    oauth_account_default_strategy: str = Field("least_utilized", alias="OAUTH_ACCOUNT_DEFAULT_STRATEGY")

    # v5.17.1 — chronic-CB keepalive gate. Providers whose CB has
    # re-opened >=threshold times consecutively (default 5) get a
    # 6h backoff on keepalive probes so we stop writing an activity_log
    # warning per sweep for known-broken upstreams. Grok-Web-Devin post-#513
    # was the trigger — bridge timeouts on /api/chat had generated 19+
    # keepalive_probe events in 24h with zero diagnostic value.
    keepalive_chronic_cb_open_threshold: int = Field(5, alias="KEEPALIVE_CHRONIC_CB_OPEN_THRESHOLD")
    keepalive_chronic_cb_open_backoff_sec: int = Field(21600, alias="KEEPALIVE_CHRONIC_CB_OPEN_BACKOFF_SEC")

    # v5.7.17 — client-disconnect watchdog. Closes the supervisor DB
    # pool leak (2026-06-16): handler kept running + held its DB
    # connection after the client disconnected. Polls
    # ``request.is_disconnected()`` every ``disconnect_watchdog_interval_sec``
    # and cancels the handler task on disconnect — CancelledError
    # propagates through ``async with db: ...`` and releases the
    # connection. Default ON. Set ``DISCONNECT_WATCHDOG_ENABLED=false``
    # to confirm the pool leak returns (clean A/B repro).
    disconnect_watchdog_enabled: bool = Field(True, alias="DISCONNECT_WATCHDOG_ENABLED")
    disconnect_watchdog_interval_sec: float = Field(2.0, alias="DISCONNECT_WATCHDOG_INTERVAL_SEC")

    # v5.10.0 — MCP capability back-pressure (Ship 1+2). The capability
    # scout already writes activity_log rows when it sees refusal
    # patterns that map to known MCP tools; v5.10 also bumps a per-
    # (api_key, tool) score. When the score exceeds the threshold,
    # responses carry X-Proxy-MCP-Suggestion so the caller can decide
    # to wire the tool. Operator picked threshold=50 in the 2026-06-30
    # interview (≈ 3 refusals at the default +20 per bump). Master
    # switch is on; settings-side flip drops emission fleet-wide.
    mcp_suggestion_emission_enabled: bool = Field(True, alias="MCP_SUGGESTION_EMISSION_ENABLED")
    mcp_suggestion_threshold: int = Field(50, alias="MCP_SUGGESTION_THRESHOLD")

    # v5.14.0 — Response-shaping callback registry. Hub team's Tier 1
    # ask from the 2026-06-30 peer-comparison memo. Per-hook timeout
    # default 2s. Fail-closed default (opposite of Portkey's
    # webhook default-true) per our v2.0.0 banned-vendor 451 posture.
    # Settings keys reserved for hub-managed hooks:
    #   callbacks.fail_closed: bool  — global default fail-closed
    #   callbacks.<hook_name>.timeout_sec: float  — per-hook timeout
    #   callbacks.<hook_name>.enabled: bool       — per-hook on/off
    # Hub team registers its substitution-mirror hook via these.
    callbacks_fail_closed: bool = Field(True, alias="CALLBACKS_FAIL_CLOSED")
    callbacks_default_timeout_sec: float = Field(2.0, alias="CALLBACKS_DEFAULT_TIMEOUT_SEC")

    # v5.18.0 — outbound substitution callback POST. Hub team's v2.6.6
    # receiver lives at POST /api/compliance/callbacks/substitution.
    # Empty URL = hook is a no-op (safe default: operator must opt in
    # per-cluster). Shared secret goes in the X-Proxy-Callback-Token
    # header; empty = dev-mode passthrough (hub accepts unauthed
    # bodies until it sets its own callbacks.shared_secret).
    substitution_callback_url: str = Field("", alias="SUBSTITUTION_CALLBACK_URL")
    substitution_callback_shared_secret: str = Field("", alias="SUBSTITUTION_CALLBACK_SHARED_SECRET")

    # v5.8.0 — AI integration protocol. Lets other AI projects discover
    # the proxy's capabilities via a public ``/announce`` URL, then
    # negotiate API key configuration through a passphrase-gated chat
    # endpoint ``/api/integration/chat``. The management chat is
    # LLM-backed (same internal key as the supervisor) and may mint
    # keys via the create_api_key tool. Disabled by default.
    integration_enabled: bool = Field(False, alias="INTEGRATION_ENABLED")
    integration_passphrase: str = Field("", alias="INTEGRATION_PASSPHRASE")
    integration_default_daily_budget_usd: float = Field(5.00, alias="INTEGRATION_DEFAULT_DAILY_BUDGET_USD")
    integration_max_daily_budget_usd: float = Field(20.00, alias="INTEGRATION_MAX_DAILY_BUDGET_USD")
    integration_max_messages_per_session: int = Field(20, alias="INTEGRATION_MAX_MESSAGES_PER_SESSION")
    integration_model: str = Field("claude-haiku-4-5-20251001", alias="INTEGRATION_MODEL")

    # v4.0 — AIRI (AI Router Interface): the conversational chat UI for the
    # AI Provider Supervisor, on the Routing page. ``airi_enabled`` is the
    # feature flag for the whole 4.0 arc — off by default. ``airi_model``
    # is an optional override; empty falls back to the supervisor's model.
    # AIRI reuses ``ai_provider_supervisor_internal_api_key`` for its own
    # LLM calls (which route through the proxy, so they inherit fallback).
    airi_enabled: bool = Field(False, alias="AIRI_ENABLED")
    airi_model: str = Field("", alias="AIRI_MODEL")
    # v4.0 milestone 4 — scheduled-rule automation. ``airi_automation_enabled``
    # is the kill switch for the deterministic rule evaluator (default off —
    # automation runs only when explicitly enabled). The evaluator never runs
    # an LLM; it evaluates operator-authored rules on this interval.
    airi_automation_enabled: bool = Field(False, alias="AIRI_AUTOMATION_ENABLED")
    airi_evaluator_interval_sec: int = Field(60, alias="AIRI_EVALUATOR_INTERVAL_SEC")
    # v4.2 — voice input. ``airi_voice_enabled`` is the feature flag (off
    # until the v4.2.0 build is complete). Speech-to-text runs on the
    # self-hosted whisper-bridge sidecar — audio is transcribed on our own
    # infrastructure and never persisted. See docs/4.2-voice-design.md.
    airi_voice_enabled: bool = Field(False, alias="AIRI_VOICE_ENABLED")
    airi_whisper_bridge_url: str = Field(
        "http://whisper-bridge:9000", alias="AIRI_WHISPER_BRIDGE_URL")
    airi_whisper_bridge_token: str = Field("", alias="AIRI_WHISPER_BRIDGE_TOKEN")
    # v4.3 — voice output (text-to-speech). ``airi_tts_enabled`` is the v4.3
    # feature flag (off until the build is complete). "Airy" reads its
    # answers aloud; synthesis runs on the self-hosted whisper-bridge sidecar
    # (Piper TTS) and audio is never persisted. See docs/4.3-tts-design.md.
    airi_tts_enabled: bool = Field(False, alias="AIRI_TTS_ENABLED")

    # v5.9.0 — public OpenAI-compatible /v1/audio/* endpoints fall back to
    # the whisper-bridge sidecar (Piper TTS + Whisper STT) when the upstream
    # provider call errors. Disable to force upstream-only (e.g. for strict
    # cost-accounting environments where the bridge bypass would skew the
    # bill).
    audio_fallback_to_whisper_bridge: bool = Field(
        True, alias="AUDIO_FALLBACK_TO_WHISPER_BRIDGE"
    )

    # v3.8.4 (#264): tool-call capability prober. Fires a standard
    # get_weather(city) probe at every (provider, default_model) on
    # a configurable cadence; rolling success rate drives
    # ModelCapability.native_tools via hysteresis.
    ai_tool_prober_enabled: bool = Field(False, alias="AI_TOOL_PROBER_ENABLED")
    ai_tool_prober_interval_sec: int = Field(86400, alias="AI_TOOL_PROBER_INTERVAL_SEC")  # daily
    ai_tool_prober_internal_api_key: str = Field("", alias="AI_TOOL_PROBER_INTERNAL_API_KEY")
    ai_tool_prober_native_threshold: float = Field(0.8, alias="AI_TOOL_PROBER_NATIVE_THRESHOLD")
    ai_tool_prober_emulate_threshold: float = Field(0.6, alias="AI_TOOL_PROBER_EMULATE_THRESHOLD")
    ai_tool_prober_success_window: int = Field(5, alias="AI_TOOL_PROBER_SUCCESS_WINDOW")

    # v3.8.7 (#267): proxy-side caller memory store. Cluster-replicated
    # via the same LWW pattern as Provider / ApiKeyAiReview rows.
    # Read-cached in Redis when redis_url is set; SQLite is the durable
    # source of truth.
    # See docs/rfc/2026-05-proxy-memory-store.md for full design.
    caller_memory_enabled: bool = Field(False, alias="CALLER_MEMORY_ENABLED")
    # When operator wants to opt out of provider-side memory flushing
    # (e.g. for debugging), this overrides the per-Provider auto-flush.
    caller_memory_active_flush_enabled: bool = Field(True, alias="CALLER_MEMORY_ACTIVE_FLUSH_ENABLED")
    # v3.9.4 Phase 7 — back-pressure recovery. When a marker exists but
    # content is gone (DB restore that lost content rows), try to
    # reconstruct from the original upstream provider. Default ON; gated
    # behind caller_memory_enabled overall.
    caller_memory_recovery_enabled: bool = Field(True, alias="CALLER_MEMORY_RECOVERY_ENABLED")
    # v3.9.13 — TTL sweeper interval (seconds). Default 3600 (1h). The
    # per-key opt-in itself lives on ApiKey.caller_memory_ttl_days; this
    # is just how often the background worker checks. Set to 0 to
    # disable the sweeper entirely (rows never auto-expire even if
    # ttl_days is set on a key).
    caller_memory_ttl_sweep_interval_sec: int = Field(3600, alias="CALLER_MEMORY_TTL_SWEEP_INTERVAL_SEC")
    # v3.0.13: how long a provider tombstone (deleted_at non-null) is kept
    # before hard-delete. Cluster sync converges in seconds, so 7d is a
    # comfortable safety margin.
    provider_tombstone_retention_days: int = Field(7, alias="PROVIDER_TOMBSTONE_RETENTION_DAYS")

    # v5.0.0 — compliance enforcement (see docs/5.0-compliance-design.md).
    # When OFF, the entire compliance path is a no-op (router pre-filter
    # short-circuits, UA detector skipped). Flipping ON is the audit-trail
    # cutover event; the daily worker also runs the one-shot
    # caller_memory.source_company backfill on first activation.
    compliance_enabled: bool = Field(False, alias="COMPLIANCE_ENABLED")
    # JSON-encoded list of company IDs to block for ALL keys on this
    # deployment (e.g. ["anthropic"]). Unioned with each ApiKey's
    # blocked_companies at request time. Cluster-synced.
    compliance_system_blocked_companies: str = Field(
        "", alias="COMPLIANCE_SYSTEM_BLOCKED_COMPANIES"
    )
    # Audit retention (days; 2555 = 7 years per decision 7).
    compliance_audit_retention_days: int = Field(
        2555, alias="COMPLIANCE_AUDIT_RETENTION_DAYS"
    )
    # UA detection master switch (decision 16). On by default. Turn off for
    # UA-spoofing debug only.
    compliance_ua_block_enabled: bool = Field(
        True, alias="COMPLIANCE_UA_BLOCK_ENABLED"
    )
    # JSON-encoded custom company taxonomy entries (decision 12), merged
    # with KNOWN_COMPANIES at lookup time. Schema in
    # docs/compliance-taxonomy-v5.0.0.md.
    compliance_custom_companies: str = Field(
        "", alias="COMPLIANCE_CUSTOM_COMPANIES"
    )
    # One-shot backfill flag (decision 19). Set automatically on first
    # policy enable; do NOT toggle manually.
    compliance_backfill_applied: bool = Field(
        False, alias="COMPLIANCE_BACKFILL_APPLIED"
    )
    # v5.2.0 / Batch V2 — vendor-neutrality fine-grained policy.
    # JSON-encoded list[str]. Mirror the per-key fields of the same name.
    # ``allowed_companies`` non-empty switches to allowlist mode (only
    # listed companies pass; everything else dropped). ``blocked_models``
    # and ``allowed_models`` entries can be exact names or fnmatch globs
    # ("claude-*", "gpt-4-*-turbo"). Cluster-synced via the existing
    # settings sync loop. Deny wins everywhere: a model in both
    # allowed_models and blocked_models is blocked.
    compliance_system_allowed_companies: str = Field(
        "", alias="COMPLIANCE_SYSTEM_ALLOWED_COMPANIES"
    )
    compliance_system_blocked_models: str = Field(
        "", alias="COMPLIANCE_SYSTEM_BLOCKED_MODELS"
    )
    compliance_system_allowed_models: str = Field(
        "", alias="COMPLIANCE_SYSTEM_ALLOWED_MODELS"
    )

    # v4.4.13: retention for the AI supervisor review tables. These
    # accumulate ~250 rows/day on a 30-min cadence × 10 providers and
    # ARE included in the cluster sync push payload — left unbounded
    # they bloat the payload (observed 2026-05-21: 1561 rows on www1 +
    # 1384 on www2, payload at 2.78 MB, www2 sync timing out at 15s).
    # 30d retention preserves operator-meaningful history while
    # bounding the table to ~7,500 rows. Tunable per env.
    ai_review_retention_days: int = Field(30, alias="AI_REVIEW_RETENTION_DAYS")

    # Cluster
    cluster_enabled: bool = Field(False, alias="CLUSTER_ENABLED")
    cluster_node_id: Optional[str] = Field(None, alias="CLUSTER_NODE_ID")
    cluster_node_name: Optional[str] = Field(None, alias="CLUSTER_NODE_NAME")
    cluster_node_url: Optional[str] = Field(None, alias="CLUSTER_NODE_URL")
    cluster_peers: Optional[str] = Field(None, alias="CLUSTER_PEERS")  # "id:url,id:url"
    cluster_sync_secret: Optional[str] = Field(None, alias="CLUSTER_SYNC_SECRET")
    cluster_heartbeat_sec: int = Field(30, alias="CLUSTER_HEARTBEAT_SEC")

    # Notifications
    smtp_enabled: bool = Field(False, alias="SMTP_ENABLED")
    smtp_host: Optional[str] = Field(None, alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: Optional[str] = Field(None, alias="SMTP_USER")
    smtp_pass: Optional[str] = Field(None, alias="SMTP_PASS")
    # v5.22.7 — some relays (earthlink) require an explicit HELO/EHLO name
    smtp_helo: Optional[str] = Field(None, alias="SMTP_HELO")
    smtp_from: Optional[str] = Field(None, alias="SMTP_FROM")
    smtp_to: Optional[str] = Field(None, alias="SMTP_TO")

    # Wave 6 — audit log export
    audit_export_dir: Optional[str] = Field(None, alias="AUDIT_EXPORT_DIR")
    audit_export_s3_bucket: Optional[str] = Field(None, alias="AUDIT_EXPORT_S3_BUCKET")
    audit_export_s3_endpoint: Optional[str] = Field(None, alias="AUDIT_EXPORT_S3_ENDPOINT")
    audit_export_s3_region: Optional[str] = Field(None, alias="AUDIT_EXPORT_S3_REGION")
    audit_export_s3_access_key: Optional[str] = Field(None, alias="AUDIT_EXPORT_S3_ACCESS_KEY")
    audit_export_s3_secret_key: Optional[str] = Field(None, alias="AUDIT_EXPORT_S3_SECRET_KEY")
    audit_export_retention_days: int = Field(90, alias="AUDIT_EXPORT_RETENTION_DAYS")

    # v2.8.4 — activity-log payload capture
    # v3.0.91 — bodies default flipped to False (1 GB activity_log incident).
    # v3.0.94 — split previews from full bodies. Operators want to see
    # message-in / response-out text in the activity log even with bodies
    # off. Previews cap at 240 chars each (~500 bytes per row) — bounded
    # cost. Full bodies remain off-by-default; only enable when wire-
    # debugging.
    activity_log_capture_previews: bool = Field(True, alias="ACTIVITY_LOG_CAPTURE_PREVIEWS")
    activity_log_capture_bodies: bool = Field(False, alias="ACTIVITY_LOG_CAPTURE_BODIES")
    activity_log_max_body_chars: int = Field(4000, alias="ACTIVITY_LOG_MAX_BODY_CHARS")
    # v3.9.2 (#268) — probabilistic full-body capture on bad_request
    # rejections from upstream. Hub team wanted a way to debug the
    # exact payload shape that an upstream 4xx'd on, without paying the
    # full-time storage cost of activity_log_capture_bodies=True (the
    # 2026-05-06 1 GB blowup). Default 0.01 = 1%. Tagged in event_meta
    # as ``body_sampled=True`` so the hub UI can filter on it.
    activity_log_body_sample_rate_4xx: float = Field(0.01, alias="ACTIVITY_LOG_BODY_SAMPLE_RATE_4XX")

    # v3.6.3 — LAN-egress IP rewrite map.
    # When the proxy is hairpin-NAT'd from inside a LAN, nginx sees the
    # LAN gateway IP (e.g. 192.168.18.1) as the source — the actual
    # public egress IP is invisible at the HTTP layer. For each known
    # internal gateway, declare a hostname whose A record reflects the
    # LAN's current public IP; the proxy resolves it (TTL-cached at
    # 300s) and substitutes the public IP in the activity log's
    # ``client_ip`` field. The original inside IP is preserved as
    # ``client_ip_inside`` for diagnostics.
    #
    # Example:
    #   client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    #
    # Empty (default) → no rewriting; ``client_ip`` is whatever XFF
    # reported. See ``app/observability/request_context.py`` for the
    # resolve logic.
    client_ip_lan_resolve_map: dict[str, str] = Field(
        default_factory=dict,
        alias="CLIENT_IP_LAN_RESOLVE_MAP",
    )

    # v3.7.0 — Anthropic Console billing scraper cadence. Every 4 hours
    # by default. Set to 0 to disable the worker (operator pause).
    # See ``app/monitoring/anthropic_billing_worker.py`` and
    # ``project_backlog_anthropic_billing_scrape.md`` for context.
    anthropic_billing_scrape_interval_sec: int = Field(
        14400, alias="ANTHROPIC_BILLING_SCRAPE_INTERVAL_SEC",
    )

    # v3.7.1 — auto-rotation rule thresholds. The router skips providers
    # whose latest snapshot reports ``seven_day_utilization >=
    # external_rotation_capacity_pct``. Skip is cleared once
    # utilization drops back below ``external_rotation_capacity_pct -
    # external_rotation_hysteresis_pct``. Defaults are 95% / 5%
    # → skip at 95%, clear at 90%.
    external_rotation_capacity_pct: float = Field(
        95.0, alias="EXTERNAL_ROTATION_CAPACITY_PCT",
    )
    external_rotation_hysteresis_pct: float = Field(
        5.0, alias="EXTERNAL_ROTATION_HYSTERESIS_PCT",
    )

    # v3.7.4 — bucket size (percentage points) for the utilization-
    # weighted reorder among claude-oauth providers. Smaller = more
    # reactive, more potential for flapping when utilization is near
    # a boundary. Default 25pp means a provider has to be >=25% lower
    # utilization than another to override operator priority.
    external_rotation_util_bucket_pct: float = Field(
        25.0, alias="EXTERNAL_ROTATION_UTIL_BUCKET_PCT",
    )

    # v3.7.10 — proactive AI rate limiter (operator-requested 2026-05-10).
    # A background worker reviews each api_key's last 30 min of traffic
    # every interval_sec and asks an LLM whether the pattern looks
    # normal/watch/throttle/block. Default OFF (opt-in per node per
    # operator Q6). When enabled but auto_apply=False (default),
    # suggestions are only recorded — operator reviews + applies via
    # the admin endpoint. Set auto_apply=True for hands-off operation
    # once you trust the model's judgement.
    ai_rate_limiter_enabled: bool = Field(False, alias="AI_RATE_LIMITER_ENABLED")
    ai_rate_limiter_interval_sec: int = Field(300, alias="AI_RATE_LIMITER_INTERVAL_SEC")
    ai_rate_limiter_window_min: int = Field(30, alias="AI_RATE_LIMITER_WINDOW_MIN")
    ai_rate_limiter_model: str = Field(
        "claude-haiku-4-5-20251001", alias="AI_RATE_LIMITER_MODEL",
    )
    ai_rate_limiter_throttle_floor_rpm: int = Field(
        5, alias="AI_RATE_LIMITER_THROTTLE_FLOOR_RPM",
    )
    ai_rate_limiter_auto_apply: bool = Field(False, alias="AI_RATE_LIMITER_AUTO_APPLY")
    # Internal api_key the worker uses to call /v1/messages for classification.
    # Operator creates a low-privilege key + sets this — without it the
    # worker no-ops cleanly. Self-issued to avoid recursion through OAuth
    # rotation logic.
    ai_rate_limiter_internal_api_key: Optional[str] = Field(
        None, alias="AI_RATE_LIMITER_INTERNAL_API_KEY",
    )

    # v3.0.98 → v3.1.2 — cluster-sync catalog-table replication.
    #
    # v3.0.96 added ModelCapability/ModelAlias/OAuthCaptureProfile to every
    # /cluster/sync push. The receiver did per-row SELECT-then-INSERT/UPDATE
    # (304 rows × DB round-trip = 12-17s per sync), causing DB contention
    # severe enough to hit the 60s nginx upstream timeout on real
    # /v1/messages calls. v3.0.98 disabled the feature behind this flag
    # while we reworked the apply path.
    #
    # v3.1.2 replaced the per-row loop with bulk-SELECT + in-memory diff:
    # one query pulls all matching existing rows, the per-row LWW logic
    # runs in memory, and ORM mutations flush in a single batch on commit.
    # Steady-state apply time dropped to 48-52ms; first-time apply (when
    # peer_updated > local for every row) is ~2s as a one-time cost.
    # Default flipped True so catalog rows actually propagate across the
    # cluster — without this, ModelCapability discoveries on one node
    # never reach peers and /v1/models capability scoring drifts.
    cluster_sync_catalog_tables: bool = Field(True, alias="CLUSTER_SYNC_CATALOG_TABLES")

    # Wave 6 — PII masking
    pii_masking_enabled: bool = Field(False, alias="PII_MASKING_ENABLED")

    # Wave 6 — semantic prompt guard
    prompt_guard_enabled: bool = Field(False, alias="PROMPT_GUARD_ENABLED")
    prompt_guard_denylist: Optional[str] = Field(None, alias="PROMPT_GUARD_DENYLIST")

    # OAuth capture (research tool for Claude Pro Max OAuth provider)
    oauth_capture_enabled: bool = Field(False, alias="OAUTH_CAPTURE_ENABLED")
    oauth_capture_upstream: Optional[str] = Field(None, alias="OAUTH_CAPTURE_UPSTREAM")
    oauth_capture_secret: Optional[str] = Field(None, alias="OAUTH_CAPTURE_SECRET")
    # v2.6.0 capture-sidecar settings removed in v2.7.0 — the sidecar was
    # deleted in favor of "paste vendor CLI credentials" UX.

    # Wave 6 — SSO/SAML
    sso_enabled: bool = Field(False, alias="SSO_ENABLED")
    sso_entity_id: Optional[str] = Field(None, alias="SSO_ENTITY_ID")
    sso_idp_metadata_url: Optional[str] = Field(None, alias="SSO_IDP_METADATA_URL")
    sso_acs_url: Optional[str] = Field(None, alias="SSO_ACS_URL")


settings = Settings()
