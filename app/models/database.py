from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import event
from app.config import settings
from app.models.db import Base
import logging

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
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
    async with AsyncSessionLocal() as session:
        yield session
