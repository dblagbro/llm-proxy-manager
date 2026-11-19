"""v5.22.67 — cluster-wide circuit-breaker reset.

Breaker state is in-memory and per node. Before this, a passing provider
Test (card button or the Needs-attention "re-test now") cleared the
breaker only on the node that served the click; the other node kept the
provider out of rotation until its own hold-down (up to 6 h for a billing
hold) expired.

``reset_everywhere(provider_id)`` clears this node, then pushes an
HMAC-signed reset to every reachable peer's ``/cluster/circuit-breaker-reset``.
The peer handler (``reset_local``) does NOT re-fan-out, so there is no loop.
The fan-out is best-effort: a peer that is down simply recovers on its own
hold-down, exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from app.cluster.auth import cluster_auth_configured, sign_payload
from app.config import settings

logger = logging.getLogger(__name__)

PEER_PATH = "/cluster/circuit-breaker-reset"
_PEER_TIMEOUT_SEC = 3.0
_MAX_ID_LEN = 128


def valid_provider_id(provider_id: object) -> bool:
    return isinstance(provider_id, str) and 0 < len(provider_id) <= _MAX_ID_LEN


async def reset_local(provider_id: str) -> None:
    """Clear auth-failure + billing marks and close this node's breaker."""
    from app.monitoring.keepalive import clear_chronic_gate
    from app.routing.circuit_breaker import clear_auth_failure, force_close

    clear_auth_failure(provider_id)
    await force_close(provider_id)
    clear_chronic_gate(provider_id)  # v5.22.68: probes resume at once


async def _post_peer(peer, body: bytes, sig: str) -> dict:
    url = f"{peer.url.rstrip('/')}{PEER_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_PEER_TIMEOUT_SEC, verify=False) as client:
            resp = await client.post(
                url,
                content=body,
                headers={
                    "X-Cluster-Node": settings.cluster_node_id or "",
                    "X-Cluster-Sig": sig,
                    "Content-Type": "application/json",
                },
            )
        status = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception as exc:  # peer down / timeout: it recovers on its own
        status = "unreachable"
        logger.info("breaker_reset.peer_failed peer=%s err=%s", peer.id, str(exc)[:120])
    return {"peer_id": peer.id, "status": status}


async def fan_out(provider_id: str) -> list[dict]:
    """Push the reset to every peer not marked unreachable."""
    from app.cluster.manager import peers

    targets = [p for p in list(peers.values()) if p.status != "unreachable"]
    if not targets or not settings.cluster_enabled:
        return []
    if not cluster_auth_configured():
        logger.error("breaker_reset.fanout_skipped reason=no_secret")
        return []
    body = json.dumps({"provider_id": provider_id}, sort_keys=True).encode()
    sig = sign_payload(body)
    return list(await asyncio.gather(*(_post_peer(p, body, sig) for p in targets)))


async def reset_everywhere(provider_id: str) -> list[dict]:
    """Reset this node, then best-effort reset every peer. Never raises."""
    await reset_local(provider_id)
    try:
        return await fan_out(provider_id)
    except Exception as exc:
        logger.warning("breaker_reset.fanout_error err=%s", str(exc)[:200])
        return []
