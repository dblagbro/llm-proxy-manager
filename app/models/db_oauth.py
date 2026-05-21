"""OAuth capture ORM models.

Split out from ``db.py`` in v4.4.11. Owns:

- ``OAuthCaptureProfile`` — named OAuth capture configurations.
- ``OAuthCaptureLog`` — recorded req/resp pairs from the
  ``/api/oauth-capture/`` passthrough endpoint.
"""
from sqlalchemy import Column, String, Integer, Boolean, Float, DateTime, Text, JSON
from sqlalchemy.sql import func

from app.models.db_base import Base


class OAuthCaptureProfile(Base):
    """A named OAuth capture configuration. Each profile has its own upstream
    host(s), secret, and enabled flag so multiple CLIs (claude-code, codex,
    gh copilot, …) can be captured concurrently without interference.

    Added in v2.5.0 — replaces the former single-upstream settings model.
    """
    __tablename__ = "oauth_capture_profiles"

    name = Column(String, primary_key=True)  # "claude-code", "codex", "gh-copilot", etc.
    preset = Column(String, nullable=True)   # matches PRESETS key in oauth_capture.py
    upstream_urls = Column(JSON, default=list)  # list[str], typically 1-2 hosts
    secret = Column(String, nullable=True)   # per-profile capture secret
    enabled = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    # v3.0.97: tombstone for cluster-replicated soft delete.
    deleted_at = Column(DateTime, nullable=True, index=True)


class OAuthCaptureLog(Base):
    """Recorded request+response pairs from the OAuth-passthrough endpoint.
    Used to reverse-engineer vendor OAuth flows (claude-code, codex, gh copilot,
    etc.) before implementing a direct `*-oauth` provider.
    """
    __tablename__ = "oauth_capture_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_name = Column(String, nullable=True, index=True)  # v2.5.0: which capture profile
    capture_session = Column(String, nullable=True, index=True)  # optional client-tag
    method = Column(String, nullable=False)
    path = Column(String, nullable=False)          # the subpath of /api/oauth-capture/<profile>/
    upstream_url = Column(String, nullable=False)  # where we actually sent it
    req_headers = Column(JSON, default=dict)
    req_body = Column(Text, nullable=True)         # raw body; may be JSON or form-urlencoded
    req_query = Column(String, nullable=True)
    resp_status = Column(Integer, nullable=True)
    resp_headers = Column(JSON, default=dict)
    resp_body = Column(Text, nullable=True)
    latency_ms = Column(Float, default=0.0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
