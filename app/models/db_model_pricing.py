"""v5.20.4 — Model pricing catalog.

Populated by the daily ``model_cost_map_worker`` (LiteLLM upstream
JSON ingestion). Consumed by ``pricing.estimate_cost_split`` as the
first-priority lookup so day-zero-released models have accurate
pricing without waiting for a ``litellm`` package upgrade.

Split into its own module (rather than the mega-``db.py``) so the
schema is easy to find + avoids growing the import surface of
callers who only need pricing.
"""
from sqlalchemy import Column, String, Float, Integer, DateTime

from app.models.db_base import Base


class ModelPricingEntry(Base):
    """One row per model name known to the catalog.

    ``model_key`` uses LiteLLM's naming convention (e.g.
    ``anthropic/claude-sonnet-4-6``, ``gpt-4o-mini``). Callers match
    against ``litellm_model`` before the / or after the / depending on
    the provider — see ``pricing.estimate_cost_split`` for the lookup
    ordering.

    ``source`` distinguishes ``litellm_upstream`` rows (auto-managed
    by the sync worker) from ``manual_override`` rows (operator-edited
    via admin API or SQL). The sync worker does NOT touch
    manual_override rows on upsert — see the worker's ``_fetch_and_upsert``
    for the guard.
    """

    __tablename__ = "model_pricing_catalog"

    model_key = Column(String(256), primary_key=True)
    input_cost_per_token = Column(Float, default=0.0)
    output_cost_per_token = Column(Float, default=0.0)
    max_input_tokens = Column(Integer, nullable=True)
    max_output_tokens = Column(Integer, nullable=True)
    provider_family = Column(String(64), nullable=True)
    source = Column(String(32), default="litellm_upstream")
    synced_at = Column(DateTime, nullable=True)
