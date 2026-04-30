"""
Cluster OAuth recovery — pull-from-peers on invalid_grant (v3.0.18).

Why this exists
---------------
Anthropic and OpenAI both rotate the refresh_token on every successful
refresh call. In a cluster, two nodes can independently attempt to
refresh the same provider's access_token within the 60s sync window:
whichever gets the upstream call in second sees the OLD refresh_token
(it just got rotated by the first node) and gets back an
``invalid_grant`` 400 — the loser's local row is now broken, and the
existing auth-failure CB shuts that provider out for 24h until an
admin manually re-pastes credentials.

Today's fix (reactive, low-cost)
--------------------------------
On ``invalid_grant`` from upstream, the loser node fans out a signed
``GET /cluster/oauth-pull/{provider_id}`` to each peer. Whoever has a
fresher (non-expired) ``oauth_expires_at`` sends back their current
api_key + refresh_token. The loser adopts the fresher state locally
and returns success — the original chat call retries seamlessly with
the rotated tokens. If no peer has fresher tokens (real revocation),
we raise as before and the auth-failure CB takes over.

Authoritative auth signature: same HMAC-of-(node_id) shared secret
that ``/cluster/settings`` uses.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.cluster.auth import sign_payload
from app.cluster.manager import _parse_peers
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PeerOAuthState:
    api_key: str
    oauth_refresh_token: Optional[str]
    oauth_expires_at: Optional[float]
    last_user_edit_at: Optional[float]
    updated_at_iso: Optional[str]
    extra_config: Optional[dict]
    source_peer_id: str


async def pull_oauth_state_from_peers(provider_id: str) -> Optional[PeerOAuthState]:
    """Fan out to each peer; return the freshest non-stale OAuth state, or None.

    "Freshest" = highest ``oauth_expires_at`` that is still in the future.
    Peers that 404 the provider, fail HMAC verification, are unreachable, or
    return tokens that have already expired are silently skipped.
    """
    if not settings.cluster_enabled:
        return None
    peers = _parse_peers()
    if not peers:
        return None

    node_id = (settings.cluster_node_id or "").encode()
    sig = sign_payload(node_id)
    headers = {
        "X-Cluster-Node": settings.cluster_node_id or "",
        "X-Cluster-Sig": sig,
    }

    candidates: list[PeerOAuthState] = []
    now = time.time()
    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
        for peer in peers:
            url = f"{peer.url.rstrip('/')}/cluster/oauth-pull/{provider_id}"
            try:
                r = await client.get(url, headers=headers)
            except Exception as e:
                logger.debug("oauth_recovery.peer_unreachable peer=%s err=%s", peer.id, e)
                continue
            if r.status_code != 200:
                logger.debug(
                    "oauth_recovery.peer_returned_non_200 peer=%s status=%d",
                    peer.id, r.status_code,
                )
                continue
            try:
                data = r.json()
            except Exception:
                continue
            api_key = data.get("api_key")
            if not isinstance(api_key, str) or not api_key:
                continue
            expires_at = data.get("oauth_expires_at")
            try:
                expires_at = float(expires_at) if expires_at is not None else None
            except (TypeError, ValueError):
                expires_at = None
            # Skip already-expired peer state (would be useless to adopt).
            if expires_at is not None and expires_at <= now:
                continue
            candidates.append(PeerOAuthState(
                api_key=api_key,
                oauth_refresh_token=data.get("oauth_refresh_token") or None,
                oauth_expires_at=expires_at,
                last_user_edit_at=data.get("last_user_edit_at"),
                updated_at_iso=data.get("updated_at"),
                extra_config=data.get("extra_config"),
                source_peer_id=peer.id,
            ))

    if not candidates:
        return None
    # Pick the candidate with the latest oauth_expires_at (fall back to
    # any one if none have a stamp — non-expiring tokens).
    candidates.sort(
        key=lambda c: c.oauth_expires_at if c.oauth_expires_at is not None else 0,
        reverse=True,
    )
    return candidates[0]


async def adopt_peer_state(provider, db, state: PeerOAuthState) -> None:
    """Persist a peer's OAuth state to the local Provider row in-place.

    Treats the adoption as a non-user-edit write — preserves the
    existing ``last_user_edit_at`` and only updates the OAuth fields +
    extra_config OAuth keys (chatgpt_account_id, chatgpt_plan_type).
    """
    provider.api_key = state.api_key
    if state.oauth_refresh_token:
        provider.oauth_refresh_token = state.oauth_refresh_token
    if state.oauth_expires_at is not None:
        provider.oauth_expires_at = state.oauth_expires_at
    # Carry over the OAuth-stashed extra_config keys if present.
    if isinstance(state.extra_config, dict):
        merged = dict(provider.extra_config or {})
        for k in ("chatgpt_account_id", "chatgpt_plan_type"):
            if k in state.extra_config and state.extra_config[k]:
                merged[k] = state.extra_config[k]
        provider.extra_config = merged
    await db.commit()
    logger.warning(
        "oauth_recovery.adopted_peer_state provider=%s peer=%s expires_at=%s",
        provider.id, state.source_peer_id, state.oauth_expires_at,
    )
