"""Provider create/update request schemas (split out of providers.py, v5.22.63)."""

from typing import Optional

from pydantic import BaseModel, field_validator

from app.api._provider_type_policy import ProviderFieldGuards


class ProviderCreate(ProviderFieldGuards, BaseModel):
    name: str
    provider_type: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    default_model: Optional[str] = None
    priority: int = 10
    enabled: bool = True
    timeout_sec: int = 30
    exclude_from_tool_requests: bool = False
    # v3.9.5 (#267 Phase 8) — opt-out from the caller-memory system
    memory_disabled: bool = False
    hold_down_sec: Optional[int] = None       # None = use global setting
    failure_threshold: Optional[int] = None   # None = use global setting
    daily_budget_usd: Optional[float] = None  # None = unlimited
    extra_config: dict = {}
    # v3.0.45: tenant scoping. Null = shared across all keys (default).
    # Set to an api_keys.id to restrict this provider to a single key.
    owned_by_key_id: Optional[str] = None
    # v2.7.0: claude-oauth credential paste. The frontend sends either:
    #   - bare ``sk-ant-oat...`` access token, OR
    #   - the JSON contents of ``~/.claude/credentials.json``.
    # Server parses, extracts access/refresh/expires_at, and stores them in
    # the existing api_key column + new oauth_* columns. The raw blob is
    # never persisted.
    oauth_credentials_blob: Optional[str] = None
    # v3.0.64: per-provider usage-based rotation config (Phase 2). All
    # optional — providers default to "no tracking" until operator opts in.
    usage_tracking_enabled: Optional[bool] = None
    usage_session_window_sec: Optional[int] = None
    usage_weekly_reset_dow: Optional[int] = None
    usage_weekly_reset_hour: Optional[int] = None
    usage_session_limit_tokens: Optional[int] = None
    usage_weekly_limit_tokens: Optional[int] = None
    usage_rotation_threshold_pct: Optional[int] = None


class ProviderUpdate(ProviderCreate):
    # Row-relative gate (v5.22.44): neutralize inherited create-time type check;
    # update_provider() gates a CHANGE into a closed type instead.
    @field_validator("provider_type")
    @classmethod
    def _check_provider_type(cls, v):
        return v




# v4.4.14 — list_providers + rolling_stats + rolling_stats_by_node + provider_usage
# extracted to app/api/providers_stats.py to keep this file under the 1000-LOC
# ceiling. main.py mounts both routers at the same /api/providers prefix; no
# path collisions.
