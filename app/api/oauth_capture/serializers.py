"""Serializers + header filters + safe-text coercion used across the
``oauth_capture`` package. Pure functions, no I/O."""
from __future__ import annotations

from typing import Optional

from app.models.db import OAuthCaptureLog, OAuthCaptureProfile


# ── Header filtering ────────────────────────────────────────────────────────
# Hop-by-hop headers that must not be copied to the upstream.

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-length",
    "host",
})


def _filter_req_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _filter_resp_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _safe_text(raw: Optional[bytes]) -> Optional[str]:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        return "[binary:" + base64.b64encode(raw[:4096]).decode() + "]"


# ── Row → JSON-safe dict ────────────────────────────────────────────────────


def _serialize_profile(p: OAuthCaptureProfile, include_secret: bool = False) -> dict:
    out = {
        "name": p.name,
        "preset": p.preset,
        "upstream_urls": list(p.upstream_urls or []),
        "enabled": bool(p.enabled),
        "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "has_secret": bool(p.secret),
    }
    if include_secret:
        out["secret"] = p.secret
    return out


def _serialize_log_summary(r: OAuthCaptureLog) -> dict:
    return {
        "id": r.id,
        "profile_name": r.profile_name,
        "method": r.method,
        "path": r.path,
        "upstream_url": r.upstream_url,
        "resp_status": r.resp_status,
        "latency_ms": r.latency_ms,
        "error": r.error,
        "req_body_preview": (r.req_body or "")[:200],
        "resp_body_preview": (r.resp_body or "")[:200],
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _serialize_log_full(r: OAuthCaptureLog) -> dict:
    return {
        **_serialize_log_summary(r),
        "capture_session": r.capture_session,
        "req_headers": r.req_headers,
        "req_body": r.req_body,
        "req_query": r.req_query,
        "resp_headers": r.resp_headers,
        "resp_body": r.resp_body,
    }
