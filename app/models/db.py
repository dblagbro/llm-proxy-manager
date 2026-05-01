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

    capabilities = relationship("ModelCapability", back_populates="provider", cascade="all, delete-orphan")


class ModelCapability(Base):
    __tablename__ = "model_capabilities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False)
    model_id = Column(String, nullable=False)
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


class ModelAlias(Base):
    """Client-facing model name → specific provider + model mapping."""
    __tablename__ = "model_aliases"

    alias = Column(String, primary_key=True)
    provider_id = Column(String, ForeignKey("providers.id", ondelete="CASCADE"), nullable=True)
    model_id = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


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
