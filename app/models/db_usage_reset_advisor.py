"""v5.22.59 — persisted state for the usage-reset advisor.

One row per provider. ``advised_window`` is the "already advised for
weekly window X" marker that makes the check idempotent across the
in-process worker and the host cron script. Created by
``Base.metadata.create_all`` (idempotent); not cluster-synced.
"""

from sqlalchemy import Column, DateTime, Float, String, Text

from app.models.db_base import Base


class UsageResetAdvisorState(Base):
    __tablename__ = "usage_reset_advisor_state"

    provider_id = Column(String(64), primary_key=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_decision = Column(String(32), nullable=True)
    last_reason = Column(Text, nullable=True)
    last_weekly_pct = Column(Float, nullable=True)
    last_fable_pp = Column(Float, nullable=True)
    # Weekly window key (resets_at rounded to the hour, ISO) already advised.
    advised_window = Column(String(32), nullable=True)
    advised_at = Column(DateTime, nullable=True)
    # De-dupe key of the "cannot read usage" alert already sent; cleared on
    # the next successful decision so a later failure alerts again.
    unreadable_alerted_key = Column(String(128), nullable=True)
