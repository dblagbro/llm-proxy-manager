"""Cluster data sync — incoming payload merge logic.

Handles the insert-if-missing / update-if-changed strategy for users, API keys,
providers, and settings received from peer nodes during cluster synchronisation.
"""
import logging
import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


def _parse_iso_or_none(v):
    """Module-level ISO parse helper (mirror of the local one in
    apply_payload). Used by v3.7.3 provider-insert path."""
    if not v:
        return None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return v

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
    from datetime import datetime, timezone
    def _parse_iso(v):
        # Returns a NAIVE datetime in UTC. SQLAlchemy's ``Column(DateTime)``
        # without ``timezone=True`` returns naive values when reading from
        # SQLite, so we strip tzinfo here to keep comparisons consistent
        # across all the LWW branches below. Without this, peer payloads
        # (ISO strings with explicit offsets) compare as tz-aware against
        # locally-loaded naive datetimes and TypeError out on >=/> ops.
        if not v:
            return None
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        elif isinstance(v, datetime):
            dt = v
        else:
            return v
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

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
                # v3.5.9 BUG-012 fix — clear local CB state on inbound
                # tombstone propagation. Same cleanup as the admin
                # DELETE endpoint does; without it, a delete that
                # happens on a peer leaves our local /health
                # reporting ghost CB entries until container restart.
                try:
                    from app.routing.circuit_breaker import (
                        _local_states as _cb_states,
                        _auth_failed as _cb_auth_failed,
                    )
                    _cb_states.pop(existing.id, None)
                    _cb_auth_failed.pop(existing.id, None)
                except Exception:
                    pass  # cluster sync must not fail if CB import shifts
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
                # v3.0.63: STRICT-greater. Was `>=` which meant on ties
                # (post-sync, both sides carry identical last_user_edit_at)
                # peer always won — creating a ping-pong where any
                # divergent state on either node would flip back and
                # forth on each sync cycle.
                # Strict-greater + tie → keep local means an admin edit
                # only gets overwritten by an explicitly NEWER edit
                # elsewhere.
                # v3.2.7: when peer_user_edit_at == local_user_edit (a real
                # tie, NOT a missing stamp), fall through to legacy LWW on
                # ``updated_at``. This catches background mutations —
                # direct DB writes, sync-cascade flushes, OAuth refresh on
                # a non-claude/codex provider type — where the row genuinely
                # changed but no admin path bumped last_user_edit_at. The
                # anti-ping-pong concern is preserved because the legacy
                # LWW path below also uses STRICT-greater on updated_at
                # for ties, so genuinely-converged state stays converged.
                # Bug surfaced 2026-05-08: bridge_url change in extra_config
                # on www01 didn't propagate to peers for hours (hand-fixed
                # node-by-node) until the operator wondered why.
                if peer_user_edit_at != local_user_edit:
                    accept = peer_user_edit_at > local_user_edit
                else:
                    accept = (
                        peer_updated_at is not None
                        and local_updated is not None
                        and peer_updated_at > local_updated
                    )
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
                # v3.1.4: replicate provider_type changes too. Pre-fix this
                # field was set on insert but never on update, so admin
                # changes to provider type (e.g. openai → openrouter)
                # silently failed to propagate. Caught when shipping v3.1.3
                # OpenRouter support: www01 had type=openrouter post-edit,
                # www02 stayed at type=openai despite same last_user_edit_at.
                if "provider_type" in p_data:
                    existing.provider_type = p_data["provider_type"]
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
                # v3.7.3 — Anthropic billing scrape + auto-rotation
                # fields. Membership-test so an empty/null from a
                # peer correctly overwrites a stale local value.
                # ``anthropic_session_cookies`` is INTENTIONALLY not
                # synced (auth material stays on the capture node).
                if "anthropic_org_uuid" in p_data:
                    existing.anthropic_org_uuid = p_data.get("anthropic_org_uuid")
                if "anthropic_session_captured_at" in p_data:
                    existing.anthropic_session_captured_at = p_data.get("anthropic_session_captured_at")
                # v3.7.27 (#245) — Codex billing scrape state. Same posture as
                # the Anthropic fields above. Cookies are NOT synced (auth
                # material stays on the capture node); only the endpoint URL
                # + captured-at timestamp.
                if "codex_usage_endpoint_url" in p_data:
                    existing.codex_usage_endpoint_url = p_data.get("codex_usage_endpoint_url")
                if "codex_session_captured_at" in p_data:
                    existing.codex_session_captured_at = p_data.get("codex_session_captured_at")
                # v3.7.28 (#252 phase 1) — manual override lock. All 4 fields
                # sync via membership-test so a null overwrite from a peer
                # correctly releases a stale local lock.
                if "manual_override_until" in p_data:
                    existing.manual_override_until = _parse_iso_or_none(p_data.get("manual_override_until"))
                if "manual_override_set_by" in p_data:
                    existing.manual_override_set_by = p_data.get("manual_override_set_by")
                if "manual_override_set_at" in p_data:
                    existing.manual_override_set_at = _parse_iso_or_none(p_data.get("manual_override_set_at"))
                if "manual_override_reason" in p_data:
                    existing.manual_override_reason = p_data.get("manual_override_reason")
                if "auto_skip_until" in p_data:
                    val = p_data.get("auto_skip_until")
                    from datetime import datetime
                    if val:
                        try:
                            existing.auto_skip_until = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        except Exception:
                            existing.auto_skip_until = None
                    else:
                        existing.auto_skip_until = None
                if "auto_skip_reason" in p_data:
                    existing.auto_skip_reason = p_data.get("auto_skip_reason")
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
            # v3.7.3 — billing-scrape + auto-rotation fields. Cookies
            # are intentionally not synced (stay on the capture node).
            anthropic_org_uuid=p_data.get("anthropic_org_uuid"),
            anthropic_session_captured_at=p_data.get("anthropic_session_captured_at"),
            # v3.7.27 (#245) — Codex billing scrape state.
            codex_usage_endpoint_url=p_data.get("codex_usage_endpoint_url"),
            codex_session_captured_at=p_data.get("codex_session_captured_at"),
            # v3.7.28 (#252 phase 1) — manual override lock.
            manual_override_until=_parse_iso_or_none(p_data.get("manual_override_until")),
            manual_override_set_by=p_data.get("manual_override_set_by"),
            manual_override_set_at=_parse_iso_or_none(p_data.get("manual_override_set_at")),
            manual_override_reason=p_data.get("manual_override_reason"),
            auto_skip_until=_parse_iso_or_none(p_data.get("auto_skip_until")),
            auto_skip_reason=p_data.get("auto_skip_reason"),
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

    # v3.0.96: replicate operator-configured catalog tables. Apply
    # AFTER providers (above) so FK references to providers.id resolve.
    from app.models.db import ModelCapability, ModelAlias, OAuthCaptureProfile

    # ── ModelCapability ─────────────────────────────────────────────────
    # v3.1.2: bulk fetch + in-memory diff replaces the per-row SELECT loop.
    # With 304 rows the old loop ran 12-17s per sync (root cause of the
    # 2026-05-07 60s /v1/messages hang incident). The new path:
    #   (1) ONE bulk SELECT pulls all existing rows whose (provider_id,
    #       model_id) matches any incoming row;
    #   (2) per-row diff happens in memory (no DB round-trips);
    #   (3) inserts go through ``db.add()`` and flush in batch on commit;
    #   (4) updates mutate the loaded instance in place — also batched.
    # ON CONFLICT was tried first but rejected: the table's PK is an
    # autoincrement ``id`` column, no composite UNIQUE on (provider_id,
    # model_id), so no constraint to conflict against. Adding one would
    # need a migration with dup-detection — overkill for the win.
    # Identity = (provider_id, model_id). LWW by updated_at when both
    # sides have a stamp. Peer wins on insert.
    cap_rows = payload.get("model_capabilities", []) or []
    if cap_rows:
        # FK pre-filter — skip rows whose referenced provider hasn't
        # replicated yet. Defer to next sync cycle.
        known_provider_ids = {
            pid for (pid,) in (await db.execute(select(Provider.id))).all()
        }
        # Filter + collect incoming keys for the bulk fetch
        valid_data: list[dict] = []
        incoming_keys: set[tuple[str, str]] = set()
        for c_data in cap_rows:
            prov_id = c_data.get("provider_id")
            model_id = c_data.get("model_id")
            if not prov_id or not model_id:
                continue
            if prov_id not in known_provider_ids:
                continue
            valid_data.append(c_data)
            incoming_keys.add((prov_id, model_id))

        if valid_data:
            # ONE bulk SELECT — pulls every row whose composite key matches
            # any incoming row. SQLAlchemy `tuple_().in_()` compiles to
            # `WHERE (provider_id, model_id) IN ((...), (...), ...)` on
            # SQLite, evaluated as a single planned query.
            from sqlalchemy import tuple_
            existing_q = await db.execute(
                select(ModelCapability).where(
                    tuple_(
                        ModelCapability.provider_id,
                        ModelCapability.model_id,
                    ).in_(list(incoming_keys))
                )
            )
            existing_by_key: dict[tuple[str, str], ModelCapability] = {
                (r.provider_id, r.model_id): r
                for r in existing_q.scalars().all()
            }

            # Now apply each row using the pre-fetched index. This loop is
            # in-memory — no DB round-trips per row. db.add()/instance
            # mutations are flushed by the outer commit.
            for c_data in valid_data:
                prov_id = c_data["provider_id"]
                model_id = c_data["model_id"]
                peer_updated = _parse_iso(c_data.get("updated_at"))
                existing = existing_by_key.get((prov_id, model_id))
                if existing is None:
                    db.add(ModelCapability(
                        provider_id=prov_id,
                        model_id=model_id,
                        tasks=c_data.get("tasks") or [],
                        latency=c_data.get("latency") or "medium",
                        cost_tier=c_data.get("cost_tier") or "standard",
                        safety=c_data.get("safety") or 3,
                        context_length=c_data.get("context_length") or 128000,
                        regions=c_data.get("regions") or [],
                        modalities=c_data.get("modalities") or [],
                        native_reasoning=bool(c_data.get("native_reasoning")),
                        native_tools=(
                            bool(c_data.get("native_tools"))
                            if c_data.get("native_tools") is not None else True
                        ),
                        native_vision=(
                            bool(c_data.get("native_vision"))
                            if c_data.get("native_vision") is not None else False
                        ),
                        source=c_data.get("source") or "inferred",
                        # v3.6.0 — replicate identity fields on insert
                        aliases=c_data.get("aliases") or [],
                        model_family=c_data.get("model_family"),
                        model_variant=c_data.get("model_variant"),
                    ))
                    continue
                # LWW: skip if local is newer or equal
                local_updated = existing.updated_at
                if peer_updated and local_updated and peer_updated <= local_updated:
                    continue
                existing.tasks = c_data.get("tasks") or existing.tasks
                existing.latency = c_data.get("latency") or existing.latency
                existing.cost_tier = c_data.get("cost_tier") or existing.cost_tier
                if c_data.get("safety") is not None:
                    existing.safety = c_data["safety"]
                if c_data.get("context_length") is not None:
                    existing.context_length = c_data["context_length"]
                existing.regions = c_data.get("regions") or existing.regions
                existing.modalities = c_data.get("modalities") or existing.modalities
                if c_data.get("native_reasoning") is not None:
                    existing.native_reasoning = bool(c_data["native_reasoning"])
                if c_data.get("native_tools") is not None:
                    existing.native_tools = bool(c_data["native_tools"])
                if c_data.get("native_vision") is not None:
                    existing.native_vision = bool(c_data["native_vision"])
                existing.source = c_data.get("source") or existing.source
                # v3.6.0 — replicate identity fields on update. Use the
                # ``in c_data`` membership test instead of a truthy check
                # so an empty list (cleared aliases) or null (cleared
                # family/variant) overwrites correctly.
                if "aliases" in c_data:
                    existing.aliases = c_data.get("aliases") or []
                if "model_family" in c_data:
                    existing.model_family = c_data.get("model_family")
                if "model_variant" in c_data:
                    existing.model_variant = c_data.get("model_variant")
                if peer_updated:
                    existing.updated_at = peer_updated

    # ── ModelAlias ──────────────────────────────────────────────────────
    # Identity = alias (PK). No updated_at — peer-wins on update.
    for a_data in payload.get("model_aliases", []):
        alias = a_data.get("alias")
        if not alias:
            continue
        # FK guard
        prov_id = a_data.get("provider_id")
        if prov_id:
            prov_check = (await db.execute(
                select(Provider.id).where(Provider.id == prov_id)
            )).scalar_one_or_none()
            if prov_check is None:
                continue  # defer until provider syncs
        result = await db.execute(select(ModelAlias).where(ModelAlias.alias == alias))
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(ModelAlias(
                alias=alias,
                provider_id=prov_id,
                model_id=a_data.get("model_id") or "",
                description=a_data.get("description"),
            ))
        else:
            # Apply only fields that differ (avoid no-op writes)
            if a_data.get("provider_id") != existing.provider_id:
                existing.provider_id = a_data.get("provider_id")
            if a_data.get("model_id") and a_data.get("model_id") != existing.model_id:
                existing.model_id = a_data["model_id"]
            if a_data.get("description") != existing.description:
                existing.description = a_data.get("description")

    # ── OAuthCaptureProfile ─────────────────────────────────────────────
    # Identity = name (PK). No updated_at — peer-wins on update.
    # NOTE: secret field is treated as cluster-shared (same secret on each
    # node so any node can verify capture-side requests).
    for p_data in payload.get("oauth_capture_profiles", []):
        name = p_data.get("name")
        if not name:
            continue
        result = await db.execute(
            select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == name)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(OAuthCaptureProfile(
                name=name,
                preset=p_data.get("preset"),
                upstream_urls=p_data.get("upstream_urls") or [],
                secret=p_data.get("secret"),
                enabled=bool(p_data.get("enabled")),
                notes=p_data.get("notes"),
            ))
        else:
            if p_data.get("preset") != existing.preset:
                existing.preset = p_data.get("preset")
            if p_data.get("upstream_urls") is not None:
                existing.upstream_urls = p_data["upstream_urls"] or []
            if p_data.get("secret") != existing.secret and p_data.get("secret"):
                existing.secret = p_data["secret"]
            if p_data.get("enabled") is not None:
                existing.enabled = bool(p_data["enabled"])
            if p_data.get("notes") != existing.notes:
                existing.notes = p_data.get("notes")

    # v3.7.15 — BUG-016: merge the three v3.7.x tables. LWW by
    # added_at / captured_at. Tracks whether the block-list mutated
    # so we can invalidate the middleware cache after commit
    # (BUG-018 fix).
    blocked_ips_changed = await _apply_blocked_ips(db, payload.get("blocked_ips", []))
    await _apply_ai_reviews(db, payload.get("api_key_ai_reviews", []))
    await _apply_external_usage_snapshots(db, payload.get("external_usage_snapshots", []))
    await _apply_provider_ai_reviews(db, payload.get("provider_ai_reviews", []))
    # v3.8.7 (#267) Phase 2 — caller memory king-store
    await _apply_caller_memory(db, payload.get("caller_memory", []))
    await _apply_caller_memory_markers(db, payload.get("caller_memory_markers", []))

    await db.commit()

    # v3.7.15 — BUG-018: peer nodes were waiting up to 30s for their
    # in-memory cache to expire after an admin block was synced. Now
    # we invalidate explicitly on receipt — the next request reloads
    # from the freshly-synced row.
    if blocked_ips_changed:
        try:
            from app.middleware.ip_block import _clear_cache_for_tests
            _clear_cache_for_tests()
            logger.info("cluster_ip_block_cache_invalidated")
        except Exception as exc:
            logger.warning("cluster_ip_block_cache_invalidate_failed err=%s", exc)

    if settings_to_apply:
        config_runtime.apply(settings_to_apply)
        logger.info("cluster_settings_applied count=%s keys=%s", len(settings_to_apply), list(settings_to_apply))


async def _apply_blocked_ips(db: AsyncSession, rows: list[dict]) -> bool:
    """v3.7.15 — merge incoming blocked_ips. Returns True if any
    row was inserted, updated, or tombstoned (so the caller can
    invalidate the ip_block middleware cache).

    LWW semantics:
      - peer.deleted_at non-null + > local.deleted_at → propagate tombstone
      - peer.added_at > local.added_at AND peer.deleted_at is null →
        re-arm (clears local tombstone if any)
      - otherwise no-op
    """
    from app.models.db import BlockedIp
    changed = False
    for r in rows:
        ip = (r.get("ip") or "").strip()
        if not ip:
            continue
        peer_added_at = _parse_iso_or_none(r.get("added_at"))
        peer_deleted_at = _parse_iso_or_none(r.get("deleted_at"))
        result = await db.execute(select(BlockedIp).where(BlockedIp.ip == ip))
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(BlockedIp(
                ip=ip,
                reason=r.get("reason"),
                added_by=r.get("added_by"),
                added_at=peer_added_at,
                deleted_at=peer_deleted_at,
            ))
            # Only counts as "changed cache state" if the row is live
            if peer_deleted_at is None:
                changed = True
        else:
            # Tombstone propagation: peer says deleted, local doesn't
            # know yet (or is older). Adopt the tombstone.
            if peer_deleted_at and (
                existing.deleted_at is None or peer_deleted_at > existing.deleted_at
            ):
                existing.deleted_at = peer_deleted_at
                changed = True
            # Re-arm: peer's added_at is newer than ours AND peer has
            # no tombstone — clear local tombstone, update fields.
            elif peer_added_at and peer_deleted_at is None and (
                existing.added_at is None or peer_added_at > existing.added_at
            ):
                if existing.deleted_at is not None:
                    existing.deleted_at = None
                    changed = True
                if existing.reason != r.get("reason"):
                    existing.reason = r.get("reason")
                    changed = True
                if existing.added_by != r.get("added_by"):
                    existing.added_by = r.get("added_by")
                    changed = True
    return changed


async def _apply_ai_reviews(db: AsyncSession, rows: list[dict]) -> None:
    """v3.7.15 — merge incoming api_key_ai_review rows. PK is
    auto-increment integer per node, so we de-dup by
    (api_key_id, captured_at). LWW on the lifecycle fields
    (applied_at / reverted_at / dismissed_at)."""
    from app.models.db import ApiKeyAiReview
    for r in rows:
        api_key_id = r.get("api_key_id")
        captured_at = _parse_iso_or_none(r.get("captured_at"))
        if not (api_key_id and captured_at):
            continue
        existing = (await db.execute(
            select(ApiKeyAiReview)
            .where(ApiKeyAiReview.api_key_id == api_key_id)
            .where(ApiKeyAiReview.captured_at == captured_at)
        )).scalar_one_or_none()
        if existing is None:
            db.add(ApiKeyAiReview(
                api_key_id=api_key_id,
                captured_at=captured_at,
                llm_model=r.get("llm_model"),
                llm_verdict=r.get("llm_verdict") or "watch",
                llm_reasoning=r.get("llm_reasoning"),
                suggested_action=r.get("suggested_action") or "none",
                stats_summary=r.get("stats_summary"),
                applied_at=_parse_iso_or_none(r.get("applied_at")),
                applied_action=r.get("applied_action"),
                prior_rate_limit_rpm=r.get("prior_rate_limit_rpm"),
                reverted_at=_parse_iso_or_none(r.get("reverted_at")),
                dismissed_at=_parse_iso_or_none(r.get("dismissed_at")),
                suggested_block_ip=r.get("suggested_block_ip"),
            ))
        else:
            # Lifecycle transitions are monotone (None → set), so we
            # accept any non-None peer value the local row doesn't have.
            for field in ("applied_at", "reverted_at", "dismissed_at"):
                peer_val = _parse_iso_or_none(r.get(field))
                if peer_val and getattr(existing, field) is None:
                    setattr(existing, field, peer_val)
            if r.get("applied_action") and not existing.applied_action:
                existing.applied_action = r["applied_action"]


async def _apply_provider_ai_reviews(db: AsyncSession, rows: list[dict]) -> None:
    """v3.7.31 (#252 phase 4) — merge incoming provider_ai_review rows.
    Same pattern as _apply_ai_reviews for api_key_ai_review: PK is
    auto-increment integer per node, so we de-dup by (provider_id,
    captured_at). LWW on the monotone lifecycle fields (applied_at /
    reverted_at / dismissed_at)."""
    from app.models.db import ProviderAiReview
    for r in rows:
        provider_id = r.get("provider_id")
        captured_at = _parse_iso_or_none(r.get("captured_at"))
        if not (provider_id and captured_at):
            continue
        existing = (await db.execute(
            select(ProviderAiReview)
            .where(ProviderAiReview.provider_id == provider_id)
            .where(ProviderAiReview.captured_at == captured_at)
        )).scalar_one_or_none()
        if existing is None:
            db.add(ProviderAiReview(
                provider_id=provider_id,
                captured_at=captured_at,
                llm_model=r.get("llm_model"),
                llm_verdict=r.get("llm_verdict") or "watch",
                llm_reasoning=r.get("llm_reasoning"),
                suggested_priority_delta=r.get("suggested_priority_delta"),
                suggested_auto_skip_hours=r.get("suggested_auto_skip_hours"),
                stats_summary=r.get("stats_summary"),
                applied_at=_parse_iso_or_none(r.get("applied_at")),
                applied_action=r.get("applied_action"),
                prior_priority=r.get("prior_priority"),
                prior_auto_skip_until=_parse_iso_or_none(r.get("prior_auto_skip_until")),
                reverted_at=_parse_iso_or_none(r.get("reverted_at")),
                dismissed_at=_parse_iso_or_none(r.get("dismissed_at")),
            ))
        else:
            for field in ("applied_at", "reverted_at", "dismissed_at"):
                peer_val = _parse_iso_or_none(r.get(field))
                if peer_val and getattr(existing, field) is None:
                    setattr(existing, field, peer_val)
            if r.get("applied_action") and not existing.applied_action:
                existing.applied_action = r["applied_action"]


async def _apply_caller_memory(db: AsyncSession, rows: list[dict]) -> None:
    """v3.8.7 (#267) Phase 2 — merge incoming caller_memory rows.

    Dedup by (api_key_id, conversation_id, memory_tag). LWW by
    updated_at. Tombstones (deleted_at non-null) propagate so a
    DELETE on one node reaches peers.

    NOTE: this only manages the SQLite king-store. Redis cache
    invalidation happens at the call site (Phase 3 ship) — apply_sync
    must be cheap and not require Redis to be up.
    """
    from app.models.db import CallerMemory
    for r in rows:
        akid = r.get("api_key_id")
        conv = r.get("conversation_id")  # nullable
        tag = r.get("memory_tag") or "default"
        if not akid:
            continue
        peer_ts = r.get("updated_at") or 0
        existing = (await db.execute(
            select(CallerMemory)
            .where(CallerMemory.api_key_id == akid)
            .where(CallerMemory.conversation_id.is_(None) if conv is None else CallerMemory.conversation_id == conv)
            .where(CallerMemory.memory_tag == tag)
        )).scalar_one_or_none()
        if existing is None:
            db.add(CallerMemory(
                api_key_id=akid,
                conversation_id=conv,
                memory_tag=tag,
                content=r.get("content") or "",
                content_format=r.get("content_format") or "text",
                updated_at=peer_ts,
                updated_by_node=r.get("updated_by_node"),
                source_provider_id=r.get("source_provider_id"),
                source_request_id=r.get("source_request_id"),
                deleted_at=r.get("deleted_at"),
            ))
        else:
            # LWW: only adopt the peer's view when its timestamp is
            # strictly newer (== keeps local stamps stable on tie).
            if peer_ts > (existing.updated_at or 0):
                existing.content = r.get("content") or ""
                existing.content_format = r.get("content_format") or "text"
                existing.updated_at = peer_ts
                existing.updated_by_node = r.get("updated_by_node")
                existing.source_provider_id = r.get("source_provider_id")
                existing.source_request_id = r.get("source_request_id")
                existing.deleted_at = r.get("deleted_at")


async def _apply_caller_memory_markers(db: AsyncSession, rows: list[dict]) -> None:
    """v3.8.7 (#267) Phase 2 — merge incoming caller_memory_marker
    rows. Markers are monotonically-extending records:
    first_seen_at never decreases, last_known_* updates with each
    cross-provider write, recovered_at lifecycle transitions
    None→set.

    Dedup by (api_key_id, conversation_id, memory_tag).
    """
    from app.models.db import CallerMemoryMarker
    for r in rows:
        akid = r.get("api_key_id")
        conv = r.get("conversation_id")
        tag = r.get("memory_tag") or "default"
        if not akid:
            continue
        existing = (await db.execute(
            select(CallerMemoryMarker)
            .where(CallerMemoryMarker.api_key_id == akid)
            .where(CallerMemoryMarker.conversation_id.is_(None) if conv is None else CallerMemoryMarker.conversation_id == conv)
            .where(CallerMemoryMarker.memory_tag == tag)
        )).scalar_one_or_none()
        if existing is None:
            db.add(CallerMemoryMarker(
                api_key_id=akid,
                conversation_id=conv,
                memory_tag=tag,
                first_seen_at=r.get("first_seen_at") or 0,
                last_known_provider_id=r.get("last_known_provider_id"),
                last_known_external_ref=r.get("last_known_external_ref"),
                recovered_at=r.get("recovered_at"),
                deleted_at=r.get("deleted_at"),
            ))
        else:
            # first_seen_at is the EARLIEST — keep the min so backups
            # that restore older markers don't accidentally shift it
            # forward.
            peer_first = r.get("first_seen_at") or 0
            if peer_first and (existing.first_seen_at is None or peer_first < existing.first_seen_at):
                existing.first_seen_at = peer_first
            # last_known_* picks the most recent write — but we don't
            # have a timestamp for those; treat any non-null peer value
            # as more authoritative than a local null.
            if r.get("last_known_provider_id") and not existing.last_known_provider_id:
                existing.last_known_provider_id = r["last_known_provider_id"]
            if r.get("last_known_external_ref") and not existing.last_known_external_ref:
                existing.last_known_external_ref = r["last_known_external_ref"]
            # recovered_at is monotone (None→set, never reverts)
            peer_rec = r.get("recovered_at")
            if peer_rec and not existing.recovered_at:
                existing.recovered_at = peer_rec


async def _apply_external_usage_snapshots(db: AsyncSession, rows: list[dict]) -> None:
    """v3.7.15 — merge incoming external_usage_snapshot rows. Each row
    represents one provider's latest snapshot at peer-capture-time.
    Insert if no row exists at that exact captured_at; append-only
    (we never overwrite history)."""
    from app.models.db import ExternalUsageSnapshot
    for r in rows:
        provider_id = r.get("provider_id")
        captured_at = _parse_iso_or_none(r.get("captured_at"))
        if not (provider_id and captured_at):
            continue
        existing = (await db.execute(
            select(ExternalUsageSnapshot)
            .where(ExternalUsageSnapshot.provider_id == provider_id)
            .where(ExternalUsageSnapshot.captured_at == captured_at)
            .limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(ExternalUsageSnapshot(
            provider_id=provider_id,
            captured_at=captured_at,
            source=r.get("source") or "anthropic_console_v1",
            http_status=r.get("http_status"),
            error=r.get("error"),
            auth_state=r.get("auth_state"),
            five_hour_utilization=r.get("five_hour_utilization"),
            five_hour_resets_at=_parse_iso_or_none(r.get("five_hour_resets_at")),
            seven_day_utilization=r.get("seven_day_utilization"),
            seven_day_resets_at=_parse_iso_or_none(r.get("seven_day_resets_at")),
            seven_day_sonnet_utilization=r.get("seven_day_sonnet_utilization"),
            seven_day_sonnet_resets_at=_parse_iso_or_none(r.get("seven_day_sonnet_resets_at")),
            seven_day_opus_utilization=r.get("seven_day_opus_utilization"),
            seven_day_opus_resets_at=_parse_iso_or_none(r.get("seven_day_opus_resets_at")),
            extra_usage_is_enabled=r.get("extra_usage_is_enabled"),
            extra_usage_monthly_limit=r.get("extra_usage_monthly_limit"),
            extra_usage_used_credits=r.get("extra_usage_used_credits"),
            extra_usage_utilization=r.get("extra_usage_utilization"),
            extra_usage_currency=r.get("extra_usage_currency"),
        ))
