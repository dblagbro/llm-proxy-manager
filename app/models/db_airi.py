"""AIRI (AI Router Interface) rule-layer ORM models (v4.0).

Split out from ``db.py`` in v4.4.11. AIRI lets operators organise the
AI Provider Supervisor's policy as named, snapshot-able rule-sets.
Milestone 2 shipped the data model + rule-set save/restore; later
milestones wire rules to live supervisor behaviour and add the chat
conversation surface.

Owns:

- ``AiriRuleset`` — named snapshot; exactly one ``is_active``.
- ``AiriRule`` — individual policy item (threshold | conditional |
  monitor).
- ``AiriProposal`` — propose → dry-run → apply audit trail.
- ``AiriConversation`` — persistent AIRI chat thread.
- ``AiriMessage`` — one turn inside an ``AiriConversation``.
- ``AiriNotificationPref`` — per-user AIRI-notification subscription
  (v4.0.3).
"""
import secrets

from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Text, JSON, ForeignKey
)
from sqlalchemy.sql import func

from app.models.db_base import Base


class AiriRuleset(Base):
    """A named snapshot of AIRI rules. Exactly one row is ``is_active``.
    The seeded ``Default`` set mirrors the supervisor's current settings."""
    __tablename__ = "airi_ruleset"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    name = Column(String, nullable=False, unique=True)
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=False)
    description = Column(Text)
    created_by = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AiriRule(Base):
    """One policy item inside a rule-set.

    ``kind`` is ``threshold`` (a supervisor tunable), ``conditional`` (a
    deterministic trigger->action — authored in a later milestone), or
    ``monitor`` (a read-only recurring check — later milestone). ``spec`` is
    the kind-specific JSON body.
    """
    __tablename__ = "airi_rule"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    ruleset_id = Column(String, ForeignKey("airi_ruleset.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)              # threshold|conditional|monitor
    spec = Column(JSON, default=dict)
    mode = Column(String, default="suggest")           # suggest|auto_apply
    enabled = Column(Boolean, default=True)
    blast_radius_cap = Column(Integer)
    max_runs_per_window = Column(Integer)
    cooldown_sec = Column(Integer)
    expiry_at = Column(DateTime)
    last_run_at = Column(DateTime)
    last_action = Column(String)
    oscillation_state = Column(JSON)
    created_by = Column(String)
    created_via_prompt = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AiriProposal(Base):
    """A change AIRI proposed — the unit of the propose -> dry-run -> apply
    flow, and the audit record. ``prior_state`` snapshots what to restore on
    revert. v4.0 milestone 3."""
    __tablename__ = "airi_proposal"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    kind = Column(String, nullable=False)          # provider_change | rule_change
    target_id = Column(String, nullable=False)     # provider id or rule id
    target_label = Column(String)                  # provider / rule name for display
    change = Column(JSON, default=dict)            # {field, from, to, ...}
    dry_run = Column(JSON, default=dict)           # the impact preview
    status = Column(String, default="pending")     # pending|applied|rejected|reverted
    prior_state = Column(JSON)                     # snapshot for revert
    created_by = Column(String)
    created_via_prompt = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    decided_at = Column(DateTime)
    decided_by = Column(String)


class AiriConversation(Base):
    """A persistent AIRI chat thread. History is per-user, but every user can
    SEARCH every conversation (decision #5 — the shared history is the
    cross-operator change-coordination surface). v4.0 milestone 5."""
    __tablename__ = "airi_conversation"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    user_id = Column(String, nullable=False, index=True)   # owning operator
    title = Column(String)                                 # first user line, truncated
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AiriMessage(Base):
    """One turn inside an AIRI conversation. ``content`` is searchable across
    all users. ``tool_calls`` / ``trace_id`` are kept for forward-compat with
    a richer transcript; v4.0 stores plain user/assistant text. M5."""
    __tablename__ = "airi_message"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    conversation_id = Column(String, ForeignKey("airi_conversation.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    role = Column(String, nullable=False)                  # user|assistant
    content = Column(Text)
    tool_calls = Column(JSON)                              # forward-compat, unused in 4.0
    trace_id = Column(String)                              # forward-compat, unused in 4.0
    created_at = Column(DateTime, server_default=func.now())


class AiriNotificationPref(Base):
    """A single operator's personal AIRI-notification subscription (v4.0.3).

    The global alert mailbox (``settings.smtp_to``) always receives AIRI
    notifications — this row is an ADDITIVE per-user subscription: an
    operator opts their own address in and tunes which categories and what
    minimum severity reach them. One row per user; absence == no personal
    subscription."""
    __tablename__ = "airi_notification_pref"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    user_id = Column(String, nullable=False, unique=True, index=True)  # username
    email = Column(String)                                 # NULL == no personal email
    enabled = Column(Boolean, default=True)                # personal subscription on/off
    categories = Column(JSON, default=lambda: {"monitor": True, "automation": True})
    min_severity = Column(String, default="warning")       # info|warning|critical
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
