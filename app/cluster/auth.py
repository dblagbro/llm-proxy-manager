"""
Cluster HMAC authentication — signing and verification primitives.

Separate from peer lifecycle (manager.py) because the auth scheme evolves
independently: the signing algorithm or header names change for security
reasons; heartbeat frequency and sync behaviour change for operational reasons.

v5.22.15 — fail closed when no secret is configured
---------------------------------------------------
``cluster_sync_secret`` is ``Optional[str] = None``. The previous
implementation did ``key = (settings.cluster_sync_secret or "").encode()``,
so an unset secret did not disable authentication — it silently switched the
cluster to a **publicly known key**, the empty string. Anyone able to reach
``POST /cluster/sync`` could then compute a valid signature and have
``apply_sync`` write providers, api_keys, users and settings into the node.
The nginx allow-list was the only thing standing in the way, and a network
ACL is not an authentication scheme.

``app/integration/chat.py::verify_passphrase`` already had the right shape —
"refuse to authenticate when no passphrase configured" — so this brings
cluster auth in line with the convention the rest of the codebase follows.

The rule now: **no secret, no authentication.** Verification returns False,
and signing raises rather than emitting a signature that a mis-configured
peer would accept.
"""

import hashlib
import hmac
import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Anything shorter is brute-forceable offline: a captured request body plus
# its signature is all an attacker needs to grind candidates. Not enforced as
# a hard failure — that would strand a running cluster on a short secret at
# the worst possible moment — but it is reported loudly at startup.
MIN_RECOMMENDED_SECRET_LEN = 32


class ClusterAuthNotConfigured(RuntimeError):
    """Raised when a signature is requested but no cluster secret is set.

    Deliberately an exception rather than a silently-empty signature: a
    caller that reaches this has a configuration bug, and the failure must
    be visible instead of producing a token any empty-keyed peer accepts.
    """


def cluster_auth_configured() -> bool:
    """True when a usable cluster secret is present.

    Callers that can skip work entirely (the sync push worker, replication)
    should check this first so a mis-configured node logs one clear reason
    rather than a stream of exceptions.
    """
    return bool(settings.cluster_sync_secret)


def _key() -> bytes:
    secret = settings.cluster_sync_secret
    if not secret:
        raise ClusterAuthNotConfigured(
            "CLUSTER_SYNC_SECRET is not set — refusing to sign or verify "
            "cluster traffic. Set it to a random value of at least "
            f"{MIN_RECOMMENDED_SECRET_LEN} characters on every node."
        )
    return secret.encode()


def sign_payload(payload: bytes) -> str:
    """HMAC-sign arbitrary bytes with the cluster shared secret.

    Raises ``ClusterAuthNotConfigured`` when no secret is set.
    """
    return hmac.new(_key(), payload, hashlib.sha256).hexdigest()


def verify_payload(payload: bytes, signature: str) -> bool:
    """Verify an HMAC signature produced by sign_payload().

    Returns False — never raises — when no secret is configured, so an
    unauthenticated caller gets a plain 403 and a mis-configured node cannot
    be talked into accepting forged state.
    """
    if not signature:
        return False
    try:
        expected = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    except ClusterAuthNotConfigured:
        logger.error(
            "cluster_auth.rejected reason=no_secret — inbound cluster request "
            "refused because CLUSTER_SYNC_SECRET is unset on this node"
        )
        return False
    return hmac.compare_digest(expected, signature)


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


def audit_cluster_auth_config() -> list[str]:
    """Return human-readable problems with the cluster auth configuration.

    Called at startup so a node that cannot authenticate says so once, in
    the log, instead of failing one sync at a time.
    """
    problems: list[str] = []
    if not settings.cluster_enabled:
        return problems
    secret = settings.cluster_sync_secret
    if not secret:
        problems.append(
            "CLUSTER_ENABLED is true but CLUSTER_SYNC_SECRET is unset — all "
            "cluster traffic will be refused in both directions. Peers cannot "
            "sync until a shared secret is set on every node."
        )
    elif len(secret) < MIN_RECOMMENDED_SECRET_LEN:
        problems.append(
            f"CLUSTER_SYNC_SECRET is only {len(secret)} characters; "
            f"{MIN_RECOMMENDED_SECRET_LEN}+ is recommended. A short secret is "
            "brute-forceable offline from one captured request and signature."
        )
    return problems
