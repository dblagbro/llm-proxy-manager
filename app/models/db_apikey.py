"""API key domain ORM models.

Split out from ``db.py`` in v4.4.11. Owns:

- ``ApiKey`` — the main API key row (auth surface for /v1/* calls).
- ``ApiKeyAiReview`` — the v3.7.10 proactive rate-limiter verdicts.
"""
import secrets

from sqlalchemy import (
    Column, String, Integer, Boolean, Float, DateTime, Text, JSON, ForeignKey
)
from sqlalchemy.sql import func

from app.models.db_base import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    name = Column(String, nullable=False)
    key_hash = Column(String, nullable=False, unique=True)
    key_prefix = Column(String, nullable=False)  # first 8 chars for display
    encrypted_key = Column(String, nullable=True)  # Fernet-encrypted full key; NULL for legacy pre-encryption keys
    key_type = Column(String, default="standard")  # standard|claude-code|admin|admin-readonly-catalog
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
    # v3.9.13 (#267 follow-up) — per-key caller_memory retention. Operator
    # sets days; background sweeper tombstones CallerMemory rows whose
    # ``updated_at < now - caller_memory_ttl_days * 86400``. Null = no TTL
    # (rows persist until purged via /v1/memory or admin endpoint).
    # Operator opt-in per key — different teams have different retention
    # needs (hub wants room-archival-driven cleanup; tax wants year-long
    # carryover; paperless wants per-document cycle). Default behavior
    # unchanged for keys that don't set this.
    caller_memory_ttl_days = Column(Integer, nullable=True)
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


class ApiKeyAiReview(Base):
    """v3.7.10 — operator-requested proactive rate limiter. A background
    worker scans recent activity for each api_key every 5 min, computes
    a stats summary, sends it to an LLM for classification, and writes
    a row here. Operator reviews via admin endpoints (suggest-only by
    default; ``ai_rate_limiter_auto_apply=True`` flips to auto-action).

    Verdict enum:
      - ``normal``     — healthy traffic pattern; no action
      - ``watch``      — slightly elevated; record but don't act
      - ``throttle``   — recommend lowering ``rate_limit_rpm`` to floor
      - ``block``      — recommend disabling the key entirely

    Suggested-action enum:
      - ``none``           — verdict is normal/watch; just log
      - ``throttle_rpm``   — lower rate_limit_rpm to throttle_floor
      - ``disable``        — set enabled=False (requires operator unblock)

    When ``applied_at`` is set, the suggestion was applied (manually or
    auto). ``prior_rate_limit_rpm`` lets us revert to the operator-set
    value when the throttle is lifted.
    """
    __tablename__ = "api_key_ai_review"
    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False, index=True)
    captured_at = Column(DateTime, server_default=func.now(), index=True)
    llm_model = Column(String, nullable=True)  # which model was called for classification
    llm_verdict = Column(String, nullable=False)
    llm_reasoning = Column(Text, nullable=True)
    suggested_action = Column(String, nullable=False, default="none")
    stats_summary = Column(JSON, nullable=True)  # dict of input stats — for diagnostics
    # Lifecycle: applied / dismissed / reverted
    applied_at = Column(DateTime, nullable=True)
    applied_action = Column(String, nullable=True)
    prior_rate_limit_rpm = Column(Integer, nullable=True)  # for revert
    reverted_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    # v3.7.12 — when verdict == "block_ip", the LLM names the
    # specific IP it thinks should be blocked. Stored here so the
    # ``apply`` endpoint knows which IP to insert into
    # ``blocked_ips`` and ``revert`` knows which one to remove.
    suggested_block_ip = Column(String, nullable=True)
