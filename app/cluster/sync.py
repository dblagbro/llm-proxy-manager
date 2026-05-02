"""Cluster data sync — incoming payload merge logic.

Handles the insert-if-missing / update-if-changed strategy for users, API keys,
providers, and settings received from peer nodes during cluster synchronisation.
"""
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.db import User, ApiKey, Provider, SystemSetting

logger = logging.getLogger(__name__)

# Per-peer cost accumulator: {peer_node_id: {key_id: total_cost_usd}}
# Imported by manager.py and auth/keys.py for global spending-cap enforcement.
_peer_key_costs: dict[str, dict[str, float]] = {}


def get_peer_total_cost(key_id: str) -> float:
    """Sum of total_cost_usd reported by all peers for a given key."""
    return sum(costs.get(key_id, 0.0) for costs in _peer_key_costs.values())


async def apply_sync(db: AsyncSession, payload: dict) -> None:
    """Merge incoming user/key/provider/settings data from a peer (insert-if-missing strategy)."""
    for u_data in payload.get("users", []):
        result = await db.execute(select(User).where(User.username == u_data["username"]))
        existing = result.scalar_one_or_none()
        if not existing:
            db.add(User(
                id=u_data["id"],
                username=u_data["username"],
                password_hash=u_data["password_hash"],
                role=u_data.get("role", "user"),
            ))

    source_node = payload.get("source_node", "unknown")
    peer_costs: dict[str, float] = {}

    # v3.0.20: tombstone-aware api-key merge. Without this, hard-DELETE on
    # one node was reversed by the next sync push from a peer that still
    # had the row. Peer's ``deleted_at`` propagates the soft-delete; our
    # local tombstone is preserved if peer's payload doesn't carry one.
    from datetime import datetime as _dt
    def _parse_iso_kt(v):
        if not v:
            return None
        if isinstance(v, str):
            try:
                return _dt.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return v

    for k_data in payload.get("api_keys", []):
        peer_deleted_at = _parse_iso_kt(k_data.get("deleted_at"))
        result = await db.execute(select(ApiKey).where(ApiKey.key_hash == k_data["key_hash"]))
        existing = result.scalar_one_or_none()
        if existing:
            # Peer reports a tombstone — propagate it locally if we don't
            # already have one (or if peer's stamp is newer).
            if peer_deleted_at and (
                existing.deleted_at is None or peer_deleted_at >= existing.deleted_at
            ):
                existing.deleted_at = peer_deleted_at
                existing.enabled = False
                continue
            # Local tombstone outranks any peer state when peer is not also
            # tombstoned — the delete was authoritative on this node.
            if existing.deleted_at is not None and peer_deleted_at is None:
                continue
            if "spending_cap_usd" in k_data:
                existing.spending_cap_usd = k_data["spending_cap_usd"]
            if "rate_limit_rpm" in k_data:
                existing.rate_limit_rpm = k_data["rate_limit_rpm"]
        else:
            # No local row. Don't materialize a peer's tombstone — just skip.
            if peer_deleted_at is not None:
                continue
            db.add(ApiKey(
                id=k_data["id"],
                name=k_data["name"],
                key_hash=k_data["key_hash"],
                key_prefix=k_data["key_prefix"],
                key_type=k_data.get("key_type", "standard"),
                enabled=k_data.get("enabled", True),
                spending_cap_usd=k_data.get("spending_cap_usd"),
                rate_limit_rpm=k_data.get("rate_limit_rpm"),
            ))
        key_id = k_data.get("id")
        if key_id and "total_cost_usd" in k_data:
            peer_costs[key_id] = float(k_data["total_cost_usd"])

    _peer_key_costs[source_node] = peer_costs

    from app.monitoring.status import register_provider
    from datetime import datetime
    def _parse_iso(v):
        if not v:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return v

    for p_data in payload.get("providers", []):
        peer_deleted_at = _parse_iso(p_data.get("deleted_at"))
        peer_updated_at = _parse_iso(p_data.get("updated_at"))
        # v3.0.11: per-row admin-edit timestamp (Unix float). When both sides
        # have one, this gates LWW — auto-refresh / migration writes that
        # only bump updated_at can't revert real edits on a peer.
        peer_user_edit_at = p_data.get("last_user_edit_at")
        if peer_user_edit_at is not None:
            try:
                peer_user_edit_at = float(peer_user_edit_at)
            except (TypeError, ValueError):
                peer_user_edit_at = None

        result = await db.execute(select(Provider).where(Provider.id == p_data["id"]))
        existing = result.scalar_one_or_none()
        if existing is None:
            # Match by name as a fallback (legacy rows synced pre-v2.8.2 may
            # have different ids on each node).
            result2 = await db.execute(select(Provider).where(Provider.name == p_data["name"]))
            existing = result2.scalar_one_or_none()

        if existing is not None:
            # v2.8.2: tombstone-aware merge.
            local_updated = existing.updated_at
            local_deleted = existing.deleted_at

            # If peer has a tombstone and it's newer than our local state,
            # propagate the soft-delete locally.
            if peer_deleted_at and (
                local_updated is None or peer_deleted_at >= local_updated
            ):
                existing.deleted_at = peer_deleted_at
                existing.enabled = False
                if peer_updated_at:
                    existing.updated_at = peer_updated_at
                continue

            # If WE have a tombstone newer than the peer's update, do nothing
            # — local delete wins until peer sees our tombstone next sync.
            if local_deleted and (
                peer_updated_at is None or local_deleted >= peer_updated_at
            ):
                continue

            # v2.8.3: last-write-wins by updated_at for active rows.
            # If local was modified after the peer's payload was built,
            # ignore the peer push to avoid clobbering newer local state.
            # v3.0.11: when BOTH sides carry a last_user_edit_at, gate on
            # it instead of updated_at — that way background mutations
            # (OAuth refresh, deprecation auto-bump, priority tie-break)
            # on a peer can't revert a real admin edit on this node.
            local_user_edit = existing.last_user_edit_at
            if peer_user_edit_at is not None and local_user_edit is not None:
                accept = peer_user_edit_at >= local_user_edit
            elif local_user_edit is not None and peer_user_edit_at is None:
                # Local row was admin-edited (v3.0.11+); peer's payload
                # carries no admin-edit stamp — could be a legacy v3.0.10
                # peer or a peer where only background mutations bumped
                # updated_at. Conservative: keep local edit. The peer
                # will receive our payload on the return sync and
                # converge once it upgrades.
                accept = False
            else:
                # Neither side has a user-edit stamp — legacy LWW path.
                accept = (peer_updated_at is None or local_updated is None
                          or peer_updated_at >= local_updated)
            if accept:
                # v3.0.10: previously, ``name`` was sent but never applied
                # — so renames on one node never propagated. Add it.
                # Also pick up the new daily_budget_usd + OAuth fields the
                # payload now includes (v3.0.10 manager.py change).
                if "name" in p_data:
                    existing.name = p_data["name"]
                existing.api_key = p_data.get("api_key", existing.api_key)
                existing.base_url = p_data.get("base_url", existing.base_url)
                existing.default_model = p_data.get("default_model", existing.default_model)
                existing.priority = p_data.get("priority", existing.priority)
                existing.enabled = p_data.get("enabled", existing.enabled)
                existing.timeout_sec = p_data.get("timeout_sec", existing.timeout_sec)
                existing.exclude_from_tool_requests = p_data.get("exclude_from_tool_requests", existing.exclude_from_tool_requests)
                existing.hold_down_sec = p_data.get("hold_down_sec", existing.hold_down_sec)
                existing.failure_threshold = p_data.get("failure_threshold", existing.failure_threshold)
                existing.extra_config = p_data.get("extra_config", existing.extra_config)
                # v3.0.45: tenant-scope ownership replicates with the row.
                if "owned_by_key_id" in p_data:
                    existing.owned_by_key_id = p_data["owned_by_key_id"]
                if "daily_budget_usd" in p_data:
                    existing.daily_budget_usd = p_data["daily_budget_usd"]
                if "oauth_refresh_token" in p_data:
                    existing.oauth_refresh_token = p_data["oauth_refresh_token"]
                if "oauth_expires_at" in p_data:
                    existing.oauth_expires_at = p_data["oauth_expires_at"]
                if peer_updated_at:
                    existing.updated_at = peer_updated_at
                # v3.0.11: preserve peer's user-edit timestamp so further
                # syncs use the originating node's stamp, not "now".
                if peer_user_edit_at is not None:
                    existing.last_user_edit_at = peer_user_edit_at
            continue

        # No local row — create unless peer is sending a tombstone (no point
        # materializing a deleted row).
        if peer_deleted_at is not None:
            continue
        # v3.0.10: include all replicated fields (daily_budget_usd + OAuth)
        # so a fresh peer-imported row matches the source-of-truth shape.
        p = Provider(
            id=p_data["id"],
            name=p_data["name"],
            provider_type=p_data["provider_type"],
            api_key=p_data.get("api_key"),
            base_url=p_data.get("base_url"),
            default_model=p_data.get("default_model"),
            priority=p_data.get("priority", 10),
            enabled=p_data.get("enabled", True),
            timeout_sec=p_data.get("timeout_sec", 60),
            exclude_from_tool_requests=p_data.get("exclude_from_tool_requests", False),
            hold_down_sec=p_data.get("hold_down_sec"),
            failure_threshold=p_data.get("failure_threshold"),
            extra_config=p_data.get("extra_config", {}),
            owned_by_key_id=p_data.get("owned_by_key_id"),  # v3.0.45
            daily_budget_usd=p_data.get("daily_budget_usd"),
            oauth_refresh_token=p_data.get("oauth_refresh_token"),
            oauth_expires_at=p_data.get("oauth_expires_at"),
            last_user_edit_at=peer_user_edit_at,
        )
        db.add(p)
        register_provider(p.id, p.provider_type, p.hold_down_sec, p.failure_threshold)

    # Merge settings — last-write-wins by updated_at timestamp
    from app import config_runtime
    settings_to_apply: dict = {}
    for s_data in payload.get("settings", []):
        key = s_data.get("key", "")
        if key not in config_runtime.SCHEMA:
            continue
        incoming_ts = float(s_data.get("updated_at", 0))
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        existing = result.scalar_one_or_none()
        if existing and (existing.updated_at or 0) >= incoming_ts:
            continue
        if existing:
            existing.value = s_data["value"]
            existing.value_type = s_data.get("value_type", "str")
            existing.updated_at = incoming_ts
        else:
            db.add(SystemSetting(
                key=key,
                value=s_data["value"],
                value_type=s_data.get("value_type", "str"),
                updated_at=incoming_ts,
            ))
        settings_to_apply[key] = config_runtime._coerce(s_data["value"], s_data.get("value_type", "str"))

    # v2.8.2: normalize any priority ties introduced by the merge so every
    # node converges on the same strict total order. Deterministic by
    # (priority, created_at, id) — all peers arrive at the same answer.
    try:
        from app.api.providers import normalize_priority_ties
        bumped = await normalize_priority_ties(db)
        if bumped:
            logger.info("cluster_sync_normalized_ties count=%s", bumped)
    except Exception:
        logger.exception("priority-tie normalization failed during sync apply")

    # R5: ingest replicated Run state. Last-write-wins by updated_at.
    # Workers do NOT spawn here — only the owner_node_id node spawns; if
    # ownership changes via /v1/runs/<id>/adopt that endpoint handles
    # the spawn explicitly.
    from app.models.db import Run
    for r_data in payload.get("runs", []):
        rid = r_data.get("id")
        if not rid:
            continue
        result = await db.execute(select(Run).where(Run.id == rid))
        existing = result.scalar_one_or_none()
        incoming_ts = float(r_data.get("updated_at") or 0)
        if existing is not None:
            if (existing.updated_at or 0) >= incoming_ts:
                continue  # ours is newer — keep it
            for col in (
                "api_key_id", "owner_node_id", "status", "current_step",
                "deadline_ts", "max_turns", "model_preference",
                "compaction_model", "system_prompt", "tools_spec",
                "metadata_json", "trace_id", "model_calls", "tool_calls",
                "tokens_in", "tokens_out", "last_provider_id",
                "context_summarized_at_turn", "current_tool_use_id",
                "current_tool_name", "current_tool_input",
                "result_text", "error_kind", "error_message",
                "created_at", "updated_at", "completed_at",
            ):
                if col in r_data:
                    setattr(existing, col, r_data[col])
        else:
            db.add(Run(
                id=rid,
                api_key_id=r_data.get("api_key_id", ""),
                owner_node_id=r_data.get("owner_node_id", ""),
                status=r_data.get("status", "queued"),
                current_step=r_data.get("current_step"),
                deadline_ts=r_data.get("deadline_ts", 0.0),
                max_turns=r_data.get("max_turns", 30),
                model_preference=r_data.get("model_preference") or [],
                compaction_model=r_data.get("compaction_model"),
                system_prompt=r_data.get("system_prompt"),
                tools_spec=r_data.get("tools_spec") or [],
                metadata_json=r_data.get("metadata_json") or {},
                trace_id=r_data.get("trace_id"),
                model_calls=r_data.get("model_calls", 0),
                tool_calls=r_data.get("tool_calls", 0),
                tokens_in=r_data.get("tokens_in", 0),
                tokens_out=r_data.get("tokens_out", 0),
                last_provider_id=r_data.get("last_provider_id"),
                context_summarized_at_turn=r_data.get("context_summarized_at_turn"),
                current_tool_use_id=r_data.get("current_tool_use_id"),
                current_tool_name=r_data.get("current_tool_name"),
                current_tool_input=r_data.get("current_tool_input"),
                result_text=r_data.get("result_text"),
                error_kind=r_data.get("error_kind"),
                error_message=r_data.get("error_message"),
                created_at=r_data.get("created_at", time.time()),
                updated_at=incoming_ts or time.time(),
                completed_at=r_data.get("completed_at"),
            ))

    # v3.0.25: replicate the LMRH dim registry + proposals queue.
    # Dims are immutable once registered (the canonical name space is the
    # whole point), so the merge is "insert if missing by name" with a
    # tie-break on registered_at for the corner case where two nodes raced
    # the same name through their suffix-resolver. Proposals are mutable
    # (status changes during operator review) and merge LWW on proposed_at
    # plus operator-touched columns.
    from app.models.db import LmrhDim, LmrhProposal
    for d_data in payload.get("lmrh_dims", []):
        name = d_data.get("name")
        if not name:
            continue
        # v3.0.29: peer's tombstone — propagate the soft delete.
        peer_deleted = d_data.get("deleted_at")
        peer_deleted_f = float(peer_deleted) if peer_deleted is not None else None
        result = await db.execute(select(LmrhDim).where(LmrhDim.name == name))
        existing = result.scalar_one_or_none()
        if existing is None:
            # Don't materialize a peer's tombstone — just skip.
            if peer_deleted_f is not None:
                continue
            db.add(LmrhDim(
                name=name,
                owner_app=d_data.get("owner_app"),
                owner_key_id=d_data.get("owner_key_id"),
                semantics=d_data.get("semantics"),
                value_type=d_data.get("value_type"),
                kind=d_data.get("kind", "advisory"),
                examples=d_data.get("examples") or [],
                requested_name=d_data.get("requested_name"),
                registered_at=float(d_data.get("registered_at") or time.time()),
                registered_by_node=d_data.get("registered_by_node"),
            ))
        else:
            # Tombstone propagation: peer reports a delete and ours is newer
            # OR we don't have one → adopt it.
            if peer_deleted_f is not None and (
                existing.deleted_at is None or peer_deleted_f >= existing.deleted_at
            ):
                existing.deleted_at = peer_deleted_f
                continue
            # Local tombstone newer than (or peer has none) → keep local delete
            if existing.deleted_at is not None and peer_deleted_f is None:
                continue
            # Earlier registered_at wins — preserves the originating node's
            # claim if a peer somehow allocated the same name independently.
            peer_ts = float(d_data.get("registered_at") or 0)
            local_ts = float(existing.registered_at or 0)
            if peer_ts and local_ts and peer_ts < local_ts:
                existing.owner_app = d_data.get("owner_app")
                existing.owner_key_id = d_data.get("owner_key_id")
                existing.semantics = d_data.get("semantics")
                existing.value_type = d_data.get("value_type")
                existing.kind = d_data.get("kind", existing.kind)
                existing.examples = d_data.get("examples") or []
                existing.requested_name = d_data.get("requested_name")
                existing.registered_at = peer_ts
                existing.registered_by_node = d_data.get("registered_by_node")

    for pr_data in payload.get("lmrh_proposals", []):
        proposed_name = pr_data.get("proposed_name")
        proposed_at = pr_data.get("proposed_at")
        if not proposed_name or proposed_at is None:
            continue
        # Identity = (proposed_name, proposed_at) — a proposer creating two
        # proposals for the same dim at exactly the same float second is
        # not a real concern.
        result = await db.execute(
            select(LmrhProposal).where(
                LmrhProposal.proposed_name == proposed_name,
                LmrhProposal.proposed_at == float(proposed_at),
            )
        )
        existing = result.scalar_one_or_none()
        # v3.0.29: tombstone propagation for proposals (same shape as dims).
        peer_deleted = pr_data.get("deleted_at")
        peer_deleted_f = float(peer_deleted) if peer_deleted is not None else None
        if existing is None:
            if peer_deleted_f is not None:
                continue
            db.add(LmrhProposal(
                proposed_name=proposed_name,
                rationale=pr_data.get("rationale"),
                proposer_app=pr_data.get("proposer_app"),
                proposer_key_id=pr_data.get("proposer_key_id"),
                proposed_at=float(proposed_at),
                status=pr_data.get("status", "pending"),
                review_note=pr_data.get("review_note"),
            ))
        else:
            if peer_deleted_f is not None and (
                existing.deleted_at is None or peer_deleted_f >= existing.deleted_at
            ):
                existing.deleted_at = peer_deleted_f
                continue
            if existing.deleted_at is not None and peer_deleted_f is None:
                continue
            # Operator review is the only thing that mutates an existing
            # proposal. Accept peer's status/review_note unconditionally —
            # whoever wrote last wins, and the review fields don't have a
            # separate timestamp.
            existing.status = pr_data.get("status", existing.status)
            existing.review_note = pr_data.get("review_note", existing.review_note)

    await db.commit()

    if settings_to_apply:
        config_runtime.apply(settings_to_apply)
        logger.info("cluster_settings_applied count=%s keys=%s", len(settings_to_apply), list(settings_to_apply))
