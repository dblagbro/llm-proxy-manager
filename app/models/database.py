from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import event
from app.config import settings
from app.models.db import Base
# v3.2.11: auto-bump Provider.last_user_edit_at on user-meaningful column
# changes. Side-effect import — registers a SQLAlchemy event listener.
# See _user_edit_stamp.py for design rationale.
from app.models import _user_edit_stamp  # noqa: F401
import logging

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

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db():
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
            # v3.0.20 — ApiKey soft-delete tombstone for cluster-sync resurrection bug
            "ALTER TABLE api_keys ADD COLUMN deleted_at DATETIME",
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
        ]:
            try:
                await conn.exec_driver_sql(index_stmt)
            except Exception as e:
                logger.warning(f"index create failed (likely missing column): {index_stmt[:60]}... — {e}")
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

    Correct fix: keep the ``async with`` so the pool gets the
    connection back cleanly. Wrap the ``async with`` in a try/except
    that ONLY swallows the documented post-cancellation
    ``no active connection`` error — every other exception still
    bubbles up. The ``async with __aexit__`` has already run
    rollback/close by the time the exception reaches our handler,
    so the pool state is intact.
    """
    from sqlalchemy.exc import OperationalError
    try:
        async with AsyncSessionLocal() as session:
            yield session
    except OperationalError as exc:
        # Post-cancellation: aiosqlite connection got closed before
        # SQLA finished its cleanup. The async with has already done
        # what it can; the error is log-noise only.
        if "no active connection" in str(exc).lower():
            return
        raise
