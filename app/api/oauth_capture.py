"""Multi-vendor OAuth passthrough capture (v2.5.0).

Each OAuth-capable CLI (claude-code, codex, gh copilot, gcloud, az, …) gets
its own capture "profile" with its own upstream host(s), secret, and
enabled flag. Profiles are rows in ``oauth_capture_profiles``; the
endpoint at ``/api/oauth-capture/{profile_name}/{path}`` forwards to that
profile's upstream and records the request+response into
``oauth_capture_log`` tagged with the profile name.

Workflow
========
1. Admin picks a preset (or goes Custom) and saves a profile. The server
   auto-generates a strong secret.
2. The UI shows a copy-paste env block with the profile-specific base
   URL + secret. User runs ``claude login`` / ``codex auth`` / whatever.
3. Every request that lands here is logged + forwarded. The UI tails the
   log via SSE.
4. Once we have enough captures, we reverse-engineer and ship a
   ``<vendor>-oauth`` provider type.

Security notes
==============
- Each profile has its own secret (required via ``?cap=<secret>`` or
  ``X-Capture-Secret`` header). Rotating one profile's secret doesn't
  affect others.
- Profiles are off by default. An enabled profile + stolen URL + leaked
  secret is still only a tightly-scoped open relay pointed at a single
  pre-configured vendor host — not an arbitrary CORS-bypass proxy.
- Captured bodies can contain authorization codes and bearer tokens.
  Treat the ``oauth_capture_log`` table as a secret store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets as _secrets
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.models.database import AsyncSessionLocal, get_db
from app.models.db import OAuthCaptureLog, OAuthCaptureProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth-capture", tags=["oauth-capture"])


# ── Known-CLI presets ───────────────────────────────────────────────────────
# `primary_host` is the single most important upstream — used when the CLI
# asks for the profile's "default" URL. `extra_hosts` lets us rewrite
# subpaths for CLIs that hit multiple vendor domains during login.


@dataclass(frozen=True)
class CapturePreset:
    key: str                   # short id stored in profile.preset
    label: str                 # UI-friendly name
    cli_hint: str              # which CLI this captures (shown in wizard)
    primary_upstream: str      # default upstream host (no trailing slash)
    extra_upstreams: tuple[str, ...] = ()
    env_var_names: tuple[str, ...] = ()   # which env vars the CLI checks
    setup_hint: str = ""


PRESETS: dict[str, CapturePreset] = {
    "claude-code": CapturePreset(
        key="claude-code",
        label="Anthropic — Claude Code CLI",
        cli_hint="`claude login` on the workstation",
        primary_upstream="https://console.anthropic.com",
        env_var_names=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_URL", "ANTHROPIC_API_URL"),
        setup_hint="Run `claude login` then `claude \"ping\"` to capture both auth and first chat.",
    ),
    "openai-codex": CapturePreset(
        key="openai-codex",
        label="OpenAI — Codex CLI / ChatGPT Plus",
        cli_hint="`codex auth` or the OpenAI CLI",
        primary_upstream="https://auth.openai.com",
        extra_upstreams=("https://api.openai.com",),
        env_var_names=("OPENAI_BASE_URL", "OPENAI_AUTH_URL"),
        setup_hint="ChatGPT Plus/Team tokens refresh ~every 1h; capture twice in quick succession.",
    ),
    "github-copilot": CapturePreset(
        key="github-copilot",
        label="GitHub Copilot",
        cli_hint="`gh copilot` or VS Code login",
        primary_upstream="https://github.com",
        extra_upstreams=("https://api.githubcopilot.com",),
        env_var_names=("GH_HOST",),
        setup_hint="Device-code flow — capture the /login/device dance end-to-end.",
    ),
    "azure-aad": CapturePreset(
        key="azure-aad",
        label="Microsoft / Azure AD (Azure OpenAI)",
        cli_hint="`az login` or `m365 login`",
        primary_upstream="https://login.microsoftonline.com",
        env_var_names=("AZURE_OPENAI_ENDPOINT",),
        setup_hint="MSAL device-code flow; tenant ID is part of the auth URL path.",
    ),
    "google-oauth": CapturePreset(
        key="google-oauth",
        label="Google — gcloud / Gemini CLI",
        cli_hint="`gcloud auth login` or `gemini auth`",
        primary_upstream="https://accounts.google.com",
        extra_upstreams=("https://oauth2.googleapis.com",),
        env_var_names=("CLOUDSDK_AUTH_AUTHORITY",),
        setup_hint="Browser PKCE flow with localhost redirect; we capture both the authorize + token exchange.",
    ),
    "xai-grok": CapturePreset(
        key="xai-grok",
        label="xAI — Grok (X Premium+)",
        cli_hint="any Grok CLI wrapper",
        primary_upstream="https://api.x.ai",
        setup_hint="TBD — no public CLI yet; capture whichever tool you use.",
    ),
    "cohere": CapturePreset(
        key="cohere",
        label="Cohere",
        cli_hint="`cohere login`",
        primary_upstream="https://dashboard.cohere.com",
        extra_upstreams=("https://api.cohere.com",),
    ),
    "custom": CapturePreset(
        key="custom",
        label="Custom / other",
        cli_hint="any CLI",
        primary_upstream="https://example.com",  # placeholder — user must edit
        setup_hint="Set the upstream URL yourself.",
    ),
}


# ── Header filtering ─────────────────────────────────────────────────────────

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-length",
    "host",
})


def _filter_req_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _filter_resp_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


# ── Preset + profile admin endpoints ────────────────────────────────────────


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

    # Strip trailing slashes so concatenation is unambiguous
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


# ── Capture log endpoints ────────────────────────────────────────────────────


@router.get("/_log")
async def list_captures(
    profile: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    stmt = select(OAuthCaptureLog).order_by(OAuthCaptureLog.id.desc()).limit(min(limit, 500))
    if profile:
        stmt = stmt.where(OAuthCaptureLog.profile_name == profile)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_serialize_log_summary(r) for r in rows]


@router.get("/_log/{log_id}")
async def get_capture(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(OAuthCaptureLog).where(OAuthCaptureLog.id == log_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Capture not found")
    return _serialize_log_full(r)


@router.get("/_log/stream/{profile_name}")
async def stream_captures(
    profile_name: str,
    _: AdminUser = Depends(require_admin),
):
    """SSE tail of captures for a given profile.

    Polls the log table every 500ms for new rows matching profile_name.
    Not fancy (no LISTEN/NOTIFY since we're on SQLite), but it's enough
    for the interactive capture wizard.
    """
    async def _tail():
        seen_max_id = 0
        yield f'event: ready\ndata: {{"profile":{json.dumps(profile_name)}}}\n\n'
        for _ in range(3600):  # cap at ~30 min so abandoned tails don't linger
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(OAuthCaptureLog)
                        .where(
                            OAuthCaptureLog.profile_name == profile_name,
                            OAuthCaptureLog.id > seen_max_id,
                        )
                        .order_by(OAuthCaptureLog.id)
                        .limit(50)
                    )
                    rows = result.scalars().all()
                for r in rows:
                    seen_max_id = r.id
                    data = _serialize_log_summary(r)
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception as exc:
                logger.warning("oauth_capture.stream_tick_failed %s", exc)
            await asyncio.sleep(0.5)
        yield 'event: end\ndata: {"reason":"timeout"}\n\n'

    return StreamingResponse(_tail(), media_type="text/event-stream")


@router.get("/_log/export/{profile_name}")
async def export_captures(
    profile_name: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """NDJSON dump of all captures for a given profile (offline reverse-eng)."""
    result = await db.execute(
        select(OAuthCaptureLog)
        .where(OAuthCaptureLog.profile_name == profile_name)
        .order_by(OAuthCaptureLog.id)
    )
    rows = result.scalars().all()

    async def _gen():
        for r in rows:
            yield (json.dumps(_serialize_log_full(r)) + "\n").encode()

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.delete("/_log")
async def clear_captures(
    profile: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Wipe captures. Pass ?profile=NAME to limit scope; no query wipes all."""
    stmt = sql_delete(OAuthCaptureLog)
    if profile:
        stmt = stmt.where(OAuthCaptureLog.profile_name == profile)
    result = await db.execute(stmt)
    await db.commit()
    return {"deleted": result.rowcount or 0, "profile": profile or "(all)"}


# ── Passthrough catch-all ────────────────────────────────────────────────────


@router.api_route(
    "/{profile_name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def capture_passthrough(profile_name: str, path: str, request: Request):
    """Forward the request to the profile's upstream and log everything."""
    # Load the profile
    async with AsyncSessionLocal() as db:
        profile = (await db.execute(
            select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == profile_name)
        )).scalar_one_or_none()

    if profile is None:
        raise HTTPException(404, f"Capture profile {profile_name!r} not found")
    if not profile.enabled:
        raise HTTPException(403, f"Capture profile {profile_name!r} is disabled")
    if not profile.upstream_urls:
        raise HTTPException(500, "Profile has no upstream URLs configured")

    # Secret check
    if profile.secret:
        provided = request.query_params.get("cap") or request.headers.get("x-capture-secret")
        if provided != profile.secret:
            raise HTTPException(403, "Capture secret required")
        query = {k: v for k, v in request.query_params.multi_items() if k != "cap"}
        query_string = "&".join(f"{k}={v}" for k, v in query)
    else:
        query_string = request.url.query

    session_tag = request.headers.get("x-capture-session") or None

    # Pick upstream: first URL wins for now. Future: path-prefix routing so
    # OAuth URLs go to auth host and API calls go to api host.
    upstream = profile.upstream_urls[0]
    target = f"{upstream}/{path}"
    if query_string:
        target = f"{target}?{query_string}"

    body_bytes = await request.body()
    req_headers_dict = dict(request.headers.items())
    forward_headers = _filter_req_headers(req_headers_dict)

    t0 = time.monotonic()
    resp_status: Optional[int] = None
    resp_headers_dict: dict = {}
    resp_body_bytes: bytes = b""
    error_msg: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            r = await client.request(
                request.method,
                target,
                content=body_bytes if body_bytes else None,
                headers=forward_headers,
            )
            resp_status = r.status_code
            resp_headers_dict = dict(r.headers.items())
            resp_body_bytes = r.content
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.warning("oauth_capture.upstream_failed %s", error_msg)

    latency_ms = (time.monotonic() - t0) * 1000.0

    # Persist the capture
    try:
        async with AsyncSessionLocal() as db:
            log = OAuthCaptureLog(
                profile_name=profile_name,
                capture_session=session_tag,
                method=request.method,
                path=path,
                upstream_url=target,
                req_headers=req_headers_dict,
                req_body=_safe_text(body_bytes),
                req_query=query_string,
                resp_status=resp_status,
                resp_headers=resp_headers_dict,
                resp_body=_safe_text(resp_body_bytes),
                latency_ms=latency_ms,
                error=error_msg,
            )
            db.add(log)
            await db.commit()
    except Exception as exc:
        logger.error("oauth_capture.log_insert_failed %s", exc)

    if error_msg is not None:
        return JSONResponse(
            {"error": "upstream_unreachable", "detail": error_msg},
            status_code=502,
        )

    return Response(
        content=resp_body_bytes,
        status_code=resp_status or 502,
        headers=_filter_resp_headers(resp_headers_dict),
        media_type=resp_headers_dict.get("content-type"),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_profile_or_404(db: AsyncSession, name: str) -> OAuthCaptureProfile:
    result = await db.execute(select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == name))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, f"Profile {name!r} not found")
    return profile


def _serialize_profile(p: OAuthCaptureProfile, include_secret: bool = False) -> dict:
    out = {
        "name": p.name,
        "preset": p.preset,
        "upstream_urls": list(p.upstream_urls or []),
        "enabled": bool(p.enabled),
        "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "has_secret": bool(p.secret),
    }
    if include_secret:
        out["secret"] = p.secret
    return out


def _serialize_log_summary(r: OAuthCaptureLog) -> dict:
    return {
        "id": r.id,
        "profile_name": r.profile_name,
        "method": r.method,
        "path": r.path,
        "upstream_url": r.upstream_url,
        "resp_status": r.resp_status,
        "latency_ms": r.latency_ms,
        "error": r.error,
        "req_body_preview": (r.req_body or "")[:200],
        "resp_body_preview": (r.resp_body or "")[:200],
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _serialize_log_full(r: OAuthCaptureLog) -> dict:
    return {
        **_serialize_log_summary(r),
        "capture_session": r.capture_session,
        "req_headers": r.req_headers,
        "req_body": r.req_body,
        "req_query": r.req_query,
        "resp_headers": r.resp_headers,
        "resp_body": r.resp_body,
    }


def _safe_text(raw: Optional[bytes]) -> Optional[str]:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        return "[binary:" + base64.b64encode(raw[:4096]).decode() + "]"
