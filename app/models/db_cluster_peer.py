"""v5.0.18 — UI-configurable cluster peers.

Pre-v5.0.18 the cluster peer list was env-only (``CLUSTER_PEERS=id:url,id:url``)
and only changeable by editing docker-compose.yml + restarting. v5.0.18
adds a durable ``cluster_peers`` table that the Settings → Cluster page
can mutate via the admin API.

Migration semantic:
    - On first boot with the table empty AND ``CLUSTER_PEERS`` env set,
      bootstrap rows are written from the env (one-time seed).
    - After that the DB is authoritative; the env is consulted only as
      a safety net for empty-DB scenarios.

Replication:
    - The table is cluster-sync replicated (push payload + apply
      handler), so adding peer C on www1 propagates to www2 within the
      normal sync round and www2 immediately starts syncing to C.
    - Soft-delete via ``removed_at`` + LWW via ``last_user_edit_at``
      mirror the existing api_keys / providers tombstone pattern.

Security:
    - The cluster sync HMAC secret is still env-only
      (``CLUSTER_SYNC_SECRET``) — the peer LIST is mutable but the
      shared secret is not. So adding a wrong-URL peer can't merge
      two clusters; the HMAC mismatch surfaces as a 401 on the
      sync push.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, String

from app.models.db_base import Base


class ClusterPeer(Base):
    """A peer node this cluster pushes sync to. The local node never
    appears in this table — only the OTHERS.

    Columns:
        id              CLUSTER_NODE_ID of the remote node
                        (e.g. "llm-proxy2-www2"). Primary key.
        url             Base URL of the remote node
                        (e.g. "https://www2.voipguru.org/llm-proxy2").
                        ``/cluster/sync`` is appended at push time.
        name            Optional human-readable name (defaults to id).
        added_at        Row creation timestamp (UTC, naive).
        removed_at      Soft-delete timestamp. NULL while active;
                        set when the operator clicks "Remove peer".
                        Replicated as a tombstone for 7 days then
                        pruned by the standard sweeper.
        last_user_edit_at  Float Unix timestamp of the most recent
                        operator-driven edit. Drives the LWW gate in
                        cluster sync apply so the most-recent edit
                        across the cluster wins.
    """
    __tablename__ = "cluster_peers"

    id = Column(String(64), primary_key=True)
    url = Column(String(512), nullable=False)
    name = Column(String(128), nullable=True)
    added_at = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)
    last_user_edit_at = Column(Float, nullable=True)

    def __repr__(self):
        active = "active" if self.removed_at is None else f"removed@{self.removed_at}"
        return f"<ClusterPeer id={self.id!r} url={self.url!r} {active}>"
