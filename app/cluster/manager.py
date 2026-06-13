"""
Cluster manager — multi-node coordination.

Each node runs an identical stateless service; Redis (when available) holds
shared circuit-breaker and rate-limit state automatically. This module handles:
  - Peer heartbeat (every 30s)
  - Config sync: users + API keys pushed/pulled via HMAC-signed requests
  - Cluster health endpoint
  - Node registration on startup
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.models.db import User, ApiKey, Provider, SystemSetting
from app.cluster.auth import sign_payload, verify_payload, verify_cluster_request, auth_headers_for
from app.cluster.sync import apply_sync, get_peer_total_cost

logger = logging.getLogger(__name__)


@dataclass
class PeerNode:
    id: str
    name: str
    url: str
    priority: int = 10
    status: str = "unknown"       # healthy|degraded|unreachable
    last_heartbeat: float = 0.0
    latency_ms: float = 0.0
    healthy_providers: int = 0
    total_providers: int = 0
    # v5.1.0 / Batch A2 — peer's reported __version__ from /health.
    # None on first contact or when peer is unreachable; populated
    # opportunistically by the heartbeat loop. Used by the Settings →
    # Cluster UI to surface version skew at a glance.
    version: Optional[str] = None


peers: dict[str, PeerNode] = {}
_heartbeat_task: Optional[asyncio.Task] = None
_sync_task: Optional[asyncio.Task] = None

# Private alias for internal use within this module
_peers = peers


def active_node_count() -> int:
    """Number of nodes currently reachable, including self."""
    return 1 + sum(1 for p in _peers.values() if p.status != "unreachable")


def _parse_peers() -> list[PeerNode]:
    """Parse the env-derived peer list. Used as the seed source on first
    boot AND as a safety-net fallback when the DB is empty/unreachable.

    v5.0.18: env was the only source pre-v5.0.18. Now the cluster_peers
    table is authoritative once seeded. See ``_reload_peers_from_db``.
    """
    raw = settings.cluster_peers or ""
    nodes = []
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        node_id, _, url = item.partition(":")
        nodes.append(PeerNode(id=node_id.strip(), name=node_id.strip(), url=url.strip()))
    return nodes


# v5.0.25 / Batch 4 (BUG-061) — serialize the in-memory _peers swap.
# Multiple coroutines can call _reload_peers_from_db (lifespan startup,
# 30s refresh loop, admin restore endpoint…); without a lock the
# heartbeat / sync push loops can read partial state during a swap.
_peers_lock = asyncio.Lock()


async def _reload_peers_from_db(db_factory) -> int:
    """v5.0.18 — Sync the in-memory ``_peers`` dict with the active rows
    in the ``cluster_peers`` table. Called at startup and on a 30s
    refresh loop. Returns the count of active peers after reload.

    Adds new peers (preserving their ``status`` if already known),
    removes tombstoned peers, and updates URLs in place.

    v5.0.25 / Batch 4 (BUG-061) — wraps the in-memory swap in
    ``_peers_lock`` so concurrent readers (heartbeat, sync push) see
    a consistent snapshot.
    """
    from sqlalchemy import select
    from app.models.db import ClusterPeer
    try:
        async with db_factory() as db:
            rows = (await db.execute(
                select(ClusterPeer).where(ClusterPeer.removed_at.is_(None))
            )).scalars().all()
    except Exception as exc:
        logger.warning("_reload_peers_from_db.failed err=%s — keeping current in-memory peers", exc)
        return len(_peers)

    async with _peers_lock:
        db_ids = {r.id for r in rows}
        # Remove peers no longer in DB
        for stale in [pid for pid in list(_peers.keys()) if pid not in db_ids]:
            logger.info("cluster_peers: removing peer %s (no longer in DB)", stale)
            _peers.pop(stale, None)
        # Add / update peers from DB
        for r in rows:
            existing = _peers.get(r.id)
            if existing is None:
                _peers[r.id] = PeerNode(id=r.id, name=r.name or r.id, url=r.url)
                logger.info("cluster_peers: adding peer %s -> %s", r.id, r.url)
            else:
                # URL/name may have been edited; refresh those fields without
                # clobbering status/latency tracking.
                existing.url = r.url
                existing.name = r.name or r.id
        return len(_peers)


async def _prune_self_row_from_db(db_factory) -> int:
    """v5.0.25 / Batch 4 (BUG-057) — at startup, hard-delete any
    cluster_peers row whose id matches THIS node's cluster_node_id.

    Why: pre-v5.0.25, if an operator changed CLUSTER_NODE_ID and
    restarted, the row keyed by the OLD node id remained in the
    cluster_peers table. ``_apply_cluster_peers`` filtered it out only
    when the id matched the current node id, leaving an orphan that
    the manager pushed sync payloads to — infinite self-push loop OR
    misdirected traffic to whatever URL was attached.

    Pruning at startup makes the rename case safe. Operator-initiated
    deletes (via the UI) still produce soft-delete tombstones; this
    helper only hard-deletes rows whose id is OUR id (legitimately
    orphaned).
    """
    self_id = settings.cluster_node_id or ""
    if not self_id:
        return 0
    from sqlalchemy import delete
    from app.models.db import ClusterPeer
    try:
        async with db_factory() as db:
            result = await db.execute(
                delete(ClusterPeer).where(ClusterPeer.id == self_id)
            )
            await db.commit()
            n = int(result.rowcount or 0)
            if n > 0:
                logger.warning(
                    "cluster_peers: pruned %d self-row(s) matching "
                    "current cluster_node_id=%s (operator changed "
                    "CLUSTER_NODE_ID since last boot?)",
                    n, self_id,
                )
            return n
    except Exception as exc:
        logger.warning("_prune_self_row_from_db.failed err=%s", exc)
        return 0


async def _seed_peers_from_env_if_empty(db_factory) -> int:
    """v5.0.18 one-time migration: if cluster_peers table is empty AND
    CLUSTER_PEERS env is set, seed the table from env. Idempotent — a
    second call with the table already populated is a no-op.
    """
    import time as _t
    from sqlalchemy import select, func
    from app.models.db import ClusterPeer
    try:
        async with db_factory() as db:
            existing = (await db.execute(select(func.count(ClusterPeer.id)))).scalar_one()
            if existing > 0:
                # BUG-066 — operators were editing CLUSTER_PEERS env
                # and silently getting no behavior change because the
                # DB had taken over. Log so the env edit is visible
                # and not mysteriously ignored.
                env_peers = _parse_peers()
                if env_peers:
                    logger.info(
                        "cluster_peers: %d rows in DB; CLUSTER_PEERS "
                        "env is set (%d entries) but ignored — DB is "
                        "authoritative after first boot. Edit via "
                        "Settings → Cluster Peers UI or "
                        "POST /cluster/peers.",
                        existing, len(env_peers),
                    )
                return 0
            env_peers = _parse_peers()
            if not env_peers:
                return 0
            now = datetime.utcnow()
            now_ts = _t.time()
            for p in env_peers:
                db.add(ClusterPeer(
                    id=p.id, url=p.url, name=p.name,
                    added_at=now,
                    last_user_edit_at=now_ts,
                ))
            await db.commit()
            logger.info("cluster_peers: seeded %d peers from CLUSTER_PEERS env", len(env_peers))
            return len(env_peers)
    except Exception as exc:
        logger.warning("_seed_peers_from_env_if_empty.failed err=%s", exc)
        return 0



async def _heartbeat_loop(notify_fn=None):
    while True:
        await asyncio.sleep(settings.cluster_heartbeat_sec)
        for peer in list(_peers.values()):
            await _ping_peer(peer, notify_fn)


async def _ping_peer(peer: PeerNode, notify_fn=None):
    url = f"{peer.url.rstrip('/')}/health"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers={"X-Cluster-Node": settings.cluster_node_id or ""})
        latency_ms = (time.monotonic() - start) * 1000

        # v4.4.17 (F4) — distinguish "peer responded but isn't serving
        # normally" (non-200, or a non-JSON body — e.g. an nginx 502/504
        # HTML page while the peer's container is mid-restart) from "peer
        # truly unreachable" (connection refused / timeout — the except
        # block below). Pre-fix, a deploy-window 502 hit ``resp.json()``,
        # raised JSONDecodeError, and got logged identically to a dead
        # peer + fired the all-providers-down notifier. Now it's
        # classified as ``degraded`` (transient), logged at INFO, and does
        # NOT page — only genuine connection failures mark ``unreachable``
        # and notify.
        if resp.status_code != 200:
            was = peer.status
            peer.status = "degraded"
            peer.last_heartbeat = time.time()
            if was != "degraded":
                logger.info(
                    "Cluster peer %s degraded: HTTP %s (likely restarting)",
                    peer.id, resp.status_code,
                )
            return
        try:
            data = resp.json()
        except Exception as je:
            was = peer.status
            peer.status = "degraded"
            peer.last_heartbeat = time.time()
            if was != "degraded":
                logger.info(
                    "Cluster peer %s degraded: 200 but non-JSON body (%s) "
                    "— likely restarting / behind an error page",
                    peer.id, type(je).__name__,
                )
            return

        was_unreachable = peer.status in ("unreachable", "degraded")
        peer.latency_ms = latency_ms
        peer.last_heartbeat = time.time()
        peer.healthy_providers = data.get("healthyProviders", 0)
        peer.total_providers = data.get("totalProviders", 0)
        peer.status = data.get("status", "healthy")
        # v5.1.0 / Batch A2 — capture peer's reported version for the
        # Settings → Cluster skew display. /health is the only endpoint
        # that surfaces __version__; capturing here means we don't add
        # an extra round-trip.
        peer.version = data.get("version")

        if was_unreachable:
            logger.info(f"Cluster peer {peer.id} recovered")

    except Exception as e:
        if peer.status != "unreachable":
            # v4.4.16: same empty-exception-string fix as push_sync got in
            # v4.4.13. ``str(httpx.ReadTimeout())`` / ``ConnectError("")``
            # render blank, so "Cluster peer X unreachable: " lost the
            # diagnostic. Surface the exception class + non-empty message.
            # v4.4.17 (F4): this except now only catches CONNECTION-level
            # failures (refused/timeout/DNS) — non-200 + non-JSON are
            # handled above as ``degraded``, so reaching here means the
            # peer is genuinely unreachable and a page is warranted.
            msg = str(e) if str(e) else f"{type(e).__name__} (no message)"
            logger.warning("Cluster peer %s unreachable: %s: %s", peer.id, type(e).__name__, msg)
            peer.status = "unreachable"
            if notify_fn:
                await notify_fn(peer.id, peer.url)


async def _sync_loop(db_factory):
    """Push local users/keys to all peers every 60 seconds."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="cluster_sync_push")
    register_expected_interval("cluster_sync_push", 60)
    while True:
        await asyncio.sleep(60)
        pushed = 0
        failed = 0
        for peer in list(_peers.values()):
            if peer.status != "unreachable":
                try:
                    await push_sync(peer, db_factory)
                    pushed += 1
                except Exception:
                    failed += 1
        status = "ok" if failed == 0 else ("partial" if pushed else "error")
        await hb.tick(
            status=status,
            note=f"peers={len(_peers)} pushed={pushed} failed={failed}",
        )


async def _build_sync_payload(db) -> dict:
    # v5.0.22 — include tombstoned (soft-deleted) users so peers learn
    # about deletes (BUG-070). Mirrors api_keys / providers tombstone
    # replication. Without the tombstone, peer 'insert-if-missing'
    # merge resurrected deleted users.
    users_result = await db.execute(select(User))
    users = [
        {"id": u.id, "username": u.username, "password_hash": u.password_hash,
         "role": u.role, "created_at": str(u.created_at),
         "deleted_at": (u.deleted_at.isoformat() if u.deleted_at else None),
         "last_user_edit_at": u.last_user_edit_at}
        for u in users_result.scalars().all()
    ]
    # v3.0.20: include tombstoned (soft-deleted) keys so peers learn about
    # deletes; mirrors the v2.8.2 provider-tombstone replication path.
    keys_result = await db.execute(select(ApiKey))
    keys = [
        {"id": k.id, "name": k.name, "key_hash": k.key_hash, "key_prefix": k.key_prefix,
         "key_type": k.key_type, "enabled": k.enabled,
         "spending_cap_usd": k.spending_cap_usd,
         "rate_limit_rpm": k.rate_limit_rpm,
         "total_cost_usd": k.total_cost_usd or 0.0,
         "deleted_at": k.deleted_at.isoformat() if k.deleted_at else None,
         # v4.4.18 — broader field coverage. Pre-fix the apply handler
         # only wrote ``spending_cap_usd`` + ``rate_limit_rpm`` on update,
         # and the push omitted all the other operator-settable fields,
         # so flipping ``semantic_cache_enabled`` (or any of the budget /
         # LMRH / retention overrides) on one node never reached peers.
         # Surfaced 2026-05-22 when F3 from the routing-cost research
         # asked us to enable semantic cache on the coordinator-hub key
         # and only www1 picked it up.
         "semantic_cache_enabled": bool(getattr(k, "semantic_cache_enabled", False)),
         "daily_soft_cap_usd": getattr(k, "daily_soft_cap_usd", None),
         "daily_hard_cap_usd": getattr(k, "daily_hard_cap_usd", None),
         "hourly_cap_usd": getattr(k, "hourly_cap_usd", None),
         "rate_limit_tier": getattr(k, "rate_limit_tier", None),
         "caller_memory_ttl_days": getattr(k, "caller_memory_ttl_days", None),
         "lmrh_polling_rpm": getattr(k, "lmrh_polling_rpm", None),
         "lmrh_quotes_rpm": getattr(k, "lmrh_quotes_rpm", None),
         # v5.0.0 — compliance per-key policy fields. Same field-coverage
         # discipline as v4.4.18/v4.4.25: every operator-settable column
         # must round-trip via the sync payload, otherwise a PATCH on
         # one node never reaches peers.
         "blocked_companies": getattr(k, "blocked_companies", None),
         "allowed_paths": getattr(k, "allowed_paths", None),
         # v5.2.0 / Batch V2 — fine-grained vendor-neutrality policy
         # field coverage. Mirrors blocked_companies' discipline. The
         # apply handler uses membership-test pattern so pre-v5.2.0
         # peers omitting these keys don't clobber receiver state.
         "allowed_companies": getattr(k, "allowed_companies", None),
         "blocked_models": getattr(k, "blocked_models", None),
         "allowed_models": getattr(k, "allowed_models", None),
         "debug_echo_enabled": bool(getattr(k, "debug_echo_enabled", False)),
         # v4.4.20 — LWW gate, mirrors providers. Pre-v4.4.20 peers
         # omit this; apply handler treats absence as "legacy peer"
         # and falls through to last-sync-wins, same as today.
         "last_user_edit_at": getattr(k, "last_user_edit_at", None)}
        for k in keys_result.scalars().all()
    ]
    # v2.8.2: include tombstoned (soft-deleted) rows so peers learn about deletes.
    providers_result = await db.execute(select(Provider))
    providers = [
        {"id": p.id, "name": p.name, "provider_type": p.provider_type, "api_key": p.api_key,
         "base_url": p.base_url, "default_model": p.default_model, "priority": p.priority,
         "enabled": p.enabled, "timeout_sec": p.timeout_sec,
         "exclude_from_tool_requests": p.exclude_from_tool_requests,
         "hold_down_sec": p.hold_down_sec, "failure_threshold": p.failure_threshold,
         "extra_config": p.extra_config or {},
         # v3.0.10: previously-missing fields. Without these, daily-budget /
         # OAuth-token rotations on one node never reach peers. User-flagged
         # symptom: provider edits on www1 don't show up on www2.
         "daily_budget_usd": p.daily_budget_usd,
         "oauth_refresh_token": p.oauth_refresh_token,
         "oauth_expires_at": p.oauth_expires_at,
         "deleted_at": p.deleted_at.isoformat() if p.deleted_at else None,
         "updated_at": p.updated_at.isoformat() if p.updated_at else None,
         # v3.0.11: per-row "last admin-edit" timestamp. Cluster sync LWW
         # prefers this over updated_at so OAuth auto-refresh and other
         # background mutations can't revert a real config edit.
         "last_user_edit_at": p.last_user_edit_at,
         # v3.0.45 — provider tenant scoping
         "owned_by_key_id": p.owned_by_key_id,
         # v3.7.0/v3.7.1/v3.7.3 — Anthropic billing scrape + auto-rotation.
         # We sync the org_uuid (identifier, not sensitive), the
         # cookie-captured-at timestamp (so the "cookies are N days
         # old" UI works cluster-wide), and the auto-skip decision
         # (auto_skip_until / auto_skip_reason). We INTENTIONALLY do
         # NOT sync the cookies themselves — they stay on the node
         # where the operator pasted them, limiting auth-material
         # spread. The worker on peer nodes filters
         # ``Provider.anthropic_session_cookies.is_not(None)``, so
         # peers won't try to scrape without credentials.
         "anthropic_org_uuid": p.anthropic_org_uuid,
         "anthropic_session_captured_at": p.anthropic_session_captured_at,
         # v3.7.27 (#245) — Codex billing scrape state. Same posture as
         # Anthropic: replicate the endpoint URL + captured-at so peers
         # can render UI badges, but NOT the cookies (auth material
         # stays on the node where the operator pasted them).
         "codex_usage_endpoint_url": getattr(p, "codex_usage_endpoint_url", None),
         "codex_session_captured_at": getattr(p, "codex_session_captured_at", None),
         # v3.7.28 (#252 phase 1) — manual override lock. ALL fields
         # sync so any node can render the banner + 🔒 badge identically.
         "manual_override_until": p.manual_override_until.isoformat() if getattr(p, "manual_override_until", None) else None,
         "manual_override_set_by": getattr(p, "manual_override_set_by", None),
         "manual_override_set_at": p.manual_override_set_at.isoformat() if getattr(p, "manual_override_set_at", None) else None,
         "manual_override_reason": getattr(p, "manual_override_reason", None),
         "auto_skip_until": p.auto_skip_until.isoformat() if p.auto_skip_until else None,
         "auto_skip_reason": p.auto_skip_reason,
         # v5.0.0 — compliance: owner_company is used by the router
         # pre-filter to drop providers belonging to a banned company.
         # Auto-derived at create/update time from provider_type, with
         # per-row override allowed.
         "owner_company": getattr(p, "owner_company", None)}
        for p in providers_result.scalars().all()
    ]
    # Only push settings that were explicitly saved (have a DB row) — not env-var defaults
    settings_result = await db.execute(select(SystemSetting))
    node_settings = [
        {"key": s.key, "value": s.value, "value_type": s.value_type, "updated_at": s.updated_at or 0.0}
        for s in settings_result.scalars().all()
    ]
    # v3.0.25: replicate the LMRH dim registry + proposals queue so all
    # nodes see the same canonical name space. Last-write-wins by
    # registered_at / proposed_at.
    from app.models.db import LmrhDim, LmrhProposal
    dims_result = await db.execute(select(LmrhDim))
    lmrh_dims = [
        {"name": d.name, "owner_app": d.owner_app, "owner_key_id": d.owner_key_id,
         "semantics": d.semantics, "value_type": d.value_type, "kind": d.kind,
         "examples": d.examples or [], "requested_name": d.requested_name,
         "registered_at": d.registered_at, "registered_by_node": d.registered_by_node,
         # v3.0.29: tombstone replication so soft-deletes propagate.
         "deleted_at": d.deleted_at}
        for d in dims_result.scalars().all()
    ]
    proposals_result = await db.execute(select(LmrhProposal))
    lmrh_proposals = [
        {"id": p.id, "proposed_name": p.proposed_name, "rationale": p.rationale,
         "proposer_app": p.proposer_app, "proposer_key_id": p.proposer_key_id,
         "proposed_at": p.proposed_at, "status": p.status, "review_note": p.review_note,
         "deleted_at": p.deleted_at}
        for p in proposals_result.scalars().all()
    ]
    # v3.0.96: replicate operator-configured catalog tables.
    # v3.0.98 HOTFIX: disabled by default. The full-payload sync of 304
    # ModelCapability rows × SELECT-then-INSERT/UPDATE per row on the
    # receiver side made each /cluster/sync take 12-17s (was 200-700ms
    # pre-v3.0.96). With 30s push interval, that meant ~50% of every
    # minute the DB was busy applying sync — real /v1/messages calls
    # queued waiting for DB connections and timed out at the 60s nginx
    # upstream limit. coordinator-hub team observed 60s hangs from
    # llmp-CwLU on 2026-05-07.
    #
    # Restore by setting cluster_sync_catalog_tables=True. Proper
    # implementation (delta-only sync + batched apply) is queued for
    # v3.0.99.
    from app.models.db import ModelCapability, ModelAlias, OAuthCaptureProfile
    if getattr(settings, "cluster_sync_catalog_tables", False):
        caps_result = await db.execute(select(ModelCapability))
        model_capabilities = [
            {"provider_id": c.provider_id, "model_id": c.model_id,
             "tasks": c.tasks or [], "latency": c.latency or "medium",
             "cost_tier": c.cost_tier or "standard", "safety": c.safety or 3,
             "context_length": c.context_length or 128000,
             "regions": c.regions or [], "modalities": c.modalities or [],
             "native_reasoning": bool(c.native_reasoning),
             "native_tools": bool(c.native_tools) if c.native_tools is not None else True,
             "native_vision": bool(c.native_vision) if c.native_vision is not None else False,
             "source": c.source or "inferred",
             # v3.6.0 — aliases / family / variant must replicate so the
             # Hub model-identity edit endpoint produces cluster-wide
             # consistent results. Pre-v3.6.0 these were silently dropped
             # by the sync apply pass (model_capabilities entries didn't
             # include them) so a PUT on www01 wouldn't reach www02/GCP.
             "aliases": c.aliases or [],
             "model_family": c.model_family,
             "model_variant": c.model_variant,
             "updated_at": c.updated_at.isoformat() if c.updated_at else None}
            for c in caps_result.scalars().all()
        ]
        aliases_result = await db.execute(select(ModelAlias))
        model_aliases = [
            {"alias": a.alias, "provider_id": a.provider_id,
             "model_id": a.model_id, "description": a.description,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in aliases_result.scalars().all()
        ]
        profiles_result = await db.execute(select(OAuthCaptureProfile))
        oauth_capture_profiles = [
            {"name": p.name, "preset": p.preset,
             "upstream_urls": p.upstream_urls or [],
             "secret": p.secret, "enabled": bool(p.enabled),
             "notes": p.notes,
             "created_at": p.created_at.isoformat() if p.created_at else None}
            for p in profiles_result.scalars().all()
        ]
    else:
        model_capabilities = []
        model_aliases = []
        oauth_capture_profiles = []
    # v3.7.15 — BUG-016: replicate the three v3.7.x tables that landed
    # in a hurry without sync entries. LWW conflict resolution: latest
    # added_at / captured_at wins.
    from app.models.db import BlockedIp, ApiKeyAiReview, ExternalUsageSnapshot, ProviderAiReview, CallerMemory, CallerMemoryMarker, ProviderNodeAuthState
    # Include tombstoned rows so peers learn about deletions.
    blocked_rs = await db.execute(select(BlockedIp))
    blocked_ips_payload = [
        {"ip": b.ip, "reason": b.reason, "added_by": b.added_by,
         "added_at": b.added_at.isoformat() if b.added_at else None,
         "deleted_at": b.deleted_at.isoformat() if b.deleted_at else None}
        for b in blocked_rs.scalars().all()
    ]
    # Reviews — last 7 days is plenty for cross-node visibility; older
    # rows are operator-audit-only and don't need to be on every node.
    from datetime import timedelta as _td, datetime as _dtnow, timezone as _tz
    cutoff = _dtnow.now(_tz.utc) - _td(days=7)
    reviews_rs = await db.execute(select(ApiKeyAiReview).where(ApiKeyAiReview.captured_at >= cutoff))
    ai_reviews_payload = [
        {"id": r.id, "api_key_id": r.api_key_id,
         "captured_at": r.captured_at.isoformat() if r.captured_at else None,
         "llm_model": r.llm_model, "llm_verdict": r.llm_verdict,
         "llm_reasoning": r.llm_reasoning, "suggested_action": r.suggested_action,
         "stats_summary": r.stats_summary,
         "applied_at": r.applied_at.isoformat() if r.applied_at else None,
         "applied_action": r.applied_action,
         "prior_rate_limit_rpm": r.prior_rate_limit_rpm,
         "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
         "dismissed_at": r.dismissed_at.isoformat() if r.dismissed_at else None,
         "suggested_block_ip": r.suggested_block_ip}
        for r in reviews_rs.scalars().all()
    ]
    # External usage snapshots: only keep the LATEST per (provider_id)
    # cluster-wide. Older history is per-node audit; the routing layer
    # only consults the latest row. Each node also has its own scrape
    # in case the leader's last scrape was stale.
    from sqlalchemy import func as _sqlfunc
    latest_per_provider = (
        select(ExternalUsageSnapshot.provider_id,
               _sqlfunc.max(ExternalUsageSnapshot.captured_at).label("last_at"))
        .group_by(ExternalUsageSnapshot.provider_id)
        .subquery()
    )
    snap_rs = await db.execute(
        select(ExternalUsageSnapshot).join(
            latest_per_provider,
            (ExternalUsageSnapshot.provider_id == latest_per_provider.c.provider_id)
            & (ExternalUsageSnapshot.captured_at == latest_per_provider.c.last_at)
        )
    )
    external_usage_payload = [
        {"provider_id": s.provider_id,
         "captured_at": s.captured_at.isoformat() if s.captured_at else None,
         "source": s.source, "http_status": s.http_status,
         "error": s.error, "auth_state": s.auth_state,
         "five_hour_utilization": s.five_hour_utilization,
         "five_hour_resets_at": s.five_hour_resets_at.isoformat() if s.five_hour_resets_at else None,
         "seven_day_utilization": s.seven_day_utilization,
         "seven_day_resets_at": s.seven_day_resets_at.isoformat() if s.seven_day_resets_at else None,
         "seven_day_sonnet_utilization": s.seven_day_sonnet_utilization,
         "seven_day_sonnet_resets_at": s.seven_day_sonnet_resets_at.isoformat() if s.seven_day_sonnet_resets_at else None,
         "seven_day_opus_utilization": s.seven_day_opus_utilization,
         "seven_day_opus_resets_at": s.seven_day_opus_resets_at.isoformat() if s.seven_day_opus_resets_at else None,
         "extra_usage_is_enabled": s.extra_usage_is_enabled,
         "extra_usage_monthly_limit": s.extra_usage_monthly_limit,
         "extra_usage_used_credits": s.extra_usage_used_credits,
         "extra_usage_utilization": s.extra_usage_utilization,
         "extra_usage_currency": s.extra_usage_currency}
        for s in snap_rs.scalars().all()
    ]
    # v3.7.31 (#252 phase 4) — provider AI reviews (last 7d).
    provider_reviews_rs = await db.execute(
        select(ProviderAiReview).where(ProviderAiReview.captured_at >= cutoff)
    )
    provider_ai_reviews_payload = [
        {"id": r.id, "provider_id": r.provider_id,
         "captured_at": r.captured_at.isoformat() if r.captured_at else None,
         "llm_model": r.llm_model, "llm_verdict": r.llm_verdict,
         "llm_reasoning": r.llm_reasoning,
         "suggested_priority_delta": r.suggested_priority_delta,
         "suggested_auto_skip_hours": r.suggested_auto_skip_hours,
         "stats_summary": r.stats_summary,
         "applied_at": r.applied_at.isoformat() if r.applied_at else None,
         "applied_action": r.applied_action,
         "prior_priority": r.prior_priority,
         "prior_auto_skip_until": r.prior_auto_skip_until.isoformat() if r.prior_auto_skip_until else None,
         "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
         "dismissed_at": r.dismissed_at.isoformat() if r.dismissed_at else None}
        for r in provider_reviews_rs.scalars().all()
    ]

    # v3.8.7 (#267) Phase 2 — caller memory (king-store for cross-provider
    # memory state). Cluster-sync LWW by (api_key_id, conversation_id,
    # memory_tag) + updated_at. Last 7d window to match the existing
    # sync-payload posture; older rows are operator-audit-only.
    memory_rs = await db.execute(
        select(CallerMemory).where(CallerMemory.updated_at >= time.time() - 7 * 86400)
    )
    caller_memory_payload = [
        {"api_key_id": r.api_key_id,
         "conversation_id": r.conversation_id,
         "memory_tag": r.memory_tag,
         "content": r.content,
         "content_format": r.content_format,
         "updated_at": r.updated_at,
         "updated_by_node": r.updated_by_node,
         "source_provider_id": r.source_provider_id,
         "source_request_id": r.source_request_id,
         "deleted_at": r.deleted_at}
        for r in memory_rs.scalars().all()
    ]
    # v4.4 M-2 — per-node bridge auth state (Path A foundation).
    # Send ALL rows on every push; the table is small (≤ providers ×
    # nodes; today ≤ 10 × 3 = 30 rows) and LWW resolution on apply
    # handles ordering. No time-window filter — we want late joiners
    # to immediately see the full per-node-state picture.
    pnas_rs = await db.execute(select(ProviderNodeAuthState))
    provider_node_auth_states_payload = [
        {"provider_id": r.provider_id,
         "node_id": r.node_id,
         "auth_state": r.auth_state,
         "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
         "last_check_at": r.last_check_at.isoformat() if r.last_check_at else None,
         "reauth_url": r.reauth_url,
         "last_error": r.last_error}
        for r in pnas_rs.scalars().all()
    ]

    # Markers are smaller + lower-frequency; send all non-deleted rows
    # so back-pressure recovery has full visibility cluster-wide.
    marker_rs = await db.execute(
        select(CallerMemoryMarker).where(CallerMemoryMarker.deleted_at.is_(None))
    )
    caller_memory_markers_payload = [
        {"api_key_id": r.api_key_id,
         "conversation_id": r.conversation_id,
         "memory_tag": r.memory_tag,
         "first_seen_at": r.first_seen_at,
         "last_known_provider_id": r.last_known_provider_id,
         "last_known_external_ref": r.last_known_external_ref,
         "recovered_at": r.recovered_at,
         "deleted_at": r.deleted_at}
        for r in marker_rs.scalars().all()
    ]

    # v5.0.0 — compliance_events + compliance_policy_changes. Both tables
    # are append-only audit logs; receiver dedupes on the unique
    # business key (audit_id / policy_change_id). Spec §6 calls for
    # "rows since last sync"; this codebase has no last-sync watermark
    # so we push the last 1000 rows by created_at — idempotent on the
    # receiver thanks to the dedup, and 1000 is well above the per-sync
    # event rate for the foreseeable future.
    from app.models.db import ComplianceEvent, CompliancePolicyChange
    from app.cluster.sync_handlers import (
        serialize_compliance_event,
        serialize_policy_change,
    )
    events_rs = await db.execute(
        select(ComplianceEvent)
        .order_by(ComplianceEvent.id.desc())
        .limit(1000)
    )
    compliance_events_payload = [
        serialize_compliance_event(r) for r in events_rs.scalars().all()
    ]
    policy_changes_rs = await db.execute(
        select(CompliancePolicyChange)
        .order_by(CompliancePolicyChange.id.desc())
        .limit(1000)
    )
    compliance_policy_changes_payload = [
        serialize_policy_change(r) for r in policy_changes_rs.scalars().all()
    ]

    # v5.0.18 — UI-configurable cluster peer list. Replicate the full
    # cluster_peers table (including tombstones — receiver's apply
    # handler honours removed_at) so add/remove operations propagate.
    from app.models.db import ClusterPeer as _CP
    cp_rs = await db.execute(select(_CP))
    cluster_peers_payload = [
        {
            "id": r.id,
            "url": r.url,
            "name": r.name,
            "added_at": r.added_at.isoformat() if r.added_at else None,
            "removed_at": r.removed_at.isoformat() if r.removed_at else None,
            "last_user_edit_at": r.last_user_edit_at,
        }
        for r in cp_rs.scalars().all()
    ]

    return {
        "source_node": settings.cluster_node_id,
        "timestamp": time.time(),
        "users": users,
        "api_keys": keys,
        "providers": providers,
        "settings": node_settings,
        "lmrh_dims": lmrh_dims,
        "lmrh_proposals": lmrh_proposals,
        "model_capabilities": model_capabilities,
        "model_aliases": model_aliases,
        "oauth_capture_profiles": oauth_capture_profiles,
        # v3.7.15 — BUG-016
        "blocked_ips": blocked_ips_payload,
        "api_key_ai_reviews": ai_reviews_payload,
        "external_usage_snapshots": external_usage_payload,
        # v4.4 M-2 — per-node grok-bridge (or other per-node-session
        # provider) auth state. Each node owns the rows where
        # node_id == settings.cluster_node_id; cluster sync gives
        # everyone a global picture for routing + UI display.
        "provider_node_auth_states": provider_node_auth_states_payload,
        # v3.7.31 (#252 phase 4) — provider supervisor reviews follow
        # the same posture as api_key_ai_reviews: last 7 days, PK is
        # (provider_id, captured_at).
        "provider_ai_reviews": provider_ai_reviews_payload,
        # v3.8.7 (#267) Phase 2 — caller memory king-store.
        "caller_memory": caller_memory_payload,
        "caller_memory_markers": caller_memory_markers_payload,
        # v5.0.0 — compliance audit trail; append-only on the receiver.
        "compliance_events": compliance_events_payload,
        "compliance_policy_changes": compliance_policy_changes_payload,
        # v5.0.18 — UI-configurable peer list with LWW + tombstones.
        "cluster_peers": cluster_peers_payload,
    }


async def push_sync(peer: PeerNode, db_factory):
    async with db_factory() as db:
        payload = await _build_sync_payload(db)
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_payload(body)

    try:
        # v4.4.13: timeout raised 15→45s. The 15s ceiling held while the
        # sync payload was ~500 KB but with unbounded ai-review tables it
        # grew to ~2.8 MB; live measurement 2026-05-21 showed c1conv at
        # 10.7s and www2 timing out at 15s. v4.4.13 also adds the
        # ai_review prune to bound the table, but the timeout bump is a
        # belt-and-braces hedge — a transiently slow peer shouldn't get
        # falsely logged as failed.
        async with httpx.AsyncClient(timeout=45, verify=False) as client:
            resp = await client.post(
                f"{peer.url.rstrip('/')}/cluster/sync",
                content=body,
                headers={"X-Cluster-Node": settings.cluster_node_id or "", "X-Cluster-Sig": sig,
                         "Content-Type": "application/json"},
            )
        # v4.4.24 (BUG-081) — inspect the peer response. Pre-fix this was a
        # fire-and-forget POST: a peer 500-ing on apply_sync (e.g. BUG-079's
        # MultipleResultsFound) was completely invisible to the originator,
        # which is why a fully-broken cluster sync went undetected for ~6
        # days while heartbeat still reported "healthy". A non-200 here is
        # the peer rejecting our payload — surface it loudly.
        if resp.status_code != 200:
            body_preview = resp.text[:300] if resp.text else "(empty body)"
            logger.warning(
                "Sync to %s REJECTED: HTTP %s — %s",
                peer.id, resp.status_code, body_preview,
            )
    except Exception as e:
        # v4.4.13: render the exception with both class name AND str(),
        # because empty-message exceptions like ``httpx.ReadTimeout()`` and
        # ``ConnectError("")`` rendered as a bare "Sync to X failed: "
        # — exactly the diagnostic gap the ``_exc_str`` helper was created
        # for in ``_messages_streaming.py``. Duplicated here to avoid a
        # cross-package import; v4.4.12's split-invariant test pattern is
        # not warranted for a 3-line helper. If the message ever drifts,
        # operators will notice because both paths surface in the same
        # activity-log search.
        msg = str(e) if str(e) else f"{type(e).__name__} (no message)"
        logger.warning("Sync to %s failed: %s: %s", peer.id, type(e).__name__, msg)


_push_sync = push_sync


# ───────────────────────────────────────────────────────────────────
# v5.0.0 — compliance policy-change quorum fan-out (spec §6.1).
# ───────────────────────────────────────────────────────────────────


class ClusterSyncQuorumNotReached(Exception):
    """v5.0.0 — raised when ``push_policy_change_with_quorum`` cannot
    collect ``required_acks`` peer ACKs within the timeout window. The
    caller (the policy-change endpoint) rolls back the local edit and
    surfaces a 503 to the operator so they know the cluster is partitioned
    or unhealthy."""

    def __init__(self, acks, pending):
        self.acks = acks
        self.pending = pending
        super().__init__(
            f"quorum not reached: {len(acks)} acks, {len(pending)} pending"
        )


def _active_peers() -> list[PeerNode]:
    """Peers we'd consider eligible for a policy-change fan-out: anything
    not currently unreachable. ``degraded`` peers are still attempted —
    the quorum result will surface them as pending if they fail to ack."""
    return [p for p in _peers.values() if p.status != "unreachable"]


async def _push_to_peer(peer: PeerNode, payload: dict, timeout_sec: float):
    """POST one signed payload to a peer's /cluster/sync. Returns the
    peer + ack timestamp on success. Raises on any failure (HTTP non-200,
    network error, timeout) — the caller categorises it as ``pending``."""
    from datetime import datetime as _dt, timezone as _tz
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_payload(body)
    async with httpx.AsyncClient(timeout=timeout_sec, verify=False) as client:
        resp = await client.post(
            f"{peer.url.rstrip('/')}/cluster/sync",
            content=body,
            headers={
                "X-Cluster-Node": settings.cluster_node_id or "",
                "X-Cluster-Sig": sig,
                "Content-Type": "application/json",
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return peer, _dt.now(_tz.utc)


async def push_policy_change_with_quorum(
    payload: dict,
    required_acks: int,
    timeout_sec: float = 5.0,
) -> dict:
    """v5.0.0 spec §6.1 — fan out a policy-change payload to all active
    peers; return as soon as ``required_acks`` peers ACK or the timeout
    elapses.

    Module-level (not method) because ``manager.py`` is module-scoped in
    this codebase — there's no ``ClusterManager`` class. The spec wrote
    it as ``self.push_policy_change_with_quorum`` against an assumed
    class layout; the call site is the same shape either way.

    Returns ``{applied_to_peers, pending_peers, cluster_sync_status}``.
    Raises ``ClusterSyncQuorumNotReached`` if ``required_acks`` cannot
    be collected within ``timeout_sec``."""
    peers = _active_peers()
    acks: list[dict] = []
    pending: list[dict] = []

    if not peers:
        # Single-node cluster — quorum is trivially the local write.
        if required_acks <= 0:
            return {
                "applied_to_peers": acks,
                "pending_peers": pending,
                "cluster_sync_status": "fully-acked",
            }
        raise ClusterSyncQuorumNotReached(acks, pending)

    # Build a task→peer index so we can name lagging peers in the
    # pending list after quorum is reached.
    task_to_peer: dict[asyncio.Task, PeerNode] = {}
    for p in peers:
        t = asyncio.create_task(_push_to_peer(p, payload, timeout_sec))
        task_to_peer[t] = p

    deadline = asyncio.get_event_loop().time() + timeout_sec
    try:
        while task_to_peer:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait(
                list(task_to_peer.keys()),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for t in done:
                p = task_to_peer.pop(t)
                try:
                    _, ack_time = t.result()
                    acks.append({"peer": p.name, "acked_at": ack_time.isoformat()})
                except Exception as e:
                    pending.append({"peer": p.name, "reason": str(e) or type(e).__name__})
            if len(acks) >= required_acks:
                # Quorum reached — flag still-running tasks as lagging
                # and stop waiting. Don't cancel: the writes might still
                # land, and the next regular sync cycle will reconcile.
                for t, p in task_to_peer.items():
                    pending.append({"peer": p.name, "reason": "lagging"})
                task_to_peer.clear()
                break
    finally:
        # Anything still in task_to_peer at this point timed out; mark
        # those peers pending too.
        for t, p in task_to_peer.items():
            pending.append({"peer": p.name, "reason": "timeout"})

    if len(acks) < required_acks:
        raise ClusterSyncQuorumNotReached(acks, pending)

    status = (
        "fully-acked"
        if not pending
        else f"quorum-reached-{len(pending)}-pending"
    )
    return {
        "applied_to_peers": acks,
        "pending_peers": pending,
        "cluster_sync_status": status,
    }


def get_cluster_status() -> dict:
    # v5.1.0 / Batch A2 — include version on local + peer entries so
    # the UI can flag skew at a glance. Local version is the canonical
    # source (from __version__); peer versions come from their /health
    # responses as captured by _ping_peer.
    from app.__version__ import __version__ as _local_version
    return {
        "cluster_enabled": settings.cluster_enabled,
        "local_node": {
            "id": settings.cluster_node_id,
            "name": settings.cluster_node_name,
            "url": settings.cluster_node_url,
            "status": "healthy",
            "version": _local_version,
        },
        "peers": [
            {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "status": p.status,
                "latency_ms": round(p.latency_ms, 1),
                "last_heartbeat": p.last_heartbeat,
                "healthy_providers": p.healthy_providers,
                "total_providers": p.total_providers,
                "version": p.version,
            }
            for p in _peers.values()
        ],
        "total_nodes": 1 + len(_peers),
        "healthy_nodes": 1 + sum(1 for p in _peers.values() if p.status == "healthy"),
    }


async def _peer_refresh_loop(db_factory):
    """v5.0.18 — every 30s, refresh ``_peers`` from the cluster_peers
    table so UI-driven add/remove ops on this or any peer node take
    effect within one cycle without a container restart."""
    while True:
        await asyncio.sleep(30)
        try:
            await _reload_peers_from_db(db_factory)
        except Exception as exc:
            logger.warning("peer_refresh_loop.iteration_failed err=%s", exc)


_peer_refresh_task: Optional[asyncio.Task] = None


def start_cluster(db_factory, notify_fn=None):
    global _heartbeat_task, _sync_task, _peer_refresh_task
    if not settings.cluster_enabled:
        return

    # v5.0.18 — defer the seed+load to the event loop so we can use
    # the async DB factory. Pre-v5.0.18 used the synchronous env parse
    # only; that path is now the safety-net inside _reload_peers_from_db.
    async def _bootstrap():
        # v5.0.25 / Batch 4 (BUG-057) — prune any stale self-row
        # BEFORE the seed/load so the rest of bootstrap operates on
        # a clean cluster_peers table.
        await _prune_self_row_from_db(db_factory)
        await _seed_peers_from_env_if_empty(db_factory)
        await _reload_peers_from_db(db_factory)
        # If both DB and env failed, fall back to env-only legacy behavior
        # to preserve pre-v5.0.18 startup semantics.
        if not _peers:
            for peer in _parse_peers():
                _peers[peer.id] = peer
        logger.info(f"Cluster started — {len(_peers)} peers registered")

    asyncio.create_task(_bootstrap())
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(notify_fn))
    _sync_task = asyncio.create_task(_sync_loop(db_factory))
    _peer_refresh_task = asyncio.create_task(_peer_refresh_loop(db_factory))
