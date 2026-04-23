"""
Cluster HMAC authentication — signing and verification primitives.

Separate from peer lifecycle (manager.py) because the auth scheme evolves
independently: the signing algorithm or header names change for security
reasons; heartbeat frequency and sync behaviour change for operational reasons.
"""
import hashlib
import hmac
import json

from app.config import settings


def sign_payload(payload: bytes) -> str:
    """HMAC-sign arbitrary bytes with the cluster shared secret."""
    key = (settings.cluster_sync_secret or "").encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_payload(payload: bytes, signature: str) -> bool:
    """Verify an HMAC signature produced by sign_payload()."""
    return hmac.compare_digest(sign_payload(payload), signature)


def verify_cluster_request(body: bytes, signature: str) -> bool:
    """Convenience wrapper — verifies the /cluster/sync request body."""
    return verify_payload(body, signature)


def auth_headers_for(payload: dict) -> dict:
    """Build HMAC-signed headers for an outgoing cluster request."""
    body = json.dumps(payload, sort_keys=True).encode()
    return {
        "X-Cluster-Node": settings.cluster_node_id or "",
        "X-Cluster-Sig": sign_payload(body),
        "Content-Type": "application/json",
    }
