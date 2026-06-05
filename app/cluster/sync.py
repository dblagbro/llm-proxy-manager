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
    """Merge incoming user/key/provider/settings data from a peer (insert-if-missing strategy).

    v5.0.5: commit between major sub-sections to keep the SQLite write
    lock held in short bursts instead of one 10-20s span. Pre-v5.0.5 the
    receiver wrapped 10+ tables × thousands of rows × row-by-row dedup
    in a single transaction; while the lock was held, every other writer
    on the node (per-request metrics rollup, auth lookups, allowed_paths
    middleware) hit SQLite's busy_timeout and threw
    "database is locked". The fix preserves correctness — each sub-
    section is independent (no FK that requires cross-section
    atomicity for the merge to make sense) — while bounding each lock
    hold to one table's worth of writes.

    Helper closure ``_section_commit`` keeps the existing failure
    semantics: a per-section commit failure logs and continues to the
    next section (the prior failure pattern was "one section fails →
    everything rolls back"; we deliberately accept partial sync on
    error in exchange for short locks).
    """
    async def _section_commit(label: str) -> None:
        try:
            await db.commit()
        except Exception as exc:
            logger.warning(
                "cluster_sync.section_commit_failed label=%s err=%s",
                label, exc,
            )
            try:
                await db.rollback()
            except Exception:
                pass

    # v5.0.22 — users merge now respects soft-delete tombstones +
    # LWW (BUG-070). Pre-fix this was insert-if-missing which made
    # peer pushes resurrect locally-deleted users. The handler:
    #   1. If incoming row is tombstoned (deleted_at set), propagate
    #      the tombstone to local — set local deleted_at + role/hash
    #      if absent.
    #   2. If local row is missing AND incoming is alive, insert it.
    #   3. If both exist, LWW on last_user_edit_at: peer wins iff
    #      peer's edit-stamp is strictly newer than local.
    # Lookup by id (canonical) AND by username (collision recovery
    # for cases where peers disagree on id but agree on username).
    from datetime import datetime as _dt
    def _parse_iso(s):
        if not s: return None
        if isinstance(s, _dt): return s
        try: return _dt.fromisoformat(s.replace("Z", ""))
        except Exception: return None
    for u_data in payload.get("users", []):
        # Prefer id-based lookup; fall back to username if no row by id.
        rs = await db.execute(select(User).where(User.id == u_data["id"]))
        existing = rs.scalar_one_or_none()
        if existing is None:
            rs = await db.execute(
                select(User).where(User.username == u_data["username"])
            )
            existing = rs.scalar_one_or_none()
        incoming_edit = u_data.get("last_user_edit_at") or 0.0
        incoming_deleted = _parse_iso(u_data.get("deleted_at"))
        if existing is None:
            # Only insert if peer's row is alive; if it's already
            # tombstoned, there's nothing to merge.
            if incoming_deleted is None:
                db.add(User(
                    id=u_data["id"],
                    username=u_data["username"],
                    password_hash=u_data["password_hash"],
                    role=u_data.get("role", "user"),
                    last_user_edit_at=incoming_edit or None,
                ))
        else:
            local_edit = existing.last_user_edit_at or 0.0
            if incoming_edit > local_edit:
                if incoming_deleted is not None:
                    existing.deleted_at = incoming_deleted
                else:
                    existing.deleted_at = None  # peer restored
                    existing.password_hash = u_data["password_hash"]
                    existing.role = u_data.get("role", existing.role)
                existing.last_user_edit_at = incoming_edit
    await _section_commit("users")

    source_node = payload.get("source_node", "unknown")

    # v5.0.10 — api_keys merge extracted to ``sync_handlers._apply_api_keys``.
    # Behavior identical; the helper returns the per-key peer_cost map.
    peer_costs = await _apply_api_keys(db, payload.get("api_keys", []))

    _peer_key_costs[source_node] = peer_costs

    # v5.0.5 — commit api_keys before starting providers so the write
    # lock is released between sections (see apply_sync docstring).
    await _section_commit("api_keys")

    # v5.0.10 — providers merge extracted to ``sync_handlers._apply_providers``.
    # Behavior identical; ``register_provider`` runs inside the helper.
    await _apply_providers(db, payload.get("providers", []))

    await _section_commit("providers")

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

    # v5.0.5 — commit settings before runs.
    await _section_commit("settings")

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

    # v5.0.5 — commit runs before lmrh.
    await _section_commit("runs")

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
                peer_updated = _parse_iso_naive_utc(c_data.get("updated_at"))
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

    # v5.0.5 — commit lmrh / runs / model_capabilities / model_aliases /
    # oauth_capture_profiles together. These were authored under one
    # transaction historically; keeping them grouped preserves the
    # "all-or-nothing for slow-growing tables" property while still
    # releasing the lock between this batch and the bigger ones below.
    await _section_commit("lmrh+runs+capabilities+aliases+oauth")

    # v3.7.15 — BUG-016: merge the three v3.7.x tables. LWW by
    # added_at / captured_at. Tracks whether the block-list mutated
    # so we can invalidate the middleware cache after commit
    # (BUG-018 fix).
    blocked_ips_changed = await _apply_blocked_ips(db, payload.get("blocked_ips", []))
    await _section_commit("blocked_ips")
    await _apply_ai_reviews(db, payload.get("api_key_ai_reviews", []))
    await _section_commit("ai_reviews")
    await _apply_external_usage_snapshots(db, payload.get("external_usage_snapshots", []))
    await _section_commit("external_usage_snapshots")
    # v4.4 M-2 — per-node bridge auth state (Path A foundation).
    await _apply_provider_node_auth_states(db, payload.get("provider_node_auth_states", []))
    await _section_commit("provider_node_auth_states")
    # v5.0.5 — provider_ai_reviews is the largest+slowest section (5K+ rows
    # at scale); committing immediately after it is what halts the
    # 12+s write-lock-thrash that triggered today's incident.
    await _apply_provider_ai_reviews(db, payload.get("provider_ai_reviews", []))
    await _section_commit("provider_ai_reviews")
    # v3.8.7 (#267) Phase 2 — caller memory king-store
    await _apply_caller_memory(db, payload.get("caller_memory", []))
    await _apply_caller_memory_markers(db, payload.get("caller_memory_markers", []))
    await _section_commit("caller_memory")
    # v5.0.0 — compliance audit trail. Both handlers are append-only;
    # they dedupe on the unique business key (audit_id / policy_change_id)
    # and skip duplicates so re-applying the last 1000 rows on every
    # sync round is idempotent.
    await _apply_compliance_events(db, payload.get("compliance_events", []))
    await _apply_compliance_policy_changes(db, payload.get("compliance_policy_changes", []))
    # v5.0.18 — UI-configurable cluster peer list. Tombstone-aware LWW
    # merge; same shape as api_keys.
    await _apply_cluster_peers(db, payload.get("cluster_peers", []))
    # Final tail commit covers compliance + cluster_peers + any leftover state.
    await _section_commit("compliance+cluster_peers+tail")

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



# v3.9.8 (P5 refactor) — per-table handlers live in sync_handlers.py.
# Re-import here so this module's public surface is unchanged.
from app.cluster.sync_handlers import (  # noqa: E402,F401
    _apply_blocked_ips,
    _apply_ai_reviews,
    _apply_provider_ai_reviews,
    _apply_caller_memory,
    _apply_caller_memory_markers,
    _apply_external_usage_snapshots,
    _apply_provider_node_auth_states,
    # v5.0.0 — compliance audit trail
    _apply_compliance_events,
    _apply_compliance_policy_changes,
    # v5.0.10 — extracted from inline apply_sync
    _apply_api_keys,
    _apply_providers,
    _parse_iso_naive_utc,
    # v5.0.18 — UI-configurable cluster peer list
    _apply_cluster_peers,
)
