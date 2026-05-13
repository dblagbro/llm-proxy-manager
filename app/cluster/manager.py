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


peers: dict[str, PeerNode] = {}
_heartbeat_task: Optional[asyncio.Task] = None
_sync_task: Optional[asyncio.Task] = None

# Private alias for internal use within this module
_peers = peers


def active_node_count() -> int:
    """Number of nodes currently reachable, including self."""
    return 1 + sum(1 for p in _peers.values() if p.status != "unreachable")


def _parse_peers() -> list[PeerNode]:
    raw = settings.cluster_peers or ""
    nodes = []
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        node_id, _, url = item.partition(":")
        nodes.append(PeerNode(id=node_id.strip(), name=node_id.strip(), url=url.strip()))
    return nodes



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
        data = resp.json()

        was_unreachable = peer.status == "unreachable"
        peer.latency_ms = latency_ms
        peer.last_heartbeat = time.time()
        peer.healthy_providers = data.get("healthyProviders", 0)
        peer.total_providers = data.get("totalProviders", 0)
        peer.status = data.get("status", "healthy")

        if was_unreachable:
            logger.info(f"Cluster peer {peer.id} recovered")

    except Exception as e:
        if peer.status != "unreachable":
            logger.warning(f"Cluster peer {peer.id} unreachable: {e}")
            peer.status = "unreachable"
            if notify_fn:
                await notify_fn(peer.id, peer.url)


async def _sync_loop(db_factory):
    """Push local users/keys to all peers every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        for peer in list(_peers.values()):
            if peer.status != "unreachable":
                await push_sync(peer, db_factory)


async def _build_sync_payload(db) -> dict:
    users_result = await db.execute(select(User))
    users = [
        {"id": u.id, "username": u.username, "password_hash": u.password_hash,
         "role": u.role, "created_at": str(u.created_at)}
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
         "deleted_at": k.deleted_at.isoformat() if k.deleted_at else None}
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
         "auto_skip_reason": p.auto_skip_reason}
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
    from app.models.db import BlockedIp, ApiKeyAiReview, ExternalUsageSnapshot, ProviderAiReview
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
        # v3.7.31 (#252 phase 4) — provider supervisor reviews follow
        # the same posture as api_key_ai_reviews: last 7 days, PK is
        # (provider_id, captured_at).
        "provider_ai_reviews": provider_ai_reviews_payload,
    }


async def push_sync(peer: PeerNode, db_factory):
    async with db_factory() as db:
        payload = await _build_sync_payload(db)
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_payload(body)

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            await client.post(
                f"{peer.url.rstrip('/')}/cluster/sync",
                content=body,
                headers={"X-Cluster-Node": settings.cluster_node_id or "", "X-Cluster-Sig": sig,
                         "Content-Type": "application/json"},
            )
    except Exception as e:
        logger.warning(f"Sync to {peer.id} failed: {e}")


_push_sync = push_sync


def get_cluster_status() -> dict:
    return {
        "cluster_enabled": settings.cluster_enabled,
        "local_node": {
            "id": settings.cluster_node_id,
            "name": settings.cluster_node_name,
            "url": settings.cluster_node_url,
            "status": "healthy",
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
            }
            for p in _peers.values()
        ],
        "total_nodes": 1 + len(_peers),
        "healthy_nodes": 1 + sum(1 for p in _peers.values() if p.status == "healthy"),
    }


def start_cluster(db_factory, notify_fn=None):
    global _heartbeat_task, _sync_task
    if not settings.cluster_enabled:
        return

    for peer in _parse_peers():
        _peers[peer.id] = peer

    _heartbeat_task = asyncio.create_task(_heartbeat_loop(notify_fn))
    _sync_task = asyncio.create_task(_sync_loop(db_factory))
    logger.info(f"Cluster started — {len(_peers)} peers registered")
