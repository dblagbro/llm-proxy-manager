"""v3.6.0 — Model-identity edit API.

Per-canonical-model GET + PUT for ``aliases`` / ``family`` / ``variant``.
Designed against the OpenAPI spec at
``docs/rfc/2026-05-model-identity-put-spec.md`` (locked with the
coordinator-hub team 2026-05-09).

Path: ``/api/llm/models/{model_id:path}``. The slash in ``model_id``
is allowed (canonical names like ``x-ai/grok-3`` carry a vendor
prefix).

Multi-row write semantic: aliases/family/variant are upstream-model-
identity properties, so the same canonical id served by two providers
should carry the same identity. Default PUT behavior applies the
write to ALL ``ModelCapability`` rows where the path matches the
canonical id OR any registered alias. Pass ``?provider_id=<id>`` to
scope a write to one row.

Concurrency: ETag covers the merged state across all matching rows
(see ``app/api/_etag.py``). PUT requires ``If-Match`` on the GET
ETag; mismatch → 412 with the fresh ETag in the response so the
caller can retry.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._etag import etag_for_canonical_model, parse_if_match
from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import ModelCapability
from app.routing.canonical import KNOWN_FAMILIES, derive_family

router = APIRouter(prefix="/api/llm", tags=["model-identity"])


class ModelIdentityUpdate(BaseModel):
    """PATCH-like semantics on a PUT — only fields present in the
    body are updated. The Hub UI always loads the full record from
    GET first, so this is safe (the Hub sends back what it loaded
    + the operator's edits).
    """
    aliases: Optional[list[str]] = None
    family: Optional[str] = Field(default=None, min_length=1)
    variant: Optional[str] = None


def _validate_aliases(aliases: list[str], model_id: str) -> None:
    """Hard-reject obviously broken aliases. See OpenAPI spec §4."""
    if len(aliases) > 16:
        raise HTTPException(400, f"max 16 aliases (got {len(aliases)})")
    seen: set[str] = set()
    for i, a in enumerate(aliases):
        if not isinstance(a, str):
            raise HTTPException(400, f"aliases[{i}] is not a string")
        if not a or not a.strip():
            raise HTTPException(400, f"aliases[{i}] is empty or whitespace-only")
        if any(ch.isspace() for ch in a):
            raise HTTPException(400, f"aliases[{i}] {a!r} contains whitespace")
        if len(a) < 1 or len(a) > 64:
            raise HTTPException(400, f"aliases[{i}] {a!r} length must be 1-64 chars")
        norm = a.lower()
        if norm in seen:
            raise HTTPException(400, f"aliases[{i}] {a!r} is a duplicate")
        seen.add(norm)
        # An alias matching the model_id itself is harmless (router
        # already accepts both); keep it but de-dup at write time.


async def _check_alias_collisions(
    db: AsyncSession,
    new_aliases: list[str],
    *,
    own_model_id: str,
    own_row_ids: set[int],
) -> None:
    """Reject aliases that already shadow another row's canonical_id
    or alias — would cause ambiguous routing.

    ``own_row_ids`` is the set of row primary keys we're updating
    (so we don't false-positive on a row's own existing aliases).
    """
    if not new_aliases:
        return
    new_lower = {a.lower() for a in new_aliases}
    # Skip aliases that match own_model_id — they're harmless
    # (router resolves them to this same record).
    new_lower.discard(own_model_id.lower())
    if not new_lower:
        return
    # Pull all non-own rows with non-null model_id or aliases
    rs = await db.execute(
        select(
            ModelCapability.id,
            ModelCapability.model_id,
            ModelCapability.aliases,
        ).where(ModelCapability.deleted_at.is_(None))
    )
    for row_id, mid, others in rs.all():
        if row_id in own_row_ids:
            continue
        if mid and mid.lower() in new_lower:
            raise HTTPException(
                400,
                f"alias {mid!r} collides with another model's canonical id",
            )
        for o in others or []:
            if o.lower() in new_lower:
                raise HTTPException(
                    400,
                    f"alias {o!r} collides with another model's existing alias",
                )


async def _find_matching_rows(
    db: AsyncSession,
    model_id: str,
    *,
    provider_id: Optional[str] = None,
) -> list[ModelCapability]:
    """Return all non-deleted ``ModelCapability`` rows whose
    canonical id OR any alias matches ``model_id`` (case-insensitive).
    Optionally filter by ``provider_id``.
    """
    stmt = select(ModelCapability).where(ModelCapability.deleted_at.is_(None))
    if provider_id:
        stmt = stmt.where(ModelCapability.provider_id == provider_id)
    rs = await db.execute(stmt)
    target = model_id.lower()
    matches: list[ModelCapability] = []
    for c in rs.scalars().all():
        if c.model_id and c.model_id.lower() == target:
            matches.append(c)
            continue
        for a in c.aliases or []:
            if a.lower() == target:
                matches.append(c)
                break
    return matches


def _merged_identity(rows: list[ModelCapability], model_id: str) -> dict:
    """Build the response shape from one or more matching rows.

    When multiple rows have divergent identity (rare), the
    lowest-priority provider's row wins for the merged view —
    callers wanting per-provider state pass ``?provider_id=`` on GET.
    Aliases are merged across all rows (set union).
    """
    if not rows:
        return {
            "model_id": model_id,
            "aliases": [],
            "family": None,
            "variant": None,
            "provider_count": 0,
        }
    primary = sorted(rows, key=lambda r: (r.provider_id,))[0]
    merged_aliases: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for a in r.aliases or []:
            low = a.lower()
            if low not in seen and a != primary.model_id:
                seen.add(low)
                merged_aliases.append(a)
    family = primary.model_family or derive_family(primary.model_id)
    return {
        "model_id": primary.model_id,
        "aliases": merged_aliases,
        "family": family,
        "variant": primary.model_variant,
        "provider_count": len(rows),
    }


@router.get("/models/{model_id:path}")
async def get_model_identity(
    model_id: str,
    response: Response,
    provider_id: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Read identity for one canonical model.

    Sets ``ETag`` header for downstream PUT concurrency. The ETag
    is per-node (cluster sync may briefly diverge) — see
    ``docs/lmrh-2.0-bidirectional.md`` §"Per-node ETag".
    """
    rows = await _find_matching_rows(db, model_id, provider_id=provider_id)
    if not rows:
        raise HTTPException(404, f"no model_capability matches {model_id!r}")
    body = _merged_identity(rows, model_id)
    response.headers["ETag"] = etag_for_canonical_model(rows)
    return body


@router.put("/models/{model_id:path}")
async def update_model_identity(
    model_id: str,
    body: ModelIdentityUpdate,
    response: Response,
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    provider_id: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Update aliases/family/variant for the canonical model.

    Default semantic: applies to ALL rows matching the canonical
    id or any alias. Pass ``?provider_id=<id>`` to scope to one row.

    Concurrency: ``If-Match`` ETag required. Mismatch → 412 with
    fresh ETag in the response.

    Validation: see OpenAPI spec §4. Aliases can't collide with
    another row's canonical id or alias.

    Family is soft-validated against ``KNOWN_FAMILIES``: novel
    values save successfully but emit ``X-Warning`` so the Hub UI
    can surface a yellow toast.
    """
    rows = await _find_matching_rows(db, model_id, provider_id=provider_id)
    if not rows:
        raise HTTPException(404, f"no model_capability matches {model_id!r}")
    # Concurrency check: PUT requires If-Match. The expected ETag is
    # what GET would return for the current state.
    expected = etag_for_canonical_model(rows)
    sent = parse_if_match(if_match)
    if not sent:
        raise HTTPException(
            400,
            "If-Match header required — fetch via GET first to capture the ETag",
        )
    if sent != expected:
        response.headers["ETag"] = expected
        raise HTTPException(
            412, "ETag mismatch — refresh via GET and retry",
        )

    # Validation pre-flight (don't mutate any row until all checks pass)
    if body.aliases is not None:
        _validate_aliases(body.aliases, model_id)
        own_ids = {r.id for r in rows}
        await _check_alias_collisions(
            db, body.aliases,
            own_model_id=rows[0].model_id,
            own_row_ids=own_ids,
        )

    # Apply the update to every matching row
    family_warning: Optional[str] = None
    if body.family is not None and body.family not in KNOWN_FAMILIES:
        family_warning = (
            f'family "{body.family}" is not in the known set. Saved anyway — '
            f"update KNOWN_FAMILIES if this should be canonical."
        )
    for r in rows:
        if body.aliases is not None:
            # De-dup case-insensitively, preserving operator's casing
            seen: set[str] = set()
            cleaned: list[str] = []
            for a in body.aliases:
                low = a.lower()
                if low not in seen:
                    seen.add(low)
                    cleaned.append(a)
            r.aliases = cleaned
        if body.family is not None:
            r.model_family = body.family
        if body.variant is not None:
            r.model_variant = body.variant
        # Mark as manual so cluster sync prefers operator-edited state
        r.source = "manual"
    await db.commit()
    # Re-fetch to pick up the auto-bumped updated_at, then recompute ETag
    fresh = await _find_matching_rows(db, model_id, provider_id=provider_id)
    response.headers["ETag"] = etag_for_canonical_model(fresh)
    if family_warning:
        response.headers["X-Warning"] = family_warning
    return _merged_identity(fresh, model_id)
