"""Activity log + blocked IPs ORM models.

Split out from ``db.py`` in v4.4.11. Owns:

- ``BlockedIp`` — v3.7.11 IP block list.
- ``ActivityLog`` — request/event log.
"""
from sqlalchemy import Column, String, Integer, DateTime, Text, JSON
from sqlalchemy.sql import func

from app.models.db_base import Base


class BlockedIp(Base):
    """v3.7.11 — IP block list. Per operator Q5: AI rate limiter
    should be able to slow keys OR source IPs. v3.7.10 shipped the
    key-level controls; v3.7.11 adds the IP-level layer.

    Middleware (``app/middleware/ip_block.py``) reads this table into
    an in-memory set (refreshed every 30s) and returns 403 early for
    any matching client_ip OR client_ip_inside. Operator manages via
    admin endpoints; auto-population by the AI rate limiter is a
    deferred follow-up (v3.7.12+).
    """
    __tablename__ = "blocked_ips"
    ip = Column(String, primary_key=True)
    reason = Column(String, nullable=True)
    added_at = Column(DateTime, server_default=func.now())
    added_by = Column(String, nullable=True)
    # v3.7.15 — soft-delete tombstone for cluster-sync propagation. The
    # admin-DELETE endpoint sets ``deleted_at`` rather than hard-deleting
    # so peer nodes can learn about the removal on next sync push. The
    # middleware filters ``deleted_at IS NULL`` so tombstoned rows
    # don't block traffic. A periodic janitor (or manual op) can hard-
    # delete tombstoned rows older than N days.
    deleted_at = Column(DateTime, nullable=True, index=True)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, nullable=False)
    severity = Column(String, default="info")  # info|warning|error|critical
    message = Column(Text)
    provider_id = Column(String)
    api_key_id = Column(String)
    event_meta = Column(JSON, default=dict)
    created_at = Column(DateTime, server_default=func.now())
