"""Per-model ORM models (provider-scoped).

v5.22.36 — split out of ``db_provider.py``, which had grown to 561 LOC
against the 500-LOC domain ceiling that
``test_v4411_db_split.test_no_domain_module_exceeds_500_loc`` enforces.

These three tables are all keyed by ``(provider_id, model_id)``: they record
what we have learned about *a provider's models* rather than about the
provider itself, which is why they were the seam rather than, say, the auth
or metrics tables.

- ``ModelCapability`` — per-(provider, model) routing capability rows.
- ``ModelToolProbe`` — tool-call probe results (v3.8.4 / #264).
- ``ModelAlias`` — client-facing alias → provider/model mapping.

``ModelCapability.provider`` still back-populates ``Provider.capabilities``.
SQLAlchemy resolves ``relationship("Provider")`` through the shared registry
on ``Base``, so the mapping works across modules as long as both are
imported — which ``app/models/db.py`` guarantees.

Sits alongside ``db_model_pricing.py`` (the LiteLLM cost-map catalog).
"""
from sqlalchemy import (
    Column, String, Integer, Boolean, Float, DateTime, Text, JSON, ForeignKey
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.db_base import Base


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
