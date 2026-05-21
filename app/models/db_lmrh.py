"""LMRH protocol ORM models.

Split out from ``db.py`` in v4.4.11. Owns:

- ``LmrhDim`` — registered LMRH dimensions (auto-register path).
- ``LmrhProposal`` — operator-reviewed dim proposals.
"""
from sqlalchemy import Column, String, Integer, Float, Text, JSON

from app.models.db_base import Base


class LmrhDim(Base):
    """v3.0.25: registered LMRH dimension. The protocol's self-extension
    mechanism — apps can register new dims via POST /lmrh/register; the
    proxy collision-resolves (suffix -2/-3 on conflict) and replicates
    the registry to peers via cluster sync. Once registered, both sides
    agree on the canonical name and the proxy stops emitting unknown-dim
    warnings for it.

    Built-in dims (task, cost, latency, safety-min, etc.) are NOT in this
    table — they live in code. This table is for dims registered AT RUNTIME
    by integrating apps. Read of merged-set goes through ``known_dim_names()``
    which combines both.
    """
    __tablename__ = "lmrh_dims"

    name = Column(String, primary_key=True)
    owner_app = Column(String, nullable=True)         # free-form ("paperless-ai-analyzer")
    owner_key_id = Column(String, nullable=True)      # api_keys.id of submitter
    semantics = Column(Text, nullable=True)           # one-paragraph description
    value_type = Column(String, nullable=True)        # "string|int|enum:a,b,c|float"
    kind = Column(String, default="advisory")        # hard|soft|advisory
    examples = Column(JSON, default=list)             # ["task=foo;exclude=bar"]
    requested_name = Column(String, nullable=True)    # what was originally requested
    registered_at = Column(Float, nullable=False)
    registered_by_node = Column(String, nullable=True)
    # v3.0.29: tombstone for cluster-replicated soft delete. Without this,
    # hard-DELETE on one node was reversed by the next sync push from a peer
    # that still had the row. Mirrors the same pattern used for Provider
    # (v2.8.2) and ApiKey (v3.0.20). Stores Unix-epoch float for parity
    # with registered_at.
    deleted_at = Column(Float, nullable=True)


class LmrhProposal(Base):
    """v3.0.25: free-form proposals for dims that the submitter wants
    OPERATOR-REVIEWED before official adoption (vs the auto-register
    path). Distinct from the registry — proposals are read-only-by-admins
    until promoted to a registry entry.
    """
    __tablename__ = "lmrh_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposed_name = Column(String, nullable=False)
    rationale = Column(Text, nullable=True)
    proposer_app = Column(String, nullable=True)
    proposer_key_id = Column(String, nullable=True)
    proposed_at = Column(Float, nullable=False)
    status = Column(String, default="pending")        # pending|accepted|rejected
    review_note = Column(Text, nullable=True)
    # v3.0.29: tombstone for cluster-replicated soft delete (see LmrhDim).
    deleted_at = Column(Float, nullable=True)
