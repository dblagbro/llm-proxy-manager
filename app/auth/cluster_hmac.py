"""HMAC verification for cross-cluster admin reads (v4.3.5).

Coordinator-hub team request 2026-05-20: hub UI needs to surface
per-Anthropic-account weekly utilization from ``external_usage_snapshot``
without holding an admin session cookie. They already share a
``COORDINATOR_HMAC_KEY`` secret with their other peers; the proxy
accepts HMAC-signed admin reads using that same secret so no new
inter-service credential needs to be provisioned.

Auth contract (caller must compute matching HMAC):

    X-Cluster-Timestamp: <unix epoch seconds>
    X-Cluster-Auth:      <hex sha256 hmac>

    signed_bytes = f"{timestamp}{request.url.path}".encode() + request.body()
    expected     = hmac.new(secret_utf8, signed_bytes, sha256).hexdigest()

The proxy validates: (a) the timestamp is within ±60s of server time
(replay protection), (b) the HMAC matches via constant-time compare.

When ``COORDINATOR_HMAC_KEY`` is unset, the dependency returns 503 so
the operator notices the misconfiguration immediately instead of
silently allowing/denying.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import Header, HTTPException, Request


_TIMESTAMP_WINDOW_SEC = 60


def _secret() -> Optional[str]:
    s = os.environ.get("COORDINATOR_HMAC_KEY")
    return s if s else None


async def require_cluster_hmac(
    request: Request,
    x_cluster_timestamp: Optional[str] = Header(None),
    x_cluster_auth: Optional[str] = Header(None),
) -> bool:
    secret = _secret()
    if not secret:
        raise HTTPException(
            503,
            "Cluster HMAC auth not configured: COORDINATOR_HMAC_KEY env "
            "var is unset on this proxy node.",
        )
    if not x_cluster_timestamp or not x_cluster_auth:
        raise HTTPException(
            401, "Missing X-Cluster-Timestamp / X-Cluster-Auth headers"
        )
    try:
        ts = int(x_cluster_timestamp)
    except ValueError:
        raise HTTPException(
            401, "X-Cluster-Timestamp must be unix epoch seconds"
        )
    if abs(int(time.time()) - ts) > _TIMESTAMP_WINDOW_SEC:
        raise HTTPException(
            401,
            f"X-Cluster-Timestamp out of window (±{_TIMESTAMP_WINDOW_SEC}s)",
        )
    body = await request.body()
    signed = f"{ts}{request.url.path}".encode("utf-8") + (body or b"")
    expected = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, x_cluster_auth.strip().lower()):
        raise HTTPException(401, "Invalid X-Cluster-Auth signature")
    return True
