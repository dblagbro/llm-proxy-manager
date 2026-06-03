"""Compliance enforcement audit tables (v5.0.0).

Three append-only tables that together form the audit trail for the
compliance subsystem (decision 10 + 16):

- ``ComplianceEvent`` — one row per substitution, refusal, cache/memory
  filter, path-not-allowed denial, or grandfathered-in-flight request.
  Cluster-replicated (append-only handler in ``app/cluster/sync_handlers.py``).
- ``CompliancePolicyChange`` — one row per system-or-key policy edit
  (operator-supplied ``reason`` mandatory). Records the cluster fan-out
  outcome (which peers acked, which were pending at quorum).
- ``ComplianceAuditChain`` — daily integrity hash; chains forward
  (sha256 of prior day's hash + sorted event content). Makes tampering
  with historical rows detectable.

Retention is governed by ``SystemSetting.compliance_audit_retention_days``
(default 2555 = 7 years). The daily worker also computes the chain hash
for the closed prior day.
"""
from sqlalchemy import (
    Column, String, Integer, Text, DateTime, ForeignKey, Index,
)
from sqlalchemy.sql import func

from app.models.db_base import Base


class ComplianceEvent(Base):
    """One row per compliance enforcement action — substitution, 451 refusal,
    503 no-substitute, cache filter, memory filter, path-not-allowed, or
    in-flight grandfather. Audit-grade: never UPDATE, never DELETE except
    via the retention sweeper at the configured day-count.

    Event types (decision 16):
      - ``model_substitution``     — model swapped at routing time
      - ``provider_substitution``  — provider swapped (same model on a
                                      compliant provider)
      - ``client_product_refusal`` — UA matched a banned client (451)
      - ``compliance_no_substitute`` — all candidates filtered (503)
      - ``client_product_warning`` — banned UA but key allows it (future)
      - ``cache_filtered``         — cache hit dropped due to source_company
      - ``memory_filtered``        — memory turn dropped due to source_company
      - ``path_not_allowed``       — allowed_paths middleware 403
      - ``policy_grandfathered_inflight`` — stream that started pre-flip
    """
    __tablename__ = "compliance_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String, nullable=False, unique=True, index=True)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False)
    event_type = Column(String, nullable=False)
    requested_at = Column(DateTime, nullable=False, server_default=func.now())
    requested_model = Column(String, nullable=True)
    served_model = Column(String, nullable=True)
    served_provider_id = Column(String, nullable=True)
    blocked_company = Column(String, nullable=True)
    reason_code = Column(String, nullable=False)
    client_user_agent = Column(String, nullable=True)  # truncated to 200 chars at write
    http_status = Column(Integer, nullable=False)
    matched_pattern = Column(String, nullable=True)
    client_identity = Column(Text, nullable=True)  # JSON-encoded dict of X-Coordinator-* headers
    policy_active_since = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


# Decision 16 — secondary indices for the admin query API and per-key reads.
Index(
    "ix_compliance_events_api_key_created",
    ComplianceEvent.api_key_id,
    ComplianceEvent.created_at.desc(),
)
Index(
    "ix_compliance_events_event_type",
    ComplianceEvent.event_type,
)


class CompliancePolicyChange(Base):
    """One row per operator-initiated policy edit (system-wide OR per-key).
    ``reason`` is mandatory (decision 6). ``cluster_sync_status`` records
    whether the quorum fan-out reached full ack or only quorum-with-N-pending.
    """
    __tablename__ = "compliance_policy_changes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_change_id = Column(String, nullable=False, unique=True, index=True)
    changed_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    changed_by_user_id = Column(String, nullable=True)
    scope = Column(String, nullable=False)  # 'system' | 'per_key'
    target_id = Column(String, nullable=True)  # api_key_id when scope=='per_key'
    before_state = Column(Text, nullable=False)  # JSON
    after_state = Column(Text, nullable=False)   # JSON
    reason = Column(Text, nullable=False)
    applied_to_peers = Column(Text, nullable=False)  # JSON list of {peer, acked_at}
    pending_peers = Column(Text, nullable=True)      # JSON list of {peer, reason}
    cluster_sync_status = Column(String, nullable=False)


class ComplianceAuditChain(Base):
    """Daily hash chain over ``compliance_events`` rows. Computed by a
    background worker shortly after midnight UTC. Tampering with a closed
    day's events breaks the chain at every subsequent day.

    Chain construction (decision 10):

      content_for_day = sorted by event.id of:
        f"{event.id}|{event.audit_id}|{event.api_key_id}|{event.event_type}|{event.http_status}"
      chain_hash = sha256(prior_day_chain_hash + content_for_day).hexdigest()

    First day has prior_day_chain_hash=NULL (effectively the empty string in
    the hash input).
    """
    __tablename__ = "compliance_audit_chain"
    id = Column(Integer, primary_key=True, autoincrement=True)
    day = Column(String, nullable=False, unique=True, index=True)  # YYYY-MM-DD
    row_count = Column(Integer, nullable=False)
    prior_day_chain_hash = Column(String, nullable=True)
    chain_hash = Column(String, nullable=False)
    computed_at = Column(DateTime, nullable=False, server_default=func.now())
