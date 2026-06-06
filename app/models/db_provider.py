"""Provider domain ORM models.

Split out from ``db.py`` in v4.4.11. This is the heaviest domain
(roughly half of the original file); it owns:

- ``Provider`` — the main provider config row.
- ``ProviderUsageWindow`` — rolling per-provider usage cache.
- ``ProviderNodeAuthState`` — v4.4 M-2 per-node auth state for
  ``grok-web``-style providers with per-node credentialed sessions.
- ``ExternalUsageSnapshot`` — Anthropic Console / Codex scrape results.
- ``ModelCapability`` — per-(provider, model) routing capability rows.
- ``ProviderAiReview`` — AI provider supervisor verdicts (v3.7.30).
- ``ModelToolProbe`` — tool-call probe results (v3.8.4 / #264).
- ``ProviderMetric`` — 5-minute provider health/usage buckets.
- ``ModelAlias`` — client-facing alias → provider/model mapping.
"""
import secrets

from sqlalchemy import (
    Column, String, Integer, Boolean, Float, DateTime, Text, JSON, ForeignKey
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.db_base import Base


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
    # v3.9.5 (#267 Phase 8) — opt-out from the proxy's caller-memory
    # system. When True: extract.py skips memory writes from this
    # provider's responses AND inject.py skips memory injection when
    # this provider is selected by the router. Use cases: keep certain
    # providers "pure" for testing, avoid memory tool surcharge on
    # specific accounts, or comply with per-provider data-residency
    # rules. Default False = participates in memory normally. Gated
    # behind caller_memory_enabled overall.
    memory_disabled = Column(Boolean, default=False)
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

    # v3.7.27 (#245) — Codex / ChatGPT Plus usage scrape. Same problem
    # as the Anthropic Pro Max case: the same ChatGPT Plus subscription
    # is also used outside this proxy (mobile app, chat UI, etc.), so
    # the proxy's local counters undercount the account's actual usage
    # against its weekly cap. The Codex Cloud analytics page at
    # ``https://chatgpt.com/codex/cloud/settings/analytics`` shows the
    # authoritative weekly figure; we scrape it every 4h and store
    # snapshots so the router can use real account totals for
    # rotation decisions.
    #
    # ``codex_usage_endpoint_url`` is operator-supplied because the
    # actual XHR endpoint behind the analytics page is not in any
    # public docs — the operator captures it from browser DevTools
    # (Network panel) on the analytics page and pastes it along with
    # the session cookies. The scraper fires a GET against that URL.
    #
    # ``codex_session_cookies`` is a JSON dict captured the same way:
    # operator copies the chatgpt.com cookies from DevTools →
    # Application → Cookies into a JSON blob and pastes both via
    # ``POST /api/providers/{id}/codex-billing-credentials``.
    codex_session_cookies = Column(String, nullable=True)            # JSON dict of chatgpt.com session cookies
    codex_usage_endpoint_url = Column(String, nullable=True)         # full URL captured from DevTools
    codex_session_captured_at = Column(Float, nullable=True)         # unix ts of last operator paste

    # v3.7.28 (#252 phase 1) — manual override escape hatch for the
    # upcoming AI provider supervisor. When non-null, the supervisor
    # MUST skip this provider entirely (no stats compute, no LLM call,
    # no review row written, no enabled/auto_skip_until mutations).
    # Operator-set via the Disable button in the UI; cleared via
    # Enable. The sentinel string "9999-12-31T23:59:59" represents
    # an indefinite lock; a real DateTime represents a time-bounded
    # lock (reserved — not used in the current UI).
    #
    # Cluster sync replicates these fields via the existing Provider
    # sync path (LWW conflict resolution applies).
    manual_override_until = Column(DateTime, nullable=True)
    manual_override_set_by = Column(String, nullable=True)           # admin user id for audit
    manual_override_set_at = Column(DateTime, nullable=True)
    manual_override_reason = Column(Text, nullable=True)             # optional operator note

    # v3.7.1 — auto-rotation: when an external snapshot reports a
    # provider above the at-capacity threshold (default 95% weekly
    # utilization), the rule evaluator sets ``auto_skip_until`` to
    # the snapshot's ``seven_day_resets_at``. The router skips this
    # provider until that timestamp passes — at which point the next
    # scrape produces a fresh snapshot whose utilization will drop
    # post-reset, and the rule evaluator clears the field. Operator
    # configured ``Provider.priority`` is preserved unchanged.
    # ``auto_skip_reason`` is a short human-readable string for the
    # admin UI / activity log so the operator sees WHY a provider is
    # being skipped automatically.
    auto_skip_until = Column(DateTime, nullable=True)
    auto_skip_reason = Column(String, nullable=True)

    # v5.0.0 — owner company for compliance enforcement (decision 14, 17).
    # Auto-derived at create/update time from provider_type via
    # ``app.compliance.company_map.provider_type_to_company`` (e.g.
    # provider_type="anthropic" → owner_company="anthropic"). Operator can
    # override via PATCH for unusual cases (a self-hosted Llama deployment
    # that should be classified as "internal" instead of "meta", say). The
    # router pre-filter drops any provider whose owner_company is in a key's
    # effective blocklist.
    owner_company = Column(String, nullable=True)

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


class ProviderNodeAuthState(Base):
    """v4.4 M-2 — per-node auth state for providers that maintain a
    node-local credentialed session (currently only ``grok-web`` via
    its per-node bridge container, but the table is generic enough
    to cover any future per-node-session provider type).

    Why this table exists: pre-v4.4 grok-web ran on a shared bridge
    on tmrwww01 only, and the CB state cluster-synced like every
    other provider. When the shared bridge crashed (BUG-025), grok-
    web went down fleet-wide. The v4.4 per-node-bridge design
    (`docs/4.4-per-node-bridge-design.md` Path A) gives each proxy
    node its own bridge with its own logged-in session for the same
    operator account — but then the cluster needs a *cluster-synced
    view of the per-node states*, written by each node about its own
    bridge, read by every node to inform routing + UI display.

    Schema:
    - ``(provider_id, node_id)`` composite PK.
    - ``auth_state`` is one of ``ok | expired | needs_reauth |
      never_authed | bridge_down``. The router consults this to
      filter grok-web routing per-node (Path A §4.3 of the design).
    - ``last_ok_at`` / ``last_check_at`` are operator-facing
      timestamps; ``reauth_url`` is the pre-signed deep link the UI
      gives the operator when ``auth_state != "ok"``.

    Cluster-sync direction: each node writes ONLY its own row(s),
    cluster sync propagates ALL rows. Other nodes' rows are read-
    only on this node — never overwrite a peer's row even if our
    sync sees a stale timestamp.
    """
    __tablename__ = "provider_node_auth_state"
    provider_id = Column(String, ForeignKey("providers.id"), primary_key=True)
    node_id = Column(String, primary_key=True)
    auth_state = Column(String, nullable=False, default="never_authed")
    last_ok_at = Column(DateTime, nullable=True)
    last_check_at = Column(DateTime, server_default=func.now(), index=True)
    # Optional pre-signed re-auth deep link. The bridge populates this
    # when it transitions to needs_reauth (e.g. cookies expired); the
    # admin UI surfaces it as the per-node [Re-auth] button target.
    reauth_url = Column(String, nullable=True)
    # Optional last error string for operator-facing diagnostics —
    # capped at ~400 chars at write time to keep the cluster sync
    # payload bounded.
    last_error = Column(String, nullable=True)


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
    # v5.0.24 — Cursor membership tier (free / pro / business). Used to
    # detect plan downgrades that silently break routing. When a Pro
    # account is downgraded to Free, Cursor's API returns
    # ``ERROR_RATE_LIMITED_CHANGEABLE`` "Named models unavailable.
    # Free plans can only use Auto" — the cursor-bridge wraps this as
    # an HTTP 200 with empty content, so the proxy can't tell from the
    # response alone. The billing scrape DOES see the membership
    # change, so we record it here and the rotation evaluator auto-
    # skips the provider on Pro→Free.
    membership_tier = Column(String, nullable=True)  # "free" | "pro" | "business" | …
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
    # v3.8.5 (#265) — rolling tool-call success rate from the v3.8.4
    # prober. Null = no probe data yet (router falls back to binary
    # native_tools). Populated by ai_tool_prober's
    # update_native_tools_from_rolling() helper.
    tool_call_success_rate = Column(Float, nullable=True)
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


class ProviderAiReview(Base):
    """v3.7.30 (#252 phase 3) — provider-level mirror of ``ApiKeyAiReview``.

    A background worker (Phase 4) scans recent activity for each
    provider on a configurable cadence (default 30 min), computes a
    stats summary including TTFT p50/p95 and response-length trends,
    sends it to an LLM for classification, and writes a row here.

    Operator reviews via admin endpoints (Phase 5). When
    ``ai_provider_supervisor_auto_apply=True``, deprioritize/disable
    verdicts mutate Provider.priority / auto_skip_until — but ONLY
    for providers without ``manual_override_until`` set (Phase 1
    escape hatch).

    Verdict enum:
      - ``normal``       — healthy; no action
      - ``watch``        — slightly elevated; record but don't act
      - ``deprioritize`` — recommend Provider.priority += N
      - ``disable``      — recommend Provider.enabled = False
      - ``investigate``  — anomaly detected, operator should look manually

    Cluster sync replicates this table via the BUG-016 pattern (added
    in Phase 4/5 ship).
    """
    __tablename__ = "provider_ai_review"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False, index=True)
    captured_at = Column(DateTime, server_default=func.now(), index=True)
    llm_model = Column(String, nullable=True)
    llm_verdict = Column(String, nullable=False)
    llm_reasoning = Column(Text, nullable=True)
    suggested_priority_delta = Column(Integer, nullable=True)
    suggested_auto_skip_hours = Column(Integer, nullable=True)
    stats_summary = Column(JSON, nullable=True)  # input stats for diagnostics
    # Lifecycle: applied / dismissed / reverted (mirrors ApiKeyAiReview)
    applied_at = Column(DateTime, nullable=True)
    applied_action = Column(String, nullable=True)
    prior_priority = Column(Integer, nullable=True)             # for revert
    prior_auto_skip_until = Column(DateTime, nullable=True)     # for revert
    reverted_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)


class ModelToolProbe(Base):
    """v3.8.4 (#264) — periodic tool-call probe results.

    The tool capability prober fires a standard ``get_weather(city)``
    tool-call request at every (provider, default_model) and records
    whether the model:
      - returned ANY tool_call block (``called=True``)
      - returned a parseable tool_call with the expected name + args
        (``parseable=True``)
      - returned the expected city argument (``correct_city=True``)

    A rolling window of the last N probes drives
    ``ModelCapability.native_tools`` via hysteresis: <60% success →
    native_tools=False (engage emulation); >=80% → native_tools=True
    (trust native).

    Table is per-node; cluster sync optional (probe results are
    deterministic-ish per node since the same prompt should produce
    the same answer, but rate-limit / network-error skew can differ).
    """
    __tablename__ = "model_tool_probe"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False, index=True)
    model_id = Column(String, nullable=False, index=True)
    captured_at = Column(DateTime, server_default=func.now(), index=True)
    # Outcome flags
    called = Column(Boolean, default=False)         # did the response contain ANY tool_call?
    parseable = Column(Boolean, default=False)      # was the tool_call name + JSON args parseable?
    correct_args = Column(Boolean, default=False)   # did the args contain the expected key?
    # Diagnostic context
    error = Column(Text, nullable=True)             # non-null on http / network errors
    raw_excerpt = Column(Text, nullable=True)       # first 500 chars of model output for inspection
    response_format = Column(String, nullable=True) # "native" | "emulated" | None


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
