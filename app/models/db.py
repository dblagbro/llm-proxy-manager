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
    provider_type = Column(String, nullable=False)  # anthropic|openai|google|ollama|compatible|vertex|grok
    api_key = Column(String)
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
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

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
    key_type = Column(String, default="standard")  # standard|claude-code
    enabled = Column(Boolean, default=True)
    total_requests = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    spending_cap_usd = Column(Float, nullable=True)  # None = unlimited
    rate_limit_rpm = Column(Integer, nullable=True)   # None = unlimited
    semantic_cache_enabled = Column(Boolean, default=False)  # Wave 1 #3 opt-in
    last_used_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")  # admin|user
    created_at = Column(DateTime, server_default=func.now())


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
