"""Profile CRUD endpoints for the OAuth capture package."""
from __future__ import annotations

import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.models.database import get_db
from app.models.db import OAuthCaptureLog, OAuthCaptureProfile

from app.api.oauth_capture.presets import PRESETS
from app.api.oauth_capture.serializers import _serialize_profile

router = APIRouter(prefix="/api/oauth-capture", tags=["oauth-capture"])


@router.get("/_presets")
async def list_presets(_: AdminUser = Depends(require_admin)):
    """UI-facing preset catalog used by the 'Add OAuth capture' wizard."""
    return [
        {
            "key": p.key,
            "label": p.label,
            "cli_hint": p.cli_hint,
            "primary_upstream": p.primary_upstream,
            "extra_upstreams": list(p.extra_upstreams),
            "env_var_names": list(p.env_var_names),
            "setup_hint": p.setup_hint,
        }
        for p in PRESETS.values()
    ]


@router.get("/_profiles")
async def list_profiles(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(OAuthCaptureProfile).order_by(OAuthCaptureProfile.created_at))
    profiles = result.scalars().all()
    return [_serialize_profile(p) for p in profiles]


@router.post("/_profiles")
async def create_profile(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Create a capture profile. Body: {name, preset?, upstream_urls?, notes?}.

    When `preset` is supplied, upstream_urls defaults to the preset's hosts
    (unless the caller provides its own). Secret is auto-generated.
    """
    name = (body.get("name") or "").strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Profile name must be alphanumeric (dashes/underscores allowed)")

    existing = await db.execute(select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Profile {name!r} already exists")

    preset_key = body.get("preset")
    preset = PRESETS.get(preset_key) if preset_key else None

    upstream_urls = body.get("upstream_urls")
    if not upstream_urls:
        if preset is None:
            raise HTTPException(400, "Provide upstream_urls or a preset")
        upstream_urls = [preset.primary_upstream, *preset.extra_upstreams]

    upstream_urls = [u.rstrip("/") for u in upstream_urls if u]
    if not upstream_urls:
        raise HTTPException(400, "At least one upstream URL is required")

    profile = OAuthCaptureProfile(
        name=name,
        preset=preset.key if preset else None,
        upstream_urls=upstream_urls,
        secret=_secrets.token_urlsafe(32),
        enabled=bool(body.get("enabled", False)),
        notes=body.get("notes"),
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _serialize_profile(profile, include_secret=True)


@router.patch("/_profiles/{name}")
async def update_profile(
    name: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    profile = await _get_profile_or_404(db, name)
    if "upstream_urls" in body and body["upstream_urls"] is not None:
        urls = [u.rstrip("/") for u in body["upstream_urls"] if u]
        if not urls:
            raise HTTPException(400, "At least one upstream URL is required")
        profile.upstream_urls = urls
    if "enabled" in body:
        profile.enabled = bool(body["enabled"])
    if "notes" in body:
        profile.notes = body["notes"]
    if body.get("rotate_secret"):
        profile.secret = _secrets.token_urlsafe(32)
    await db.commit()
    await db.refresh(profile)
    return _serialize_profile(profile, include_secret=bool(body.get("rotate_secret")))


@router.get("/_profiles/{name}/secret")
async def reveal_secret(
    name: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Reveal the capture secret once so the admin can paste it into the CLI wrapper."""
    profile = await _get_profile_or_404(db, name)
    return {"name": profile.name, "secret": profile.secret}


@router.delete("/_profiles/{name}")
async def delete_profile(
    name: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    profile = await _get_profile_or_404(db, name)
    # Clear associated logs so nothing dangling references a missing profile
    await db.execute(sql_delete(OAuthCaptureLog).where(OAuthCaptureLog.profile_name == name))
    await db.delete(profile)
    await db.commit()
    return {"ok": True}


async def _get_profile_or_404(db: AsyncSession, name: str) -> OAuthCaptureProfile:
    result = await db.execute(select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == name))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, f"Profile {name!r} not found")
    return profile
