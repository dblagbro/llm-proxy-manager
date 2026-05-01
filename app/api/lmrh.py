"""
LMRH self-extension protocol endpoints (v3.0.25).

Public spec: served at GET /lmrh.md (already shipped v3.0.23).

This module implements the runtime extension protocol the spec describes:

  POST /lmrh/register       — auth-required, collision-resolved registration
                              of a new LMRH dim. Returns the canonical name
                              the proxy will accept (with a -2/-3 suffix if
                              the requested name was taken).

  POST /lmrh/propose         — auth-required, free-form proposal for an
                              operator-reviewed dim addition (not auto-
                              accepted; queued for review).

  GET  /lmrh/registry        — public, lists all registered dims.

  GET  /lmrh/registry/{name} — public, details for one dim.

  GET  /lmrh/proposals       — admin-only, lists pending proposals.

Cluster sync replicates the ``lmrh_dims`` and ``lmrh_proposals`` tables
between peer nodes (see app/cluster/sync.py and manager.py — same
last-write-wins-by-registered_at semantics as other state).

Warning header behavior (implemented in middleware, not here): when a
client sends an unrecognized LMRH dim, the response carries
``X-LMRH-Warnings: unknown-dim:NAME register-at:/lmrh/register``.
Backwards-compatible: old clients are not broken; new clients see the
hint and can register the dim or fix their code.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.auth.keys import resolve_api_key_dep
from app.config import settings
from app.models.database import get_db
from app.models.db import LmrhDim, LmrhProposal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lmrh", tags=["lmrh"])

_AUTH = resolve_api_key_dep()

# RFC 8941–ish identifier shape for LMRH dim names — lowercase, digits,
# hyphens. Length-bounded so we don't store a megabyte of garbage.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def _norm(name: str) -> str:
    return name.strip().lower()


def _builtin_dim_names() -> set[str]:
    """Built-in dims defined in code (app/routing/lmrh/score.py case branches).
    These cannot be re-registered or shadowed by user dims."""
    return {
        "task", "safety-min", "safety-max", "refusal-rate",
        "latency", "cost", "region", "context-length", "modality",
        "max-ttft", "max-cost-per-1k",
        "effort", "cascade", "hedge", "tenant", "freshness",
        "exclude", "provider-hint",
    }


# ── Pydantic shapes ─────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    name: str = Field(..., description="Proposed dim name; lowercase, [a-z0-9_-]")
    semantics: str = Field(..., description="One-paragraph plain-English meaning")
    value_type: Optional[str] = Field(
        None, description='"string", "int", "float", or "enum:a,b,c"',
    )
    kind: str = Field("advisory", description='"hard" | "soft" | "advisory"')
    examples: list[str] = Field(default_factory=list)
    owner_app: Optional[str] = Field(
        None, description="Free-form identifier for traceability (e.g. 'paperless-ai-analyzer')",
    )


class RegisterResponse(BaseModel):
    accepted: bool
    canonical_name: str
    requested_name: str
    suffix_applied: bool
    note: Optional[str] = None


class ProposalRequest(BaseModel):
    name: str
    rationale: str
    owner_app: Optional[str] = None


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/register", response_model=RegisterResponse)
async def register_dim(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    key_record=Depends(_AUTH),
):
    """Register a new LMRH dim with collision-resolution.

    If the requested name is free → registered as-is, returns canonical=name.
    If taken (built-in OR already-registered) → returns the lowest-numbered
    suffix variant that's free (foo → foo-2 → foo-3 ...) and registers THAT.
    The caller is expected to use the returned ``canonical_name`` going
    forward.

    The protocol is intentionally optimistic: callers can call this once
    on first use of a new dim, cache the canonical_name locally, and not
    call again. Re-registering an already-owned name (same owner_app +
    same key) is idempotent — returns the existing entry.
    """
    requested = _norm(body.name)
    if not _NAME_RE.match(requested):
        raise HTTPException(
            400,
            "Invalid dim name. Must match ^[a-z][a-z0-9_-]{1,63}$ "
            "(lowercase, start with a letter, hyphens/underscores allowed).",
        )
    builtins = _builtin_dim_names()
    if requested in builtins:
        # Built-in; cannot be re-registered. Return the canonical built-in name
        # so the caller knows it already exists.
        return RegisterResponse(
            accepted=False, canonical_name=requested, requested_name=requested,
            suffix_applied=False,
            note="This dim is built into the proxy and is already canonical.",
        )

    # Idempotent re-register: same name, same owner+key → return existing entry
    existing = (await db.execute(
        select(LmrhDim).where(LmrhDim.name == requested)
    )).scalar_one_or_none()
    if existing is not None:
        same_owner = (
            (body.owner_app or "") == (existing.owner_app or "")
            and (key_record.id) == (existing.owner_key_id or "")
        )
        if same_owner:
            return RegisterResponse(
                accepted=True, canonical_name=requested, requested_name=requested,
                suffix_applied=False, note="Already registered to you (idempotent).",
            )
        # Collision — find next free suffixed name (foo-2, foo-3, ...)
        canonical = await _find_free_suffix(db, requested)
        suffix_applied = True
        note = (
            f"Name {requested!r} is owned by another app — assigned "
            f"{canonical!r}. Use that going forward."
        )
    else:
        canonical = requested
        suffix_applied = False
        note = None

    dim = LmrhDim(
        name=canonical,
        owner_app=body.owner_app,
        owner_key_id=key_record.id,
        semantics=body.semantics,
        value_type=body.value_type,
        kind=body.kind if body.kind in ("hard", "soft", "advisory") else "advisory",
        examples=body.examples or [],
        requested_name=requested,
        registered_at=time.time(),
        registered_by_node=settings.cluster_node_id,
    )
    db.add(dim)
    await db.commit()
    logger.warning(
        "lmrh.dim_registered name=%r requested=%r owner_app=%r owner_key=%s",
        canonical, requested, body.owner_app, key_record.id,
    )
    return RegisterResponse(
        accepted=True, canonical_name=canonical, requested_name=requested,
        suffix_applied=suffix_applied, note=note,
    )


@router.post("/propose")
async def propose_dim(
    body: ProposalRequest,
    db: AsyncSession = Depends(get_db),
    key_record=Depends(_AUTH),
):
    """Submit a free-form proposal for operator review. Use this when the
    new dim should become an OFFICIAL part of the LMRH spec (vs auto-
    registered via /register, which is for app-specific extensions).
    """
    p = LmrhProposal(
        proposed_name=_norm(body.name),
        rationale=body.rationale,
        proposer_app=body.owner_app,
        proposer_key_id=key_record.id,
        proposed_at=time.time(),
        status="pending",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    logger.warning(
        "lmrh.proposal_queued id=%d name=%r app=%r key=%s",
        p.id, p.proposed_name, body.owner_app, key_record.id,
    )
    return {"id": p.id, "status": "pending", "proposed_name": p.proposed_name}


@router.get("/registry")
async def list_registry(db: AsyncSession = Depends(get_db)):
    """Public discovery endpoint. Lists ALL dims (built-in + registered)
    so clients can self-document without hitting the spec markdown."""
    rows = (await db.execute(
        select(LmrhDim).order_by(LmrhDim.registered_at.asc())
    )).scalars().all()
    builtins = [
        {"name": n, "kind": "builtin",
         "semantics": "Defined by the LMRH 1.0 spec — see /lmrh.md."}
        for n in sorted(_builtin_dim_names())
    ]
    registered = [
        {"name": d.name, "kind": d.kind,
         "owner_app": d.owner_app,
         "value_type": d.value_type,
         "semantics": d.semantics,
         "examples": d.examples or [],
         "registered_at": d.registered_at}
        for d in rows
    ]
    return {
        "version": "lmrh/1.1",
        "builtins": builtins,
        "registered": registered,
        "register_endpoint": "/lmrh/register",
        "propose_endpoint": "/lmrh/propose",
        "spec_url": "/lmrh.md",
    }


@router.get("/registry/{name}")
async def get_dim(name: str, db: AsyncSession = Depends(get_db)):
    """Public lookup of a single dim's details (built-in or registered)."""
    nm = _norm(name)
    if nm in _builtin_dim_names():
        return {
            "name": nm, "kind": "builtin",
            "semantics": "See /lmrh.md for full normative semantics.",
        }
    d = (await db.execute(
        select(LmrhDim).where(LmrhDim.name == nm)
    )).scalar_one_or_none()
    if d is None:
        raise HTTPException(404, f"Dim {nm!r} not registered.")
    return {
        "name": d.name, "kind": d.kind,
        "owner_app": d.owner_app,
        "value_type": d.value_type,
        "semantics": d.semantics,
        "examples": d.examples or [],
        "registered_at": d.registered_at,
        "registered_by_node": d.registered_by_node,
    }


@router.get("/proposals")
async def list_proposals(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Admin-only review queue."""
    rows = (await db.execute(
        select(LmrhProposal).order_by(LmrhProposal.proposed_at.desc())
    )).scalars().all()
    return [
        {"id": p.id, "name": p.proposed_name, "rationale": p.rationale,
         "owner_app": p.proposer_app, "status": p.status,
         "review_note": p.review_note,
         "proposed_at": p.proposed_at}
        for p in rows
    ]


# ── Internal helpers ────────────────────────────────────────────────────────


async def _find_free_suffix(db: AsyncSession, base: str) -> str:
    """Find the lowest-numbered suffix (-2, -3, ...) that is free across
    BOTH built-ins AND the registered set. Caps at -99 to bound search.
    """
    builtins = _builtin_dim_names()
    for n in range(2, 100):
        candidate = f"{base}-{n}"
        if candidate in builtins:
            continue
        existing = (await db.execute(
            select(LmrhDim.name).where(LmrhDim.name == candidate)
        )).first()
        if existing is None:
            return candidate
    raise HTTPException(
        409,
        f"Couldn't find a free suffix for {base!r} after 100 attempts. "
        "Pick a different base name.",
    )


async def known_dim_names(db: AsyncSession) -> set[str]:
    """Returns the union of built-in and registered dim names. Used by
    the unknown-dim warning middleware."""
    rows = (await db.execute(select(LmrhDim.name))).scalars().all()
    return _builtin_dim_names() | set(rows)
