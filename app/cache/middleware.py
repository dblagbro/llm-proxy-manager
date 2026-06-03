"""Gateway-layer cache orchestration: decide cacheability, check, store.

Called from both /v1/messages and /v1/chat/completions. Returns structured
results so the endpoint handler can either short-circuit on hit or continue
with the normal LLM path and call store() after success.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.cache.keys import (
    build_namespace,
    split_prior_messages,
    is_cacheable_temperature,
    contains_pii,
)
from app.cache.semantic import get_cache
from app.observability.prometheus import observe_cache_lookup

logger = logging.getLogger(__name__)


@dataclass
class CacheDecision:
    eligible: bool
    reason: str                   # bypass reason if not eligible
    namespace: str = ""
    query: str = ""
    ttl_sec: int = 0


@dataclass
class CacheHit:
    response_text: str
    similarity: float


def resolve_ttl(header_value: Optional[str]) -> int:
    if header_value:
        try:
            return max(60, min(86400, int(header_value)))
        except ValueError:
            pass
    return settings.semantic_cache_ttl_sec


def decide_cacheable(
    *,
    x_cache_header: Optional[str],
    api_key_opt_in: bool,
    key_type: str,
    cot_engaged: bool,
    tool_emulation: bool,
    has_tools: bool,
    webhook_url: Optional[str],
    temperature: Optional[float],
    messages: list[dict],
    model: str,
    tenant_id: str,
    system: Optional[object],
    tools: Optional[list],
    x_cache_ttl_header: Optional[str] = None,
) -> CacheDecision:
    """Single source of truth for whether to cache this request."""
    # Global kill-switch
    if not settings.semantic_cache_enabled:
        return CacheDecision(False, "globally_disabled")

    # Per-request opt-out
    if (x_cache_header or "").lower() in ("none", "off", "false"):
        return CacheDecision(False, "header_opt_out")

    # Per-request force-on bypasses the per-key opt-in (but still enforces safety gates)
    force_on = (x_cache_header or "").lower() in ("semantic", "on", "true")
    if not (force_on or api_key_opt_in):
        return CacheDecision(False, "not_opted_in")

    # claude-code key type: default OFF even if force_on — code traffic has bad hit rate
    if key_type == "claude-code" and not force_on:
        return CacheDecision(False, "claude_code_default_off")

    # Never cache these patterns
    if cot_engaged:
        return CacheDecision(False, "cot_engaged")
    if tool_emulation or has_tools:
        return CacheDecision(False, "tools_present")
    if webhook_url:
        return CacheDecision(False, "webhook_async")
    if not is_cacheable_temperature(temperature):
        return CacheDecision(False, "temperature_too_high")

    prior, query = split_prior_messages(messages)
    if not query:
        return CacheDecision(False, "empty_query")
    if contains_pii(query):
        return CacheDecision(False, "pii_detected")

    namespace = build_namespace(
        tenant_id=tenant_id,
        model=model,
        system=system,
        tools=tools,
        temperature=temperature,
        prior_messages=prior,
    )
    return CacheDecision(
        eligible=True,
        reason="eligible",
        namespace=namespace,
        query=query,
        ttl_sec=resolve_ttl(x_cache_ttl_header),
    )


async def maybe_check(
    decision: CacheDecision,
    endpoint: str,
    *,
    db=None,
    api_key_id: Optional[str] = None,
    requested_model: Optional[str] = None,
) -> Optional[CacheHit]:
    """Check cache; emit Prometheus counter for hit/miss/bypass.

    v5.0.0 compliance: when ``db`` + ``api_key_id`` are supplied, resolve
    the per-key effective blocklist and ask the cache to drop banned
    (and NULL-source — decision 7) rows. If a hit existed but every
    candidate was filtered, emit a ``cache_filtered`` compliance event.
    """
    if not decision.eligible:
        observe_cache_lookup("bypass", endpoint)
        return None
    blocked_companies: Optional[set[str]] = None
    if db is not None and api_key_id:
        try:
            from app.compliance import get_effective_blocklist
            blocked_companies = await get_effective_blocklist(db, api_key_id)
        except Exception as exc:
            # Resolving the blocklist must never block a cache lookup; fall
            # through with no filter so the request itself can proceed.
            logger.warning("cache.middleware.blocklist_resolve_failed %s", exc)
            blocked_companies = None
    cache = get_cache()
    hit = await cache.check(
        decision.namespace,
        decision.query,
        settings.semantic_cache_threshold,
        blocked_companies=blocked_companies or None,
    )
    if hit is None:
        # Distinguish "true miss" from "filtered out". The cache layer
        # can't tell us which, so we re-probe without the blocklist when
        # one is in effect to detect the filter-only case for audit.
        if blocked_companies and db is not None and api_key_id:
            try:
                raw = await cache.check(
                    decision.namespace,
                    decision.query,
                    settings.semantic_cache_threshold,
                    blocked_companies=None,
                )
                if raw is not None:
                    # A hit exists but every candidate is banned for this key
                    # — record the refusal. commit=False so we don't punch a
                    # transaction inside this hot path; the request handler's
                    # own commit will pick it up.
                    try:
                        from app.compliance import emit_event, generate_audit_id
                        await emit_event(
                            db,
                            audit_id=generate_audit_id(),
                            api_key_id=api_key_id,
                            event_type="cache_filtered",
                            reason_code="source-company-banned",
                            http_status=0,  # internal filter — no HTTP response
                            requested_model=requested_model,
                            blocked_company=next(iter(blocked_companies), None),
                            commit=False,
                        )
                    except Exception as exc:
                        logger.warning("cache.middleware.emit_event_failed %s", exc)
            except Exception:
                pass
        observe_cache_lookup("miss", endpoint)
        return None
    response_text, similarity = hit
    observe_cache_lookup("hit", endpoint, similarity)
    return CacheHit(response_text=response_text, similarity=similarity)


async def maybe_store(
    decision: CacheDecision,
    response_text: str,
    min_chars: Optional[int] = None,
    *,
    provider=None,
) -> None:
    """Store response if quality gate passes.

    v5.0.0 compliance: when ``provider`` is supplied, tag the cache row
    with the provider's ``owner_company`` (or the derived company for the
    provider_type). check() uses that tag to drop hits for keys that
    have banned the source company.
    """
    if not decision.eligible or not response_text:
        return
    floor = min_chars if min_chars is not None else settings.semantic_cache_min_response_chars
    if len(response_text) < floor:
        return  # too short — likely error/refusal/pathological
    source_company: Optional[str] = None
    if provider is not None:
        try:
            from app.compliance import provider_type_to_company
            source_company = (
                getattr(provider, "owner_company", None)
                or provider_type_to_company(getattr(provider, "provider_type", None))
            )
        except Exception as exc:
            logger.warning("cache.middleware.resolve_source_company_failed %s", exc)
            source_company = None
    cache = get_cache()
    await cache.store(
        decision.namespace,
        decision.query,
        response_text,
        decision.ttl_sec,
        source_company=source_company,
    )
