"""Compliance enforcement orchestration for the chat request handlers.

Extracted 2026-06-04 from `app/api/messages.py` and `app/api/completions.py`,
where the same four orchestration blocks lived inline as near-verbatim
mirrors. Every v5.0.x patch (v5.0.1 / v5.0.2 / v5.0.4 / v5.0.6 / v5.0.7)
touched both files in lockstep; centralizing here drops the
mirror-edit burden for the inevitable v5.0.9+ compliance patches.

The four extracted orchestration points:

1. **UA pre-check** — `raise_if_banned_client_ua()` fires BEFORE any
   provider routing. If the caller's `User-Agent` matches a banned
   client product pattern AND that company is in the key's effective
   blocklist, write the audit row and raise HTTP 451.

2. **No-substitute → 503 conversion** —
   `raise_for_no_substitute_exception()` catches both
   `ComplianceNoLocalProviderError` (subclass; coordinator-local
   without a self-hosted provider; reason
   `no-compliant-local-provider`) and `ComplianceNoSubstituteError`
   (every candidate filtered out; reason
   `no-compliant-provider-available`), writes the right audit row +
   raises HTTPException(503) with the right headers.

3. **Substitution disclosure on 200** —
   `emit_substitution_disclosure_for_route()` runs after the router
   picks a provider. If `route.compliance_substituted` is True it
   writes the audit row + builds the seven `X-Compliance-*` headers +
   the SSE prelude payload + the wants-prelude flag. Returns a single
   tuple the caller merges into the response.

4. **Upstream-error 502 follow-up** —
   `disclosure_headers_for_upstream_error()` returns the
   X-Compliance-* headers + writes the follow-up audit row when the
   substituted provider's dispatch failed (so the caller knows which
   provider was tried under what policy even on 502).

Each function preserves the v5.0.6 invariant that the audit row's
`requested_model` is the caller's ORIGINAL model name (captured at the
top of the handler as `_orig_request_model`). Callers MUST pass that
captured value in — the helpers never read `body["model"]` themselves
because the v3.0.36 cross-family-fallback path has already mutated it
by the time we get here.

This module is import-cheap. No FastAPI startup-time side effects.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import (
    ComplianceNoLocalProviderError,
    ComplianceNoSubstituteError,
    build_disclosure_payload,
    compliance_headers,
    detect_client_company,
    emit_event,
    generate_audit_id,
    get_effective_blocklist,
    model_family_to_company,
    refusal_headers_no_local,
    refusal_headers_no_substitute,
    refusal_headers_ua,
    wants_sse_prelude,
)


logger = logging.getLogger(__name__)


def _coordinator_identity_headers(request: Request) -> Dict[str, Any]:
    """Pull the four `X-Coordinator-*` identity headers for audit
    correlation (CADC decision 33). Returns a plain dict with None for
    absent headers — emit_event serializes to JSON downstream."""
    return {
        "x_coordinator_client": request.headers.get("x-coordinator-client"),
        "x_coordinator_profile": request.headers.get("x-coordinator-profile"),
        "x_coordinator_client_version": request.headers.get(
            "x-coordinator-client-version"
        ),
        "x_coordinator_upstream_cli": request.headers.get(
            "x-coordinator-upstream-cli"
        ),
    }


async def raise_if_banned_client_ua(
    request: Request,
    db: AsyncSession,
    key_record: Any,
) -> None:
    """v5.0.0 decision 16 + 22 — refuse banned client products with 451.

    Fires BEFORE any provider routing so banned UAs short-circuit even
    when the requested model would be allowed. Writes a
    `client_product_refusal` audit row when blocked. No-op when the
    UA doesn't match a banned product OR the matched company isn't in
    this key's blocklist OR `settings.compliance_ua_block_enabled` is
    False.

    Caller pattern:
        await raise_if_banned_client_ua(request, db, key_record)

    Raises HTTPException(451) with the seven CADC-spec refusal headers
    + a structured `compliance_block` error body if the request matches.
    """
    from app.config import settings as _settings

    if not getattr(_settings, "compliance_ua_block_enabled", True):
        return

    ua = request.headers.get("user-agent", "")
    detection = detect_client_company(ua)
    if not detection:
        return

    company, pattern, product = detection
    blocklist = await get_effective_blocklist(db, key_record.id)
    if company not in blocklist:
        return

    audit_id = generate_audit_id()
    await emit_event(
        db,
        audit_id=audit_id,
        api_key_id=key_record.id,
        event_type="client_product_refusal",
        reason_code="client-product-banned",
        http_status=451,
        client_user_agent=ua,
        matched_pattern=pattern,
        blocked_company=company,
        client_identity=_coordinator_identity_headers(request),
        commit=True,
    )
    raise HTTPException(
        status_code=451,
        detail={"error": {
            "type": "compliance_block",
            "code": "client-product-banned",
            "matched_product": product,
            "matched_company": company,
            "matched_pattern": pattern,
            "audit_id": audit_id,
            "reason": (
                f"This API key's compliance policy prohibits clients "
                f"identified as products of {company.title()}. Migrate "
                f"to a non-{company.title()} client to continue."
            ),
        }},
        headers=refusal_headers_ua(
            matched_product=product,
            matched_company=company,
            audit_id=audit_id,
        ),
    )


async def raise_if_llm_emergency_stopped(
    db: AsyncSession,
    key_record: Any,
    *,
    endpoint: str,
    requested_model: Optional[str] = None,
) -> None:
    """v5.2.0 / Batch V1 — global LLM kill-switch.

    When ``compliance.llm_emergency_stop`` is True the operator has
    halted ALL upstream dispatch. This helper short-circuits the
    request with HTTP 503 + an audit row, BEFORE provider selection.

    Distinct from the v5.1.0 ``activity_logging_enabled`` toggle: that
    one suppresses log writes; this one refuses LLM calls. They are
    composed orthogonally — an operator can have logging OFF and
    routing OFF independently.

    The stop is unconditional — it does NOT consult the per-key
    blocklist, the requested model, or the UA. The audit row still
    records the api_key_id and (if known) ``requested_model`` so the
    operator can reconstruct who was trying what when the switch was
    engaged.

    Caller pattern (after raise_if_banned_client_ua):
        await raise_if_llm_emergency_stopped(
            db, key_record, endpoint="messages",
        )

    Raises HTTPException(503) with ``llm-emergency-stop`` reason_code.
    """
    from app.monitoring.llm_emergency_stop import is_llm_stopped, REASON_CODE

    if not await is_llm_stopped(db):
        return

    audit_id = generate_audit_id()
    await emit_event(
        db,
        audit_id=audit_id,
        api_key_id=key_record.id,
        event_type="llm_emergency_stop",
        reason_code=REASON_CODE,
        http_status=503,
        requested_model=requested_model,
        commit=True,
    )
    raise HTTPException(
        status_code=503,
        detail={"error": {
            "type": "llm_emergency_stop",
            "code": REASON_CODE,
            "audit_id": audit_id,
            "endpoint": endpoint,
            "reason": (
                "LLM routing is halted by operator (emergency stop "
                "engaged). All upstream dispatch is refused until an "
                "administrator disengages the stop. This is a "
                "fleet-wide compliance refusal — retrying will not "
                "help. Contact your administrator."
            ),
        }},
        headers={
            "X-Compliance-Refused": "llm-emergency-stop",
            "X-Compliance-Audit-Id": audit_id,
            "Retry-After": "60",
        },
    )


async def raise_for_no_substitute_exception(
    exc: ComplianceNoSubstituteError,
    *,
    request: Request,
    db: AsyncSession,
    key_record: Any,
    orig_request_model: Optional[str],
) -> None:
    """Convert a `ComplianceNoSubstituteError` (or its
    `ComplianceNoLocalProviderError` subclass) into the appropriate
    HTTP 503 response + audit row.

    Caller pattern:
        try:
            route = await select_provider_with_503(...)
        except ComplianceNoSubstituteError as exc:
            await raise_for_no_substitute_exception(
                exc, request=request, db=db, key_record=key_record,
                orig_request_model=_orig_request_model,
            )

    The `except` catches both error types because
    `ComplianceNoLocalProviderError` is a subclass; this helper then
    branches internally.

    Always raises — never returns. The return type annotation is
    `None` only because Python doesn't have NoReturn-as-suffix yet.
    """
    audit_id = generate_audit_id()
    ua = request.headers.get("user-agent", "")

    if isinstance(exc, ComplianceNoLocalProviderError):
        await emit_event(
            db,
            audit_id=audit_id,
            api_key_id=key_record.id,
            event_type="compliance_no_local_provider",
            reason_code="no-compliant-local-provider",
            http_status=503,
            requested_model=orig_request_model,
            client_user_agent=ua,
            commit=True,
        )
        raise HTTPException(
            status_code=503,
            detail={"error": {
                "type": "compliance_no_local_provider",
                "audit_id": audit_id,
                "message": (
                    "Coordinator-local was requested but no self-hosted "
                    "provider is configured on this deployment. Operator "
                    "action: enable a self-hosted provider (ollama / "
                    "vllm / llamacpp / lmstudio / localai), or call a "
                    "non-local logical alias (coordinator-code, "
                    "coordinator-fast, coordinator-reasoning)."
                ),
            }},
            headers=refusal_headers_no_local(audit_id=audit_id),
        )

    blocked_company = model_family_to_company(orig_request_model)
    await emit_event(
        db,
        audit_id=audit_id,
        api_key_id=key_record.id,
        event_type="compliance_no_substitute",
        reason_code="no-compliant-provider-available",
        http_status=503,
        requested_model=orig_request_model,
        blocked_company=blocked_company,
        client_user_agent=ua,
        commit=True,
    )
    raise HTTPException(
        status_code=503,
        detail={"error": {
            "type": "compliance_no_substitute",
            "audit_id": audit_id,
            "message": (
                "No compliance-compatible provider available for this "
                "request."
            ),
        }},
        headers=refusal_headers_no_substitute(audit_id=audit_id),
    )


async def emit_substitution_disclosure_for_route(
    request: Request,
    db: AsyncSession,
    route: Any,
    key_record: Any,
    orig_request_model: Optional[str],
) -> Tuple[Dict[str, str], Optional[Dict[str, Any]], bool]:
    """Emit the substitution audit row + build the disclosure surfaces
    when `route.compliance_substituted` is True. v5.0.0 decision
    8 + 15 + 23.

    Returns `(resp_headers_to_merge, sse_disclosure_payload,
    wants_prelude)`. When the route wasn't substituted, returns
    `({}, None, False)` so callers can unconditionally unpack +
    merge.

    `orig_request_model` MUST be the caller's original model string
    (captured at the top of the handler before the v3.0.36
    cross-family-fallback body rewrite). Pre-v5.0.6 this read
    `body.get("model")` and produced mislabeled audit rows.
    """
    if not getattr(route, "compliance_substituted", False):
        return {}, None, False

    audit_id = generate_audit_id()
    served_model = route.litellm_model or orig_request_model
    disclosure = build_disclosure_payload(
        blocked_company=route.compliance_blocked_company,
        requested_model=orig_request_model or "",
        served_model=served_model or "",
        served_company=route.compliance_served_company,
        served_provider_id=route.provider.id,
        audit_id=audit_id,
    )
    headers = compliance_headers(
        blocked_company=route.compliance_blocked_company,
        requested_model=orig_request_model or "",
        served_model=served_model or "",
        served_company=route.compliance_served_company,
        served_provider_id=route.provider.id,
        audit_id=audit_id,
    )
    await emit_event(
        db,
        audit_id=audit_id,
        api_key_id=key_record.id,
        event_type="model_substitution",
        reason_code=(
            f"api-key-policy:blocked-company:"
            f"{route.compliance_blocked_company}"
        ),
        http_status=200,
        requested_model=orig_request_model,
        served_model=served_model,
        served_provider_id=route.provider.id,
        blocked_company=route.compliance_blocked_company,
        client_user_agent=request.headers.get("user-agent", ""),
    )
    return headers, disclosure, wants_sse_prelude(request.headers)


async def disclosure_headers_for_upstream_error(
    request: Request,
    db: AsyncSession,
    route: Any,
    key_record: Any,
    orig_request_model: Optional[str],
    status_code: int,
) -> Dict[str, str]:
    """v5.0.1 — when dispatch fails (502 / 400 from upstream) but the
    route was a compliance substitution, the caller still deserves the
    disclosure headers (substitution decision was real; the substituted
    provider is the one that failed). Writes a follow-up audit row
    tagged `…-upstream-error` with the actual HTTP status.

    Returns `{}` when not a substituted route — caller merges
    unconditionally. Errors emitting the audit row are swallowed; the
    upstream error must reach the caller intact (defense-in-depth from
    v5.0.1's "audit-write failures must not mask the actual upstream
    error" guarantee).
    """
    if not getattr(route, "compliance_substituted", False):
        return {}

    audit_id = generate_audit_id()
    blocked_company = getattr(route, "compliance_blocked_company", "")
    served_model = getattr(route, "litellm_model", "") or ""
    served_company = getattr(route, "compliance_served_company", "")
    headers = compliance_headers(
        blocked_company=blocked_company,
        requested_model=orig_request_model or "",
        served_model=served_model,
        served_company=served_company,
        served_provider_id=route.provider.id,
        audit_id=audit_id,
    )
    try:
        await emit_event(
            db,
            audit_id=audit_id,
            api_key_id=key_record.id,
            event_type="model_substitution",
            reason_code=(
                f"api-key-policy:blocked-company:{blocked_company}"
                f"-upstream-error"
            ),
            http_status=status_code,
            requested_model=orig_request_model,
            served_model=getattr(route, "litellm_model", None),
            served_provider_id=route.provider.id,
            blocked_company=blocked_company or None,
            client_user_agent=request.headers.get("user-agent", "")[:200],
            commit=True,
        )
    except Exception as exc:
        logger.warning(
            "compliance.upstream_error_audit_failed audit_id=%s err=%s",
            audit_id, exc,
        )
    return headers


__all__ = [
    "raise_if_banned_client_ua",
    "raise_for_no_substitute_exception",
    "emit_substitution_disclosure_for_route",
    "disclosure_headers_for_upstream_error",
]
