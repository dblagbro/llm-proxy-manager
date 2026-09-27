"""ORM model registry — re-exports every model from its domain module.

Refactor history: in v4.4.11 (2026-05-20) this file was split into 10
``db_*.py`` modules along natural domain boundaries (`db_base`,
`db_provider`, `db_apikey`, `db_user`, `db_activity`, `db_run`,
`db_lmrh`, `db_oauth`, `db_caller_memory`, `db_airi`). Pre-split the
file was 994 LOC — one new ORM table away from the project's de-facto
1,000-LOC ceiling.

This file is now a backwards-compat shim: it imports every model so
the SQLAlchemy registry on ``Base.metadata`` is populated (the
``create_all`` call in ``models/database.py`` only sees tables whose
classes have been imported), and re-exports them all so existing
callers using ``from app.models.db import Provider, ApiKey, ...``
keep working unchanged.

When adding a new ORM table, put it in the appropriate ``db_*.py``
module (or create a new one for a new domain). Then add an import +
re-export here so:
  1. The table is registered on ``Base.metadata`` at app boot.
  2. Existing ``from app.models.db import X`` imports keep working.
"""
# Re-export Base + auth Session
from app.models.db_base import Base, Session

# Provider domain — heaviest module
from app.models.db_provider import (
    Provider,
    ProviderOAuthAccount,
    ProviderUsageWindow,
    ProviderNodeAuthState,
    ExternalUsageSnapshot,
    ProviderAiReview,
    ProviderMetric,
)

# Per-model tables (v5.22.36 — split out of db_provider.py for the
# 500-LOC domain ceiling; keyed by (provider_id, model_id)).
from app.models.db_model import (
    ModelCapability,
    ModelToolProbe,
    ModelAlias,
)

# API key domain
from app.models.db_apikey import (
    ApiKey,
    ApiKeyAiReview,
)

# User + system settings
from app.models.db_user import (
    User,
    SystemSetting,
    PasswordResetToken,
)

# Activity log + blocked IPs
from app.models.db_activity import (
    BlockedIp,
    ActivityLog,
)

# v5.7.0 — MCP tool-call audit
from app.models.db_mcp import (
    McpToolCall,
    CallerCapabilityScore,  # v5.10.0
)

# Run runtime (v3.0)
from app.models.db_run import (
    Run,
    RunMessage,
    RunEvent,
    RunIdempotency,
)

# LMRH protocol
from app.models.db_lmrh import (
    LmrhDim,
    LmrhProposal,
)

# OAuth capture
from app.models.db_oauth import (
    OAuthCaptureProfile,
    OAuthCaptureLog,
)

# Caller memory (#267 Phase 2)
from app.models.db_caller_memory import (
    CallerMemory,
    CallerMemoryMarker,
)

# AIRI rule layer (v4.0)
from app.models.db_airi import (
    AiriRuleset,
    AiriRule,
    AiriProposal,
    AiriConversation,
    AiriMessage,
    AiriNotificationPref,
)

# Compliance audit (v5.0.0)
from app.models.db_compliance import (
    ComplianceEvent,
    CompliancePolicyChange,
    ComplianceAuditChain,
)

# Cluster peers (v5.0.18 — UI-configurable peer list)
from app.models.db_cluster_peer import (
    ClusterPeer,
)

# Model pricing catalog (v5.20.6 — LiteLLM `main` cost map, synced daily).
# v5.22.33: this module was NOT aggregated here, so `model_pricing_catalog`
# was missing from `Base.metadata` as seen through `app.models.db` — which is
# exactly what `alembic/env.py` uses for `target_metadata`. The table has no
# migration (it is created by `create_all` at runtime), so the next
# `alembic revision --autogenerate` would have emitted a `drop_table` for it.
from app.models.db_model_pricing import (
    ModelPricingEntry,
)

__all__ = [
    "Base",
    "Session",
    # Provider domain
    "Provider",
    "ProviderUsageWindow",
    "ProviderNodeAuthState",
    "ProviderOAuthAccount",
    "ExternalUsageSnapshot",
    "ModelCapability",
    "ProviderAiReview",
    "ModelToolProbe",
    "ProviderMetric",
    "ModelAlias",
    # API key domain
    "ApiKey",
    "ApiKeyAiReview",
    # User + settings
    "User",
    "PasswordResetToken",
    "SystemSetting",
    # Activity / IP block
    "BlockedIp",
    "ActivityLog",
    # v5.7.0 — MCP tool-call audit
    "McpToolCall",
    # v5.10.0 — capability back-pressure
    "CallerCapabilityScore",
    # Run runtime
    "Run",
    "RunMessage",
    "RunEvent",
    "RunIdempotency",
    # LMRH
    "LmrhDim",
    "LmrhProposal",
    # OAuth capture
    "OAuthCaptureProfile",
    "OAuthCaptureLog",
    # Caller memory
    "CallerMemory",
    "CallerMemoryMarker",
    # AIRI
    "AiriRuleset",
    "AiriRule",
    "AiriProposal",
    "AiriConversation",
    "AiriMessage",
    "AiriNotificationPref",
    # Compliance (v5.0.0)
    "ComplianceEvent",
    "CompliancePolicyChange",
    "ComplianceAuditChain",
    # Cluster peers (v5.0.18)
    "ClusterPeer",
    # Model pricing catalog (v5.20.6)
    "ModelPricingEntry",
]
