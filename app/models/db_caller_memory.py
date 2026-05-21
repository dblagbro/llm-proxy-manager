"""Caller memory ORM models (v3.8.7 / #267 Phase 2).

Split out from ``db.py`` in v4.4.11. Owns:

- ``CallerMemory`` — durable per-(api_key, conversation, tag) state.
- ``CallerMemoryMarker`` — existence marker for back-pressure recovery.
"""
from sqlalchemy import Column, String, Integer, Float, Text, ForeignKey

from app.models.db_base import Base


class CallerMemory(Base):
    """v3.8.7 (#267) Phase 2 — proxy-side caller memory store.

    Persistent memory state scoped per (api_key_id, conversation_id,
    memory_tag). King-store: the proxy's source of truth for memory
    content, regardless of which upstream provider served the most
    recent request. Cluster-replicated via the existing LWW pattern.

    Cached in Redis (`llmproxy:mem:{api_key_id}:{conv}:{tag}`) for
    hot read perf; this SQLite row is the durable copy.

    See ``docs/rfc/2026-05-proxy-memory-store.md`` for full design.
    Default OFF (operator opt-in via ``caller_memory_enabled``).
    """
    __tablename__ = "caller_memory"
    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    memory_tag = Column(String, nullable=False, default="default")
    content = Column(Text, nullable=False, default="")
    content_format = Column(String, default="text")   # "text" | "json" | "anthropic_memory_blocks"
    updated_at = Column(Float, nullable=False)        # unix ts for LWW
    updated_by_node = Column(String, nullable=True)
    # Optional provenance for back-pressure recovery + flush logic
    source_provider_id = Column(String, nullable=True)
    source_request_id = Column(String, nullable=True)
    # Soft-delete tombstone for cluster-sync propagation (mirrors
    # the v3.7.15 BlockedIp.deleted_at pattern)
    deleted_at = Column(Float, nullable=True)


class CallerMemoryMarker(Base):
    """v3.8.7 (#267) Phase 2 — persistent existence-marker for memory
    back-pressure recovery.

    Lives separately from CallerMemory so a DB restore that loses
    content rows can still recover from the upstream provider's
    surviving state. Lossy upgrades that ALSO lose markers require
    operator-driven re-import from snapshot.

    On a request where the marker exists but ``caller_memory.content``
    is empty/missing, the proxy triggers a vendor-specific recovery:
    Anthropic memory-tool ``view``, OpenAI Assistants
    ``GET /threads/{id}/messages``, etc. Reconstructed content
    populates CallerMemory + sets ``recovered_at`` on the marker.
    """
    __tablename__ = "caller_memory_marker"
    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    memory_tag = Column(String, nullable=False, default="default")
    first_seen_at = Column(Float, nullable=False)
    last_known_provider_id = Column(String, nullable=True)
    last_known_external_ref = Column(String, nullable=True)  # provider's thread_id / conversation handle
    recovered_at = Column(Float, nullable=True)
    deleted_at = Column(Float, nullable=True)
