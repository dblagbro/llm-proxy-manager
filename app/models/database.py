from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import event
from app.config import settings
from app.models.db import Base
# v3.2.11: auto-bump Provider.last_user_edit_at on user-meaningful column
# changes. Side-effect import — registers a SQLAlchemy event listener.
# See _user_edit_stamp.py for design rationale.
from app.models import _user_edit_stamp  # noqa: F401
import logging
import time
import traceback

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # v3.0.61: bump pool capacity + tighten checkout timeout. Default
    # was 5+10=15 connections with 30s wait. During the 2026-05-05
    # outage that drained in seconds while every connection was held
    # by stuck upstream calls, leaving /health and DB-backed endpoints
    # blocked for 30s+ waiting their turn.
    # v3.0.92: bump again. The 2026-05-06 incident showed 20+30=50
    # was still drainable under sustained background-task load when
    # activity_log hit 1 GB and json_extract scans got slow. Bumping
    # to 50 base + 100 overflow = 150 connections max. SQLite handles
    # this fine (in-process, file-backed, no network overhead per
    # connection). Plus pool_recycle=1800 to age out long-held conns
    # in case there's a slow leak we haven't found yet — a 30-min
    # ceiling keeps the pool fresh.
    pool_size=50,
    max_overflow=100,
    pool_timeout=10.0,
    pool_recycle=1800,
)


# v3.0.3: every SQLite connection from the pool needs busy_timeout so it
# waits instead of failing on write contention. WAL is db-file-level,
# so a one-time PRAGMA in init_db sticks; busy_timeout is per-connection
# and must be re-applied at checkout. Sync hook because SQLAlchemy fires
# the event with a sync connection object.
if "sqlite" in settings.database_url:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _conn_record):
        try:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
        except Exception as e:
            logger.warning(f"SQLite PRAGMA setup failed: {e}")

# v3.10.2 (ARCH-A) — DB connection-pool checkout tracer. The latent
# pool leak (www01 + GCP saturated QueuePool 13-20h post-deploy,
# returning /health 500s) has an unknown root cause: every
# ``AsyncSessionLocal()`` is ``async with``-wrapped, so it is not naive
# session leakage. This tracer records the acquisition stack of every
# pool checkout; a connection that never checks back in keeps its
# entry, and its stack names the leaking code path. Default OFF
# (``settings.db_pool_trace``) — ``format_stack()`` per checkout has
# overhead. Enable on one node, recreate the container, then drive the
# harness (``scripts/archa_pool_leak_harness.py``) or just wait.
_pool_checkouts: dict[int, dict] = {}

# v4.4.22 (ARCH-A redux) — async-side session tracer. The v4.4.19 fix
# corrected the slicing direction but the captured stack was STILL
# all SQLAlchemy internals: ``format_stack()`` walks the sync stack,
# but SQLAlchemy's async adapter dispatches DB ops via a greenlet,
# and the greenlet has its own stack separate from the async caller.
# The 2026-05-27 dig confirmed this — py-spy showed 10 aiosqlite
# worker threads (one per pooled conn) but the pool-event hook
# captured stacks ended at ``session.execute()`` with no app frames
# above. To see app code, we have to capture on the async side —
# specifically in ``AsyncSession.__aenter__``, which runs on the
# caller's coroutine where ``await db = AsyncSessionLocal()`` lives.
_async_session_traces: dict[str, dict] = {}


def get_pool_checkout_trace() -> list[dict]:
    """Pooled connections currently checked out, oldest first. Each
    entry is ``{age_sec, stack}``. Empty when tracing is off or the
    pool is idle. Under the suspected leak the oldest entries — held
    far longer than any request should take — name the culprit path.

    v4.4.22 NOTE: this is the SYNC tracer; its stacks lose app frames
    due to the greenlet boundary. ``get_async_session_trace()`` is the
    one to read for "which app code is holding the session." This one
    is kept because the pool *checkout* events fire on the sync side
    and ``id(conn_record)`` is the only stable identifier that
    survives connection re-use across sessions."""
    now = time.monotonic()
    out = [
        {"age_sec": round(now - rec["since"], 1), "stack": rec["stack"]}
        for rec in list(_pool_checkouts.values())
    ]
    out.sort(key=lambda e: e["age_sec"], reverse=True)
    return out


def get_async_session_trace() -> list[dict]:
    """Async-side companion to the pool checkout trace.

    Each entry is ``{age_sec, session_id, stack}``. The stack is
    captured at ``AsyncSession.__aenter__``, so it includes the app
    code that opened the session — unlike the sync pool tracer
    whose stacks dead-end at SQLAlchemy internals because the
    greenlet boundary clips the async caller's frames.

    Sorted oldest-first. Sessions held across an unexpected ``await``
    will show up here with their originating ``async with``."""
    now = time.monotonic()
    out = [
        {"age_sec": round(now - rec["since"], 1),
         "session_id": rec["session_id"],
         "stack": rec["stack"]}
        for rec in list(_async_session_traces.values())
    ]
    out.sort(key=lambda e: e["age_sec"], reverse=True)
    return out


# Choose the AsyncSession class the sessionmaker hands out: a traced
# subclass when ``db_pool_trace`` is on, plain AsyncSession otherwise.
if settings.db_pool_trace:
    import secrets as _secrets

    class _TracedAsyncSession(AsyncSession):
        """Captures the async-side calling stack at ``__aenter__`` and
        clears the entry on ``__aexit__``.

        The capture happens on the coroutine running the
        ``async with AsyncSessionLocal()`` — i.e. the app code, where
        ``format_stack()`` walks the real Python stack and includes
        the caller. Compare with the sync pool-event hook, whose
        ``format_stack()`` runs inside a SQLAlchemy greenlet and
        sees only internals."""

        async def __aenter__(self):
            self._traced_session_id = _secrets.token_hex(8)
            try:
                stack = "".join(traceback.format_stack()[:-1])
                _async_session_traces[self._traced_session_id] = {
                    "session_id": self._traced_session_id,
                    "since": time.monotonic(),
                    "stack": stack,
                }
            except Exception:
                # Tracing must never fail the actual DB use.
                pass
            return await super().__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            try:
                sid = getattr(self, "_traced_session_id", None)
                if sid:
                    _async_session_traces.pop(sid, None)
            except Exception:
                pass
            return await super().__aexit__(exc_type, exc, tb)

    _session_class = _TracedAsyncSession
else:
    _session_class = AsyncSession

AsyncSessionLocal = async_sessionmaker(
    engine, class_=_session_class, expire_on_commit=False
)


if settings.db_pool_trace:
    @event.listens_for(engine.sync_engine, "checkout")
    def _trace_pool_checkout(_dbapi_conn, conn_record, _conn_proxy):
        # v4.4.19 — was ``[-45:]``, dropped. ``format_stack()`` returns
        # outermost-first / innermost-last, so ``[-45:]`` keeps the
        # *innermost* 45 frames = the SQLAlchemy pool-checkout chain +
        # this hook. The app caller lives in the *outer* frames, which
        # is what we want for leak hunting — exactly the part the slice
        # was discarding. (Both v3.10.13's bump 18→45 and the original
        # ``[-18:]`` had this direction wrong; the 2026-05-26 ARCH-A
        # trace on www01 returned an all-SQLA stack despite a 59h
        # leaked checkout, which is what surfaced it.) Drop the
        # trailing ``format_stack`` frame only.
        # v4.4.22 NOTE: the greenlet boundary defeats this whichever
        # way we slice — it's kept for sync-side coverage but the
        # async-side ``_TracedAsyncSession`` is the one whose stacks
        # actually name app code. See ``get_async_session_trace()``.
        stack = "".join(traceback.format_stack()[:-1])
        _pool_checkouts[id(conn_record)] = {
            "since": time.monotonic(), "stack": stack,
        }

    @event.listens_for(engine.sync_engine, "checkin")
    def _trace_pool_checkin(_dbapi_conn, conn_record):
        _pool_checkouts.pop(id(conn_record), None)

    logger.warning("ARCH-A: DB pool checkout tracing ENABLED (db_pool_trace=1)")


async def init_db():
    # v5.20.4 — import the ModelPricingEntry ORM class so
    # Base.metadata.create_all sees the new table.
    from app.models import db_model_pricing  # noqa: F401
    async with engine.begin() as conn:
        # v3.0.3: enable WAL + busy_timeout for SQLite. Without these,
        # concurrent writers (cluster /sync receivers + keep-alive probes
        # + run worker events + activity log) hit "database is locked"
        # under load. WAL lets readers and writers proceed concurrently;
        # busy_timeout makes writers wait briefly instead of failing
        # immediately. Idempotent — running on every startup is fine.
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA busy_timeout=10000")  # 10s
        await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")  # safe with WAL
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns to existing tables (SQLite doesn't support IF NOT EXISTS for columns)
        for stmt in [
            "ALTER TABLE providers ADD COLUMN hold_down_sec INTEGER",
            "ALTER TABLE providers ADD COLUMN failure_threshold INTEGER",
            "ALTER TABLE system_settings ADD COLUMN updated_at REAL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN spending_cap_usd REAL",
            "ALTER TABLE api_keys ADD COLUMN rate_limit_rpm INTEGER",
            "ALTER TABLE provider_metrics ADD COLUMN avg_ttft_ms REAL DEFAULT 0",
            "ALTER TABLE provider_metrics ADD COLUMN ttft_requests INTEGER DEFAULT 0",
            "ALTER TABLE providers ADD COLUMN daily_budget_usd REAL",
            "ALTER TABLE api_keys ADD COLUMN semantic_cache_enabled INTEGER DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN daily_soft_cap_usd REAL",
            "ALTER TABLE api_keys ADD COLUMN daily_hard_cap_usd REAL",
            "ALTER TABLE api_keys ADD COLUMN hourly_cap_usd REAL",
            "ALTER TABLE api_keys ADD COLUMN day_bucket_ts DATETIME",
            "ALTER TABLE api_keys ADD COLUMN day_cost_usd REAL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN hour_bucket_ts DATETIME",
            "ALTER TABLE api_keys ADD COLUMN hour_cost_usd REAL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN encrypted_key TEXT",
            "ALTER TABLE api_keys ADD COLUMN rate_limit_tier TEXT",
            # v2.5.0 — multi-profile OAuth capture
            "ALTER TABLE oauth_capture_log ADD COLUMN profile_name TEXT",
            # v2.7.0 — claude-oauth provider type
            "ALTER TABLE providers ADD COLUMN oauth_refresh_token TEXT",
            "ALTER TABLE providers ADD COLUMN oauth_expires_at REAL",
            # v2.8.2 — soft-delete tombstone for cluster-sync resurrection bug
            "ALTER TABLE providers ADD COLUMN deleted_at DATETIME",
            # v3.0 R1 — per-user UTC/timezone preferences (Q7 interleave)
            "ALTER TABLE users ADD COLUMN timezone TEXT",
            "ALTER TABLE users ADD COLUMN time_format TEXT",
            # v3.0.11 — sync LWW now prefers user-edit timestamp over updated_at
            "ALTER TABLE providers ADD COLUMN last_user_edit_at REAL",
            # v4.4.20 — same LWW gate, now for api_keys. The v4.4.18
            # field-coverage fix was effectively "last sync wins"
            # because api_keys had no per-row admin-edit timestamp;
            # this column closes the gap.
            "ALTER TABLE api_keys ADD COLUMN last_user_edit_at REAL",
            # v3.0.20 — ApiKey soft-delete tombstone for cluster-sync resurrection bug
            "ALTER TABLE api_keys ADD COLUMN deleted_at DATETIME",
            # v5.0.22 — same fix for users (BUG-070). User deletes
            # were being silently undone by peer sync since users
            # had no tombstone + the sync handler at sync.py:73 was
            # an insert-if-missing merge. Now users.deleted_at +
            # last_user_edit_at carry the LWW intent across nodes.
            "ALTER TABLE users ADD COLUMN deleted_at DATETIME",
            "ALTER TABLE users ADD COLUMN last_user_edit_at REAL",
            # v5.0.24 — cursor billing scrape captures membership tier
            # (BUG-053). Pro→Free downgrade silently breaks Cursor
            # routing; the scrape lands the new tier, the rotation
            # evaluator auto-skips on downgrade. Free→Pro upgrade
            # clears any auto-skip set by the downgrade.
            "ALTER TABLE external_usage_snapshot ADD COLUMN membership_tier TEXT",
            # v3.0.25 — LMRH self-extension protocol tables
            # (idempotent ALTERs; CREATE handled by Base.metadata.create_all above)
            # v3.0.29 — soft-delete tombstones on LMRH dim/proposal rows so
            # cluster-sync's "insert if missing" merge doesn't resurrect a
            # deleted entry from a peer that still has it.
            "ALTER TABLE lmrh_dims ADD COLUMN deleted_at REAL",
            "ALTER TABLE lmrh_proposals ADD COLUMN deleted_at REAL",
            # v3.0.45 — provider ownership scoping. owned_by_key_id is FK
            # to api_keys.id; when set, only that key can route to this
            # provider. Closes the 2026-05-02 paperless-ai-analyzer burn
            # (17k gpt-4o calls in 48h on the operator's personal ChatGPT
            # account because there was no tenant boundary on providers).
            "ALTER TABLE providers ADD COLUMN owned_by_key_id TEXT",
            # v3.0.57 — explicit per-provider cost_class. NULL = derive
            # from provider_type (claude-oauth/codex-oauth/anthropic-oauth
            # = subscription, all else = per_call). Set explicitly when
            # an admin needs to override (e.g. anthropic-direct on a
            # flat-rate enterprise contract → cost_class="subscription").
            "ALTER TABLE providers ADD COLUMN cost_class TEXT",
            # v3.0.62 — per-provider usage tracking + rotation tuning.
            "ALTER TABLE providers ADD COLUMN usage_tracking_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE providers ADD COLUMN usage_session_window_sec INTEGER",
            "ALTER TABLE providers ADD COLUMN usage_weekly_reset_dow INTEGER",
            "ALTER TABLE providers ADD COLUMN usage_weekly_reset_hour INTEGER",
            "ALTER TABLE providers ADD COLUMN usage_session_limit_tokens INTEGER",
            "ALTER TABLE providers ADD COLUMN usage_weekly_limit_tokens INTEGER",
            "ALTER TABLE providers ADD COLUMN usage_rotation_threshold_pct INTEGER",
            # v3.0.97 — soft-delete tombstones for cluster-replicated catalog
            # tables. Without these, hard-DELETE on one node was reversed by
            # the next sync push from a peer that still had the row.
            "ALTER TABLE model_capabilities ADD COLUMN deleted_at DATETIME",
            "ALTER TABLE model_aliases ADD COLUMN deleted_at DATETIME",
            "ALTER TABLE oauth_capture_profiles ADD COLUMN deleted_at DATETIME",
            # v3.3.0 — LMRHv2 per-key polling-rate overrides. Null = use
            # global defaults (4/min providers, 60/min quotes).
            "ALTER TABLE api_keys ADD COLUMN lmrh_polling_rpm INTEGER",
            "ALTER TABLE api_keys ADD COLUMN lmrh_quotes_rpm INTEGER",
            # v3.4.0 — per-direction cost split in provider_metrics.
            # The combined ``total_cost_usd`` was insufficient for LMRHv2
            # callers wanting to optimize input-heavy or output-heavy
            # workloads independently (e.g. summarization is output-cheap
            # vs context-stuffing being input-expensive). pricing.py was
            # already returning a tuple from cost_per_token; we just
            # weren't storing the split. New columns are nullable + default
            # 0 so the migration is safe on existing rows.
            "ALTER TABLE provider_metrics ADD COLUMN input_cost_usd REAL DEFAULT 0",
            "ALTER TABLE provider_metrics ADD COLUMN output_cost_usd REAL DEFAULT 0",
            "ALTER TABLE provider_metrics ADD COLUMN input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE provider_metrics ADD COLUMN output_tokens INTEGER DEFAULT 0",
            # v3.4.1 — alias column on model_capabilities. Lets one
            # canonical model_id absorb multiple input spellings
            # (e.g. ``x-ai/grok-3`` accepts ``grok-3`` as alias).
            # Solves the /v1/models leak where the same physical
            # model showed as two list entries.
            "ALTER TABLE model_capabilities ADD COLUMN aliases JSON",
            # v3.5.0 (LMRHv2.1) — family/variant grouping for multi-
            # route disambiguation. ``family`` = upstream model
            # identity (same physical model), ``variant`` = route
            # flavour (web/api/openrouter/direct/etc).
            "ALTER TABLE model_capabilities ADD COLUMN model_family TEXT",
            "ALTER TABLE model_capabilities ADD COLUMN model_variant TEXT",
            # v3.7.0 — external billing scrape (Anthropic Console).
            # Stores cookies + organization UUID for the
            # claude.ai/api/organizations/{uuid}/usage endpoint. The
            # `external_usage_snapshot` table is created via
            # Base.metadata.create_all above; these columns annotate
            # the existing Provider rows that drive the scraper.
            "ALTER TABLE providers ADD COLUMN anthropic_org_uuid TEXT",
            "ALTER TABLE providers ADD COLUMN anthropic_session_cookies TEXT",
            "ALTER TABLE providers ADD COLUMN anthropic_session_captured_at REAL",
            # v3.7.1 — auto-rotation skip. Cleared automatically once
            # the next snapshot shows utilization back below threshold
            # (or simply when the timestamp passes — router compares
            # at request time). Doesn't touch operator-configured
            # priority/enabled fields.
            "ALTER TABLE providers ADD COLUMN auto_skip_until DATETIME",
            "ALTER TABLE providers ADD COLUMN auto_skip_reason TEXT",
            # v3.7.10 — proactive AI rate limiter. The new
            # ``api_key_ai_review`` table is created via
            # Base.metadata.create_all; no Provider columns here.
            # ApiKey doesn't need new columns either — we read its
            # existing rate_limit_rpm and write back to it when
            # auto-applying a throttle suggestion. (prior_rate_limit_rpm
            # is stored on the review row for revert.)
            # v3.7.12 — new column for the IP that the LLM thinks
            # should be blocked when verdict == "block_ip".
            "ALTER TABLE api_key_ai_review ADD COLUMN suggested_block_ip TEXT",
            # v3.7.15 — BUG-016: soft-delete tombstone on blocked_ips
            # so DELETE propagates through cluster sync. Middleware +
            # admin listing filter ``deleted_at IS NULL``.
            "ALTER TABLE blocked_ips ADD COLUMN deleted_at DATETIME",
            # v3.7.27 (#245) — Codex / ChatGPT Plus usage scrape.
            # Mirrors the Anthropic billing scrape: operator captures
            # the chatgpt.com analytics XHR endpoint from DevTools and
            # pastes it along with session cookies; 4h worker fires a
            # GET and stores the response in ``external_usage_snapshot``
            # with source ``chatgpt_codex_v1`` for forward-compat with
            # field extraction once the response shape is confirmed.
            "ALTER TABLE providers ADD COLUMN codex_session_cookies TEXT",
            "ALTER TABLE providers ADD COLUMN codex_usage_endpoint_url TEXT",
            "ALTER TABLE providers ADD COLUMN codex_session_captured_at REAL",
            # v3.7.28 (#252 phase 1) — manual override escape hatch
            # for the AI provider supervisor. When set, supervisor must
            # skip this provider; operator's explicit Disable click is
            # sticky until they Enable again. Cluster sync replicates
            # via the existing Provider sync path.
            "ALTER TABLE providers ADD COLUMN manual_override_until DATETIME",
            "ALTER TABLE providers ADD COLUMN manual_override_set_by TEXT",
            "ALTER TABLE providers ADD COLUMN manual_override_set_at DATETIME",
            "ALTER TABLE providers ADD COLUMN manual_override_reason TEXT",
            # v3.8.0 (#251) — rename provider_type value from
            # "codex-oauth" to "ChatGPT-oauth-plan". One-shot UPDATE
            # that's safe to re-run (no-op when no rows match the old
            # value). Cluster sync propagates the new value via the
            # existing Provider sync path so peer nodes converge on
            # the new name even if they haven't been restarted yet.
            "UPDATE providers SET provider_type='ChatGPT-oauth-plan' WHERE provider_type='codex-oauth'",
            # v3.8.2 (#261) — backfill manual_override_until on
            # providers that were Disabled BEFORE v3.7.28 shipped the
            # sticky-disable mechanism. Without this, the AI provider
            # supervisor (when enabled) would see legacy-disabled
            # providers as fair game for auto-re-enable, defeating
            # the operator's original disable intent.
            #
            # Safe to re-run: no-op when no rows match. Uses the
            # ``9999-12-31 23:59:59`` sentinel that toggle_provider
            # writes for indefinite locks.
            "UPDATE providers SET manual_override_until='9999-12-31 23:59:59' WHERE enabled=0 AND manual_override_until IS NULL AND deleted_at IS NULL",
            # v3.8.3 (#263) — flip native_tools=False on every Grok-Web
            # ModelCapability row. Grok-Web is a Playwright-driven
            # screen-scrape of grok.com chat — it has NO native function-
            # calling support. Prior native_tools=True was a default-True
            # assumption that the tool-emulation audit caught.
            #
            # Once these rows flip, has_tools requests routed to Grok-Web
            # will engage the emulation layer (system-prompt injection +
            # <tool_call> marker parsing) instead of pretending grok.com
            # natively supports tools.
            "UPDATE model_capabilities SET native_tools=0 WHERE provider_id IN (SELECT id FROM providers WHERE provider_type='grok-web')",
            # v3.8.5 (#265) — rolling tool-call success rate column
            # populated by the v3.8.4 tool prober. Router uses it to
            # weight candidates on has_tools=True requests.
            "ALTER TABLE model_capabilities ADD COLUMN tool_call_success_rate REAL",
            # v3.9.5 (#267 Phase 8) — per-provider opt-out from the
            # caller-memory system. extract.py + inject.py both gate
            # on this. Default 0 = participates normally.
            "ALTER TABLE providers ADD COLUMN memory_disabled BOOLEAN DEFAULT 0",
            # v3.9.13 (#267 follow-up) — per-key caller-memory retention.
            # NULL = no TTL (current behavior); integer = sweeper
            # tombstones rows older than N days.
            "ALTER TABLE api_keys ADD COLUMN caller_memory_ttl_days INTEGER",
            # v5.0.0 — compliance policy fields. blocked_companies is a JSON
            # list of company IDs to block for this key (unioned at request
            # time with the system-wide setting). allowed_paths is a JSON
            # list of normalized request paths; NULL = unrestricted. Both
            # are read by the router pre-filter + allowed_paths middleware.
            "ALTER TABLE api_keys ADD COLUMN blocked_companies TEXT",
            "ALTER TABLE api_keys ADD COLUMN allowed_paths TEXT",
            # v5.2.0 / Batch V2 — fine-grained vendor-neutrality policy.
            # allowed_companies = positive allowlist (NULL = no allowlist);
            # blocked_models + allowed_models = per-model exact-or-glob
            # gates that apply on top of company rules. See
            # ``app/compliance/policy.evaluate_policy`` for the merge
            # semantics. Deny wins everywhere; per-key unions with the
            # system-wide settings of the same names.
            "ALTER TABLE api_keys ADD COLUMN allowed_companies TEXT",
            "ALTER TABLE api_keys ADD COLUMN blocked_models TEXT",
            "ALTER TABLE api_keys ADD COLUMN allowed_models TEXT",
            # debug_echo_enabled gates the /api/debug/echo-client sandbox
            # endpoint; production keys leave it 0.
            "ALTER TABLE api_keys ADD COLUMN debug_echo_enabled BOOLEAN DEFAULT 0",
            # v5.7.1 — system_prompt_mcp_augmentation: per-key opt-in
            # nudge added to body["system"] so the model prefers calling
            # proxy-injected tools over saying "I can't read X". Default 0.
            "ALTER TABLE api_keys ADD COLUMN system_prompt_mcp_augmentation BOOLEAN DEFAULT 0",
            # v5.7.4 — MCP per-key policy. JSON-typed fields hold lists of
            # fnmatch globs (e.g. ["read_*"]) or NULL. mcp_schema_token_budget
            # is an INT (NULL = unlimited).
            "ALTER TABLE api_keys ADD COLUMN mcp_tools_allow TEXT",
            "ALTER TABLE api_keys ADD COLUMN mcp_tools_deny TEXT",
            "ALTER TABLE api_keys ADD COLUMN mcp_schema_token_budget INTEGER",
            # v5.20.0 — per-key refusal detection + prompt hardening. All
            # default 0 (False) so pre-existing keys see no behavior
            # change. refusal_retry_enabled is reserved for v5.20.1 —
            # column is present so admins can pre-toggle it before the
            # retry path ships. See app/refusal_detection.py.
            "ALTER TABLE api_keys ADD COLUMN refusal_detection_enabled INTEGER DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN refusal_prompt_hardening INTEGER DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN refusal_retry_enabled INTEGER DEFAULT 0",
            # v5.20.1 — cascade attempts cap. NULL = default 3.
            "ALTER TABLE api_keys ADD COLUMN refusal_retry_max_attempts INTEGER",
            # v5.20.2 — self-edit permissions for the AI Integration
            # Protocol. JSON list of field names the key holder can
            # update via POST /api/integration/self-update. NULL =
            # self-edit disabled.
            "ALTER TABLE api_keys ADD COLUMN self_edit_permissions TEXT",
            # owner_company is auto-derived at provider create/update time
            # from provider_type via app.compliance.company_map; operator
            # can override for unusual rows. The router pre-filter drops
            # providers whose owner_company is in a key's effective
            # blocklist.
            "ALTER TABLE providers ADD COLUMN owner_company TEXT",
            # source_company on caller_memory + caller_memory_marker is
            # resolved at write time from the serving provider's
            # owner_company. Memory rows whose source_company is banned
            # for a request are filtered out at read time (decision 7:
            # unknown=blocked, so NULL is also treated as blocked).
            "ALTER TABLE caller_memory ADD COLUMN source_company TEXT",
            "ALTER TABLE caller_memory_marker ADD COLUMN source_company TEXT",
            # v5.15.0 (#508) — per-account OAuth fan-out. Provider-level
            # override for account-pick strategy; NULL = inherit app-wide
            # default (currently 'least_utilized'). Values:
            # 'least_utilized' | 'round_robin' | 'least_recently_used'.
            # The new provider_oauth_accounts TABLE is created by
            # Base.metadata.create_all above; only the column ALTER lands
            # here.
            "ALTER TABLE providers ADD COLUMN oauth_account_strategy TEXT",
        ]:
            try:
                await conn.exec_driver_sql(stmt)
            except Exception:
                pass  # column already exists

        # v2.7.8 BUG-017: indexes for hot lookup paths.
        # These are CREATE INDEX IF NOT EXISTS so reapplying is a no-op.
        # Without these, every authenticated request did a full scan of
        # api_keys, and activity-log queries scanned the full table.
        for index_stmt in [
            # Authenticated requests look up api_keys by key_hash on every call
            "CREATE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys(key_hash)",
            # Activity log: most queries are "recent events" or "recent events for provider X"
            "CREATE INDEX IF NOT EXISTS ix_activity_log_created_at ON activity_log(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_activity_log_provider_id ON activity_log(provider_id)",
            "CREATE INDEX IF NOT EXISTS ix_activity_log_severity ON activity_log(severity)",
            # v3.0.35: per-key filter on /api/monitoring/activity (DevinGPT
            # + operator ask 2026-05-01). Composite (api_key_id, created_at)
            # covers the common "events for this key in last 1h" query.
            "CREATE INDEX IF NOT EXISTS ix_activity_log_api_key_created ON activity_log(api_key_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_activity_log_event_type ON activity_log(event_type)",
            # Provider metrics rollup queries are always (provider_id, bucket_ts)
            "CREATE INDEX IF NOT EXISTS ix_provider_metrics_provider_bucket ON provider_metrics(provider_id, bucket_ts DESC)",
            # api_keys.last_used_at — used by activity rollup + key-usage UI
            "CREATE INDEX IF NOT EXISTS ix_api_keys_last_used_at ON api_keys(last_used_at DESC)",
            # v3.0 R1 — Run runtime hot paths
            "CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status)",
            "CREATE INDEX IF NOT EXISTS ix_runs_owner_node ON runs(owner_node_id)",
            "CREATE INDEX IF NOT EXISTS ix_runs_deadline ON runs(deadline_ts)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_run_messages_seq ON run_messages(run_id, seq)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_run_events_seq ON run_events(run_id, seq)",
            "CREATE INDEX IF NOT EXISTS ix_run_events_ts ON run_events(run_id, ts)",
            "CREATE INDEX IF NOT EXISTS ix_run_idempotency_created_at ON run_idempotency(created_at)",
            # Hub team flag A: secondary index for the 24h-TTL prune sweep.
            # Composite PK is (api_key_id, idempotency_key); prune walks by
            # created_at across all api_keys, so an index on (created_at)
            # alone (above) is the cheap right shape. Adding the leading-key
            # variant here so a future "purge keys for tenant X" lookup is
            # also indexed without a scan.
            "CREATE INDEX IF NOT EXISTS ix_run_idempotency_key_created ON run_idempotency(idempotency_key, created_at)",
            # v5.0.0 — compliance audit indices.
            "CREATE INDEX IF NOT EXISTS ix_compliance_events_api_key_created ON compliance_events(api_key_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_compliance_events_event_type ON compliance_events(event_type)",
            "CREATE INDEX IF NOT EXISTS ix_compliance_policy_changes_at ON compliance_policy_changes(changed_at DESC)",
        ]:
            try:
                await conn.exec_driver_sql(index_stmt)
            except Exception as e:
                logger.warning(f"index create failed (likely missing column): {index_stmt[:60]}... — {e}")

        # v4.4.27 — BUG-079 permanent fix. De-dup `provider_ai_review`
        # (+ `api_key_ai_review`) and then enforce
        # `UNIQUE(provider_id, captured_at)` /
        # `UNIQUE(api_key_id, captured_at)` so the check-then-insert
        # race that created the BUG-079 duplicate can never write again.
        # The v4.4.24 `.limit(1)` guard in apply_sync stops the crash;
        # this stops the cause. Per-pass observation 2026-05-28: www2
        # accumulated 3 NEW dup groups in the day since the v4.4.24
        # cleanup — race is still live, confirming the need.
        #
        # De-dup heuristic: prefer rows with any non-NULL lifecycle
        # field (`applied_at`, `dismissed_at`, `reverted_at` — the row
        # carries operator action), break ties by highest id (newest).
        # Same shape as the manual fix script used during v4.4.24.
        #
        # Idempotent: the DELETE is a no-op once the table is clean,
        # the CREATE UNIQUE INDEX is IF NOT EXISTS. Safe to run on
        # every boot.
        for tbl, cols in (
            ("provider_ai_review", ("provider_id", "captured_at")),
            ("api_key_ai_review", ("api_key_id", "captured_at")),
        ):
            try:
                col_list = ", ".join(cols)
                # SQLite window-function ROW_NUMBER ranks by keeper
                # heuristic per duplicate group; delete everything except
                # the top-ranked row in each group.
                await conn.exec_driver_sql(f"""
                    DELETE FROM {tbl} WHERE id IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY {col_list}
                                ORDER BY
                                    CASE WHEN applied_at IS NOT NULL THEN 0 ELSE 1 END,
                                    CASE WHEN dismissed_at IS NOT NULL THEN 0 ELSE 1 END,
                                    CASE WHEN reverted_at IS NOT NULL THEN 0 ELSE 1 END,
                                    id DESC
                            ) AS rn FROM {tbl}
                        ) WHERE rn > 1
                    )
                """)
                idx_name = f"ux_{tbl}_{'_'.join(cols)}"
                await conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} "
                    f"ON {tbl}({col_list})"
                )
            except Exception as e:
                # If the table doesn't exist yet (fresh DB before
                # Base.metadata.create_all ran above), or another
                # benign condition, log + continue. The migration is
                # belt-and-braces over the .limit(1) guard.
                logger.warning(
                    f"v4.4.27 UNIQUE constraint setup failed for {tbl}: "
                    f"{type(e).__name__}: {e}"
                )

        # v5.0.0 — one-shot backfill of ``providers.owner_company`` from
        # ``provider_type`` for rows that pre-date the column. Decision 17
        # in docs/5.0-compliance-design.md — the auto-derivation hook in
        # ``app/api/providers.py`` covers fresh create/update; this catches
        # everything else exactly once. Gated by ``SystemSetting`` so
        # restarts after the first run are a no-op. Idempotent within a
        # single boot — only updates rows where owner_company IS NULL,
        # never overwrites operator-set values.
        try:
            from sqlalchemy import text as _text
            applied = await conn.exec_driver_sql(
                "SELECT value FROM system_settings WHERE key='owner_company_backfill_applied'"
            )
            row = applied.fetchone()
            if not row or (row[0] or "").lower() not in ("1", "true", "yes"):
                from app.compliance.company_map import (
                    provider_type_to_company as _ptc,
                )
                rs = await conn.exec_driver_sql(
                    "SELECT id, provider_type FROM providers WHERE owner_company IS NULL"
                )
                rows = rs.fetchall()
                n = 0
                for pid, ptype in rows:
                    derived = _ptc(ptype)
                    if derived:
                        await conn.exec_driver_sql(
                            "UPDATE providers SET owner_company = ? WHERE id = ?",
                            (derived, pid),
                        )
                        n += 1
                await conn.exec_driver_sql(
                    "INSERT OR REPLACE INTO system_settings(key, value, value_type, updated_at) "
                    "VALUES ('owner_company_backfill_applied', 'true', 'bool', strftime('%s','now'))"
                )
                logger.info(f"v5.0.0 backfill: derived owner_company for {n} providers (gated; one-shot)")
        except Exception as e:
            # Non-fatal — provider rows can be backfilled manually via PATCH.
            logger.warning(f"v5.0.0 owner_company backfill skipped: {type(e).__name__}: {e}")

    # v5.15.0 Phase 1 (#508) — seed provider_oauth_accounts from legacy
    # Provider rows. Idempotent: skips providers that already have any
    # child rows. Non-fatal on failure (Phase 1 doesn't gate dispatch on
    # this data anyway; the seed is prep for the v5.15.1 dispatch flip).
    try:
        from app.providers.oauth_account_seeder import seed_missing_accounts
        async with AsyncSessionLocal() as _seed_session:
            counts = await seed_missing_accounts(_seed_session)
            if counts.get("seeded", 0) > 0:
                logger.info(
                    f"v5.15.0 seed provider_oauth_accounts: {counts}"
                )
    except Exception as e:
        logger.warning(f"v5.15.0 oauth_account_seeder skipped: {type(e).__name__}: {e}")

    logger.info("Database initialized")


async def get_db() -> AsyncSession:
    """FastAPI dependency yielding an AsyncSession.

    v3.7.21: restore the ``async with`` pattern that v3.7.19's
    BUG-022 fix accidentally broke. The previous attempt replaced the
    context manager with manual ``try/finally`` + ``session.close()``
    to swallow ``OperationalError('no active connection')`` on
    request cancellation. That swallowed the visible error but bypassed
    SQLA's pool-return path — the GC then surfaced
    ``SAWarning: non-checked-in connection will be terminated`` for
    each leaked connection. Net worse: log lines per cancellation
    increased from 3-5 to 7-10.

    v5.21.12: wrap ``session.__aexit__`` in ``asyncio.shield`` so
    cleanup completes even when the request task is cancelled
    mid-close. Root cause of the returning ``/cluster/sync`` DB-pool
    leak: FastAPI dep-cleanup is LIFO, so when a route declared
    ``Depends(watch_for_disconnect)`` BEFORE ``Depends(get_db)``, the
    watcher was still polling during get_db's ``__aexit__``. On peer
    POST completion (200 → peer's httpx closes), ``is_disconnected()``
    flipped True and the watcher called ``main_task.cancel()``. The
    resulting CancelledError raised inside SQLAlchemy's
    ``AsyncSession.close()`` mid-await → the aiosqlite connection was
    never returned to the pool. Shield converts that cancel into a
    delayed cancel (raised AFTER cleanup finishes), so the pool slot
    is safe regardless of where the cancel came from.

    Manual __aenter__/__aexit__ instead of ``async with`` because
    ``async with`` doesn't compose with ``asyncio.shield`` at the
    cleanup call. The pattern preserves the same rollback + close
    semantics as ``__aexit__(None, None, None)``.

    Correct fix (still): keep the pool-return path. Wrap the
    ``async with`` in a try/except that ONLY swallows the documented
    post-cancellation ``no active connection`` error — every other
    exception still bubbles up. The shielded ``__aexit__`` has
    already run rollback/close by the time the exception reaches our
    handler, so the pool state is intact.
    """
    import asyncio as _asyncio
    from sqlalchemy.exc import OperationalError

    session = AsyncSessionLocal()
    try:
        await session.__aenter__()
        try:
            yield session
        finally:
            # asyncio.shield defers any pending cancel until cleanup
            # completes. If the caller was already cancelled before we
            # get here, the shield's Task-wrapping still finishes the
            # close call — pool slot returns cleanly — and THEN
            # re-raises the CancelledError.
            await _asyncio.shield(
                session.__aexit__(None, None, None)
            )
    except OperationalError as exc:
        # Post-cancellation: aiosqlite connection got closed before
        # SQLA finished its cleanup. The shielded __aexit__ has already
        # done what it can; the error is log-noise only.
        if "no active connection" in str(exc).lower():
            return
        raise
