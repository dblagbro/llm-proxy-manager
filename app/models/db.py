from sqlalchemy import (
    Column, String, Integer, Boolean, Float, DateTime, Text, JSON, ForeignKey
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func
import secrets


class Base(DeclarativeBase):
    pass


class Session(Base):
    """Persisted login sessions — survives container restarts."""
    __tablename__ = "sessions"

    token = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    username = Column(String, nullable=False)
    role = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)   # Unix timestamp
    last_seen_at = Column(Float, nullable=False)  # updated on each /me call


class Provider(Base):
    __tablename__ = "providers"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    name = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)  # anthropic|openai|google|ollama|compatible|vertex|grok|claude-oauth
    api_key = Column(String)                         # for OAuth providers: stores the access_token
    base_url = Column(String)
    default_model = Column(String)
    priority = Column(Integer, default=10)
    enabled = Column(Boolean, default=True)
    timeout_sec = Column(Integer, default=30)
    exclude_from_tool_requests = Column(Boolean, default=False)
    # Per-provider CB overrides (null = use global setting)
    hold_down_sec = Column(Integer, nullable=True)
    failure_threshold = Column(Integer, nullable=True)
    daily_budget_usd = Column(Float, nullable=True)  # None = unlimited
    extra_config = Column(JSON, default=dict)
    # v2.7.0: OAuth-specific fields. Only populated when provider_type
    # is *-oauth (claude-oauth in v2.7.0). refresh_token lets us auto-
    # refresh before expires_at without admin intervention.
    oauth_refresh_token = Column(String, nullable=True)    # encrypted Fernet
    oauth_expires_at = Column(Float, nullable=True)        # unix timestamp
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    # v3.0.11: Unix timestamp set ONLY by user-facing admin edits. Cluster
    # sync LWW compares this in preference to ``updated_at`` so that
    # auto-refresh of OAuth tokens, deprecation auto-migrations, priority
    # tie-break bumps, etc. on one node cannot clobber a real rename or
    # config edit made on another node. updated_at still bumps on every
    # write — it just no longer gates which write wins across the cluster.
    last_user_edit_at = Column(Float, nullable=True)
    # v2.8.2: tombstone for soft-delete. When non-null, the provider has been
    # deleted on this node but the row stays so cluster sync can propagate the
    # delete to peers (last-write-wins on updated_at). Garbage-collected after
    # all peers have replicated the tombstone.
    deleted_at = Column(DateTime, nullable=True)
    # v3.0.45: provider ownership scoping (root-cause fix for the
    # 2026-05-02 paperless-ai-analyzer burn — paperless ran 17,000
    # gpt-4o calls in 48h on the operator's personal ChatGPT account
    # because there was no tenant boundary on which keys could route to
    # which providers). When ``owned_by_key_id`` is non-null, only that
    # api_key is allowed to route to this provider. Other keys are
    # filtered out at select_provider time and fall back to a different
    # compatible provider — or 503 if none. Null preserves the legacy
    # "shared by all keys" behavior, so this is opt-in per provider.
    owned_by_key_id = Column(String, ForeignKey("api_keys.id"), nullable=True)
    # v3.0.57: cost_class — explicit per-provider billing model. Replaces
    # the previously hardcoded SUBSCRIPTION_TIER_PROVIDER_TYPES set in
    # monitoring/helpers.py. Supports the case where a non-OAuth provider
    # is on a flat-rate enterprise contract (cost_class="subscription"
    # even though provider_type="anthropic-direct"), and the inverse —
    # an OAuth provider on per-call billing if Anthropic ever ships such
    # a tier. NULL preserves the v3.0.50 default behavior: derive from
    # provider_type (claude-oauth/codex-oauth/anthropic-oauth = subscription,
    # everything else = per_call).
    cost_class = Column(String, nullable=True)  # "subscription" | "per_call" | NULL (auto)
    # v3.0.62: per-provider usage-based rotation. Operator-tunable so any
    # OAuth-style "session + weekly quota" provider (claude-oauth, codex-
    # oauth, future grok/azure-oauth) can be tracked. NULL/False fields
    # leave behavior unchanged.
    usage_tracking_enabled = Column(Boolean, default=False)
    usage_session_window_sec = Column(Integer, nullable=True)        # e.g. 18000 = 5h (claude.ai default)
    usage_weekly_reset_dow = Column(Integer, nullable=True)          # 0=Mon … 6=Sun (claude.ai = 6)
    usage_weekly_reset_hour = Column(Integer, nullable=True)         # 0..23 local hour (claude.ai = 16, 4pm)
    usage_session_limit_tokens = Column(Integer, nullable=True)      # operator's estimate of plan ceiling
    usage_weekly_limit_tokens = Column(Integer, nullable=True)
    usage_rotation_threshold_pct = Column(Integer, nullable=True)    # rotate when max/min ratio exceeds this; default 30

    # v3.7.0 — external billing scrape (Anthropic Console).
    # When the operator pastes a captured browser session, store the
    # cookies (JSON string) + organization UUID here. The 4-hourly
    # billing worker reads these and calls
    # ``GET https://claude.ai/api/organizations/{uuid}/usage`` to
    # get authoritative weekly/per-model usage that includes ALL
    # consumption on the account (not just the proxy's slice — see
    # ``project_backlog_anthropic_billing_scrape.md`` for the why).
    # Cookies expire (typically 30+ days for ``sessionKey``); when
    # the worker hits 401/403/Cloudflare it logs a re-auth-needed
    # event and the operator pastes a fresh capture via the admin
    # endpoint at ``POST /api/providers/{id}/anthropic-billing-credentials``.
    anthropic_org_uuid = Column(String, nullable=True)
    anthropic_session_cookies = Column(String, nullable=True)        # JSON dict: {sessionKey, sessionKeyLC, routingHint, lastActiveOrg, cf_clearance, __cf_bm, ...}
    anthropic_session_captured_at = Column(Float, nullable=True)     # unix ts of last operator paste; for "cookies are N days old" UI

    capabilities = relationship("ModelCapability", back_populates="provider", cascade="all, delete-orphan")


class ProviderUsageWindow(Base):
    """v3.0.62: cached per-provider rolling usage totals. Recomputed every
    60s from ``activity_log`` by the usage_tracker task; serves
    ``GET /api/providers/{id}/usage`` reads in O(1)."""
    __tablename__ = "provider_usage_windows"
    provider_id = Column(String, ForeignKey("providers.id"), primary_key=True)
    session_tokens = Column(Integer, default=0)
    session_window_start = Column(DateTime, nullable=True)
    session_pct = Column(Float, nullable=True)        # session_tokens / usage_session_limit_tokens × 100; null if no limit set
    weekly_tokens = Column(Integer, default=0)
    weekly_reset_at = Column(DateTime, nullable=True)  # next reset (last reset + 7 days)
    weekly_pct = Column(Float, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ExternalUsageSnapshot(Base):
    """v3.7.0 — authoritative external usage view scraped from the
    Anthropic Console (``claude.ai/api/organizations/{uuid}/usage``).

    Why this exists: ``ProviderUsageWindow`` (above) tracks tokens
    consumed THROUGH THE PROXY only. The same Anthropic Pro Max
    accounts are used by other channels (Claude Code, mobile app,
    other tools), so the proxy slice ≠ the account total. Rotation
    decisions based on the proxy slice trigger at the wrong time.

    This table stores 4-hourly snapshots of the authoritative weekly
    + per-model utilization figures Anthropic itself reports. The
    cascade / rotation logic consults the latest snapshot before
    falling back to the proxy slice.

    Schema mirrors the captured response shape from 2026-05-10:

      five_hour:                  {utilization, resets_at}
      seven_day:                  {utilization, resets_at}
      seven_day_oauth_apps:       null | {utilization, resets_at}
      seven_day_opus:             null | {utilization, resets_at}
      seven_day_sonnet:           {utilization, resets_at}
      seven_day_cowork:           null | {utilization, resets_at}
      seven_day_omelette:         {utilization, resets_at}
      tangelo / iguana_necktie /
      omelette_promotional:       null | conditional objects
      extra_usage:                {is_enabled, monthly_limit, used_credits, utilization, currency}

    We extract the columnar fields below for easy querying; full
    body is preserved as ``raw_response`` JSON for forward-compat
    with response-shape changes Anthropic ships.
    """
    __tablename__ = "external_usage_snapshot"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False, index=True)
    captured_at = Column(DateTime, server_default=func.now(), index=True)
    source = Column(String, default="anthropic_console_v1")
    # Capture diagnostics
    http_status = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)              # non-null when scrape failed
    auth_state = Column(String, nullable=True)       # "ok" | "session_expired" | "cf_blocked" | "network_error"
    # Core utilization fields (percent 0-100)
    five_hour_utilization = Column(Float, nullable=True)
    five_hour_resets_at = Column(DateTime, nullable=True)
    seven_day_utilization = Column(Float, nullable=True)
    seven_day_resets_at = Column(DateTime, nullable=True)
    seven_day_sonnet_utilization = Column(Float, nullable=True)
    seven_day_sonnet_resets_at = Column(DateTime, nullable=True)
    seven_day_opus_utilization = Column(Float, nullable=True)
    seven_day_opus_resets_at = Column(DateTime, nullable=True)
    # Overage / consumer-pricing
    extra_usage_is_enabled = Column(Boolean, nullable=True)
    extra_usage_monthly_limit = Column(Float, nullable=True)
    extra_usage_used_credits = Column(Float, nullable=True)
    extra_usage_utilization = Column(Float, nullable=True)
    extra_usage_currency = Column(String, nullable=True)
    # Forward-compat catch-all so we can decode new fields without a
    # migration each time Anthropic adds one
    raw_response = Column(Text, nullable=True)


class ModelCapability(Base):
    __tablename__ = "model_capabilities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False)
    model_id = Column(String, nullable=False)
    # v3.0.97: tombstone for cluster-replicated soft delete. Same pattern
    # as Provider/ApiKey/LmrhDim — without this, a hard delete on one
    # node is silently re-inserted by the next sync push from a peer
    # that still has the row.
    deleted_at = Column(DateTime, nullable=True, index=True)
    tasks = Column(JSON, default=list)          # ["reasoning","code","chat",...]
    latency = Column(String, default="medium")  # low|medium|high
    cost_tier = Column(String, default="standard")  # economy|standard|premium
    safety = Column(Integer, default=3)         # 1-5
    context_length = Column(Integer, default=128000)
    regions = Column(JSON, default=list)        # ["us","eu",...]
    modalities = Column(JSON, default=list)     # ["text","vision","audio"]
    native_reasoning = Column(Boolean, default=False)
    native_tools = Column(Boolean, default=True)
    native_vision = Column(Boolean, default=True)
    source = Column(String, default="inferred") # inferred|manual
    # v3.4.1 — alternate spellings the router will accept and route to
    # this same capability row. Solves the "grok-3 vs x-ai/grok-3"
    # leak in /v1/models (same physical model showing as two list
    # entries because both names were registered as separate rows).
    # The router now matches on model_id OR (X IN aliases) so a request
    # for any spelling resolves to the same canonical capability.
    # Empty list means "this entry only matches its bare model_id".
    aliases = Column(JSON, default=list)
    # v3.5.0 (LMRHv2.1) — family / variant grouping for multi-route
    # disambiguation. ``family`` is the upstream model identity
    # (e.g. "grok-3" — same physical model regardless of which
    # provider serves it); ``variant`` is the route flavour
    # (e.g. "web" for the bridge, "openrouter" for the marketplace,
    # "direct" for the vendor API). Both are NULL when not
    # operator-classified — readers should fall back to deriving
    # family from the canonical model_id (strip provider prefix).
    model_family = Column(String, nullable=True)
    model_variant = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    provider = relationship("Provider", back_populates="capabilities")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    name = Column(String, nullable=False)
    key_hash = Column(String, nullable=False, unique=True)
    key_prefix = Column(String, nullable=False)  # first 8 chars for display
    encrypted_key = Column(String, nullable=True)  # Fernet-encrypted full key; NULL for legacy pre-encryption keys
    key_type = Column(String, default="standard")  # standard|claude-code
    enabled = Column(Boolean, default=True)
    total_requests = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    spending_cap_usd = Column(Float, nullable=True)  # lifetime hard cap; None = unlimited
    rate_limit_rpm = Column(Integer, nullable=True)   # None = unlimited (explicit override)
    rate_limit_tier = Column(String, nullable=True)   # Wave 6: named tier (free/starter/pro/enterprise/unlimited). None = custom/rate_limit_rpm only.
    semantic_cache_enabled = Column(Boolean, default=False)  # Wave 1 #3 opt-in
    # Wave 1 #5 — tiered budget caps (None = unlimited at that tier)
    daily_soft_cap_usd = Column(Float, nullable=True)  # warning only; X-Budget-Warning header
    daily_hard_cap_usd = Column(Float, nullable=True)  # 402 Payment Required
    hourly_cap_usd = Column(Float, nullable=True)      # burst control; 429
    # Self-resetting bucket counters (reset when bucket_ts differs from current)
    day_bucket_ts = Column(DateTime, nullable=True)
    day_cost_usd = Column(Float, default=0.0)
    hour_bucket_ts = Column(DateTime, nullable=True)
    hour_cost_usd = Column(Float, default=0.0)
    last_used_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())
    # v3.0.20: tombstone for soft-delete. Same shape as Provider.deleted_at —
    # without this, hard-DELETE on one node was reversed by the next cluster
    # sync push from a peer that still had the row, indistinguishable from
    # a fresh insert. Soft-delete + sync-aware merge fixes the resurrection.
    # Garbage collection of old tombstones is handled by the daily prune sweep.
    deleted_at = Column(DateTime, nullable=True)
    # v3.3.0 LMRHv2 polling-rate overrides. Null on either column means
    # use defaults (4/min providers, 60/min quotes per design doc §4.1).
    # Operators can set these for high-volume orchestrator keys that
    # need tighter polling, or zero them out to disable v2 access for
    # a specific tenant without touching the global flag.
    lmrh_polling_rpm = Column(Integer, nullable=True)
    lmrh_quotes_rpm = Column(Integer, nullable=True)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")  # admin|user
    created_at = Column(DateTime, server_default=func.now())
    timezone = Column(String, nullable=True)      # IANA name; NULL = browser default
    time_format = Column(String, nullable=True)   # '12h'|'24h'|NULL = locale default


class SystemSetting(Base):
    """Key/value store for runtime-tunable settings (overlays env-var defaults)."""
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)        # always stored as string
    value_type = Column(String, default="str")  # str|int|float|bool
    updated_at = Column(Float, default=0.0)     # Unix timestamp — used for last-write-wins sync


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, nullable=False)
    severity = Column(String, default="info")  # info|warning|error|critical
    message = Column(Text)
    provider_id = Column(String)
    api_key_id = Column(String)
    event_meta = Column(JSON, default=dict)
    created_at = Column(DateTime, server_default=func.now())


class ProviderMetric(Base):
    """Time-series health/usage data per provider (5-minute buckets)."""
    __tablename__ = "provider_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, nullable=False)
    bucket_ts = Column(DateTime, nullable=False)   # floored to 5-min
    requests = Column(Integer, default=0)
    successes = Column(Integer, default=0)
    failures = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    avg_latency_ms = Column(Float, default=0.0)
    avg_ttft_ms = Column(Float, default=0.0)
    ttft_requests = Column(Integer, default=0)
    circuit_state = Column(String, default="closed")  # closed|open|half-open
    # v3.4.0 — per-direction cost + token split. total_cost_usd /
    # total_tokens stay as combined sums for back-compat; the new
    # columns let LMRHv2 callers and operators see input-vs-output
    # rates independently (e.g. summarization is output-cheap; context
    # stuffing is input-expensive). Default 0 so older rows still load.
    input_cost_usd = Column(Float, default=0.0)
    output_cost_usd = Column(Float, default=0.0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)


class ModelAlias(Base):
    """Client-facing model name → specific provider + model mapping."""
    __tablename__ = "model_aliases"

    alias = Column(String, primary_key=True)
    provider_id = Column(String, ForeignKey("providers.id", ondelete="CASCADE"), nullable=True)
    model_id = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    # v3.0.97: tombstone for cluster-replicated soft delete.
    deleted_at = Column(DateTime, nullable=True, index=True)


class LmrhDim(Base):
    """v3.0.25: registered LMRH dimension. The protocol's self-extension
    mechanism — apps can register new dims via POST /lmrh/register; the
    proxy collision-resolves (suffix -2/-3 on conflict) and replicates
    the registry to peers via cluster sync. Once registered, both sides
    agree on the canonical name and the proxy stops emitting unknown-dim
    warnings for it.

    Built-in dims (task, cost, latency, safety-min, etc.) are NOT in this
    table — they live in code. This table is for dims registered AT RUNTIME
    by integrating apps. Read of merged-set goes through ``known_dim_names()``
    which combines both.
    """
    __tablename__ = "lmrh_dims"

    name = Column(String, primary_key=True)
    owner_app = Column(String, nullable=True)         # free-form ("paperless-ai-analyzer")
    owner_key_id = Column(String, nullable=True)      # api_keys.id of submitter
    semantics = Column(Text, nullable=True)           # one-paragraph description
    value_type = Column(String, nullable=True)        # "string|int|enum:a,b,c|float"
    kind = Column(String, default="advisory")        # hard|soft|advisory
    examples = Column(JSON, default=list)             # ["task=foo;exclude=bar"]
    requested_name = Column(String, nullable=True)    # what was originally requested
    registered_at = Column(Float, nullable=False)
    registered_by_node = Column(String, nullable=True)
    # v3.0.29: tombstone for cluster-replicated soft delete. Without this,
    # hard-DELETE on one node was reversed by the next sync push from a peer
    # that still had the row. Mirrors the same pattern used for Provider
    # (v2.8.2) and ApiKey (v3.0.20). Stores Unix-epoch float for parity
    # with registered_at.
    deleted_at = Column(Float, nullable=True)


class LmrhProposal(Base):
    """v3.0.25: free-form proposals for dims that the submitter wants
    OPERATOR-REVIEWED before official adoption (vs the auto-register
    path). Distinct from the registry — proposals are read-only-by-admins
    until promoted to a registry entry.
    """
    __tablename__ = "lmrh_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposed_name = Column(String, nullable=False)
    rationale = Column(Text, nullable=True)
    proposer_app = Column(String, nullable=True)
    proposer_key_id = Column(String, nullable=True)
    proposed_at = Column(Float, nullable=False)
    status = Column(String, default="pending")        # pending|accepted|rejected
    review_note = Column(Text, nullable=True)
    # v3.0.29: tombstone for cluster-replicated soft delete (see LmrhDim).
    deleted_at = Column(Float, nullable=True)


class OAuthCaptureProfile(Base):
    """A named OAuth capture configuration. Each profile has its own upstream
    host(s), secret, and enabled flag so multiple CLIs (claude-code, codex,
    gh copilot, …) can be captured concurrently without interference.

    Added in v2.5.0 — replaces the former single-upstream settings model.
    """
    __tablename__ = "oauth_capture_profiles"

    name = Column(String, primary_key=True)  # "claude-code", "codex", "gh-copilot", etc.
    preset = Column(String, nullable=True)   # matches PRESETS key in oauth_capture.py
    upstream_urls = Column(JSON, default=list)  # list[str], typically 1-2 hosts
    secret = Column(String, nullable=True)   # per-profile capture secret
    enabled = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    # v3.0.97: tombstone for cluster-replicated soft delete.
    deleted_at = Column(DateTime, nullable=True, index=True)


class OAuthCaptureLog(Base):
    """Recorded request+response pairs from the OAuth-passthrough endpoint.
    Used to reverse-engineer vendor OAuth flows (claude-code, codex, gh copilot,
    etc.) before implementing a direct `*-oauth` provider.
    """
    __tablename__ = "oauth_capture_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_name = Column(String, nullable=True, index=True)  # v2.5.0: which capture profile
    capture_session = Column(String, nullable=True, index=True)  # optional client-tag
    method = Column(String, nullable=False)
    path = Column(String, nullable=False)          # the subpath of /api/oauth-capture/<profile>/
    upstream_url = Column(String, nullable=False)  # where we actually sent it
    req_headers = Column(JSON, default=dict)
    req_body = Column(Text, nullable=True)         # raw body; may be JSON or form-urlencoded
    req_query = Column(String, nullable=True)
    resp_status = Column(Integer, nullable=True)
    resp_headers = Column(JSON, default=dict)
    resp_body = Column(Text, nullable=True)
    latency_ms = Column(Float, default=0.0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


# ── Run runtime (v3.0 — coordinator-hub spec, R1) ───────────────────────────


class Run(Base):
    """A server-mediated agent loop scoped to one hub task.

    State machine (per spec B.1):
      queued → running → requires_tool → running → ... → completed
              ↘ failed  ↘ expired  ↘ cancelled

    See app/runs/state.py for the FSM. Persistence is per-row; transitions
    bump ``updated_at`` so cluster sync can replicate via last-write-wins.
    """
    __tablename__ = "runs"

    id = Column(String, primary_key=True)         # 'run_' + 16 hex chars
    api_key_id = Column(String, nullable=False, index=True)
    owner_node_id = Column(String, nullable=False)  # which node spawned the worker
    status = Column(String, nullable=False, default="queued")
    current_step = Column(String, nullable=True)    # model_call|tool_dispatch|tool_wait|complete|fail
    deadline_ts = Column(Float, nullable=False)
    max_turns = Column(Integer, nullable=False)
    model_preference = Column(JSON, default=list)   # ordered list of model ids
    compaction_model = Column(String, nullable=True)
    system_prompt = Column(Text, nullable=True)
    tools_spec = Column(JSON, default=list)         # Anthropic-format tool schemas
    metadata_json = Column(JSON, default=dict)
    trace_id = Column(String, nullable=True)        # OTEL parent span id (top-level on create)
    # Counters / accounting
    model_calls = Column(Integer, default=0)
    tool_calls = Column(Integer, default=0)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    last_provider_id = Column(String, nullable=True)
    context_summarized_at_turn = Column(Integer, nullable=True)
    # Pending tool_use waiting for /tool_result
    current_tool_use_id = Column(String, nullable=True)
    current_tool_name = Column(String, nullable=True)
    current_tool_input = Column(JSON, nullable=True)
    # Terminal payloads
    result_text = Column(Text, nullable=True)
    error_kind = Column(String, nullable=True)      # error_provider|tool_loop_exceeded|context_exhausted|...
    error_message = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False)      # Unix; matches idempotency TTL anchor
    updated_at = Column(Float, nullable=False)      # bumped on every transition
    completed_at = Column(Float, nullable=True)


class RunMessage(Base):
    """Conversation history for a Run, ordered by ``seq``.

    Stored verbatim in Anthropic Messages format (role + content blocks).
    Compaction replaces a span of messages with a single 'assistant'
    summary message — see ``compacted_from_seq``/``compacted_to_seq``."""
    __tablename__ = "run_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)           # monotonic per run, dense
    role = Column(String, nullable=False)           # system|user|assistant
    content = Column(JSON, nullable=False)          # str or list[block]
    tokens = Column(Integer, default=0)             # estimate, for compaction trigger
    compacted_from_seq = Column(Integer, nullable=True)  # if this row is a summary
    compacted_to_seq = Column(Integer, nullable=True)
    created_at = Column(Float, nullable=False)


class RunEvent(Base):
    """SSE event ring buffer for a Run. Last 1000 per run kept; older pruned.

    ``seq`` is monotonic per run; SSE clients resume via ``Last-Event-ID``.
    Event ``kind`` matches the spec table (run_started, model_call_start, ...).
    """
    __tablename__ = "run_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)
    payload = Column(JSON, default=dict)
    ts = Column(Float, nullable=False)


class RunIdempotency(Base):
    """``(api_key_id, idempotency_key)`` → ``run_id`` map.

    24h TTL from ``created_at``; lookups beyond TTL miss and a new Run is
    created. Domain is per-API-key per the locked Q1 decision.
    """
    __tablename__ = "run_idempotency"

    api_key_id = Column(String, primary_key=True)
    idempotency_key = Column(String, primary_key=True)  # caller-supplied; ≤256 chars
    run_id = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)
