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


async def maybe_check(decision: CacheDecision, endpoint: str) -> Optional[CacheHit]:
    """Check cache; emit Prometheus counter for hit/miss/bypass."""
    if not decision.eligible:
        observe_cache_lookup("bypass", endpoint)
        return None
    cache = get_cache()
    hit = await cache.check(
        decision.namespace, decision.query, settings.semantic_cache_threshold
    )
    if hit is None:
        observe_cache_lookup("miss", endpoint)
        return None
    response_text, similarity = hit
    observe_cache_lookup("hit", endpoint, similarity)
    return CacheHit(response_text=response_text, similarity=similarity)


async def maybe_store(
    decision: CacheDecision, response_text: str, min_chars: Optional[int] = None
) -> None:
    """Store response if quality gate passes."""
    if not decision.eligible or not response_text:
        return
    floor = min_chars if min_chars is not None else settings.semantic_cache_min_response_chars
    if len(response_text) < floor:
        return  # too short — likely error/refusal/pathological
    cache = get_cache()
    await cache.store(decision.namespace, decision.query, response_text, decision.ttl_sec)
