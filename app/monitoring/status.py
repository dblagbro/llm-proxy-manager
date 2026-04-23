"""
External provider status page monitor.
Polls official status pages every 5 minutes and auto-degrades circuit
breakers when a provider reports an incident.
"""
import asyncio
import logging
import time
from typing import Optional

import httpx

from app.routing.circuit_breaker import force_open, force_close, CBState, get_all_states

logger = logging.getLogger(__name__)

# Status page URLs mapped to provider_type strings.
# Most AI providers use Atlassian Statuspage (status.json schema).
# Google is special — published incidents feed; we flag degraded when any recent
# incident mentions Vertex/AI/Gemini.
STATUS_PAGES: dict[str, str] = {
    "anthropic": "https://status.anthropic.com/api/v2/status.json",
    "openai":    "https://status.openai.com/api/v2/status.json",
    "google":    "https://status.cloud.google.com/incidents.json",
    "vertex":    "https://status.cloud.google.com/incidents.json",
    "groq":      "https://groqstatus.com/api/v2/status.json",
    "grok":      "https://status.x.ai/api/v2/status.json",
    "cohere":    "https://status.cohere.com/api/v2/status.json",
    "mistral":   "https://status.mistral.ai/api/v2/status.json",
    "together":  "https://status.together.ai/api/v2/status.json",
    "fireworks": "https://status.fireworks.ai/api/v2/status.json",
    "perplexity":"https://status.perplexity.com/api/v2/status.json",
    "deepseek":  "https://status.deepseek.com/api/v2/status.json",
    "replicate": "https://replicatestatus.com/api/v2/status.json",
}

_cache: dict[str, dict] = {}
_POLL_INTERVAL = 300  # 5 minutes
_task: Optional[asyncio.Task] = None

# provider_id → provider_type mapping (populated at startup)
_provider_type_map: dict[str, str] = {}


def register_provider(provider_id: str, provider_type: str, hold_down_sec=None, failure_threshold=None):
    _provider_type_map[provider_id] = provider_type
    from app.routing.circuit_breaker import set_provider_config
    set_provider_config(provider_id, hold_down_sec, failure_threshold)


def _is_degraded_statuspage(data: dict, provider_type: str) -> bool:
    """Parse Atlassian status page response."""
    try:
        indicator = data.get("status", {}).get("indicator", "none")
        return indicator not in ("none", "minor")
    except Exception:
        return False


async def _check_one(provider_type: str) -> tuple[bool, str]:
    """Returns (is_degraded, description)."""
    url = STATUS_PAGES.get(provider_type)
    if not url:
        return False, ""
    now = time.time()
    cached = _cache.get(provider_type)
    if cached and now - cached["ts"] < _POLL_INTERVAL:
        return cached["degraded"], cached["desc"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()

        if provider_type == "google":
            # Google uses a different format — array of active incidents
            incidents = [i for i in (data if isinstance(data, list) else []) if not i.get("end")]
            degraded = len(incidents) > 0
            desc = incidents[0].get("external-desc", "") if incidents else ""
        else:
            degraded = _is_degraded_statuspage(data, provider_type)
            desc = data.get("status", {}).get("description", "")

        _cache[provider_type] = {"ts": now, "degraded": degraded, "desc": desc}
        return degraded, desc
    except Exception as e:
        logger.debug(f"Status check failed for {provider_type}: {e}")
        # Don't degrade on a failed check — only on confirmed incident
        old = _cache.get(provider_type, {})
        return old.get("degraded", False), old.get("desc", "")


async def _monitor_loop(notify_fn=None):
    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        for provider_id, provider_type in list(_provider_type_map.items()):
            degraded, desc = await _check_one(provider_type)
            states = get_all_states()
            current = states.get(provider_id, {}).get("state", "closed")
            if degraded and current == "closed":
                await force_open(provider_id)
                logger.warning(f"Status monitor: {provider_type} degraded — {desc}; opening circuit for {provider_id}")
                if notify_fn:
                    await notify_fn("warning", f"Provider {provider_type} status page reports incident: {desc}", provider_id)
            elif not degraded and current == "open":
                # Only auto-close if the status page says it's recovered
                # (normal circuit recovery is handled by the half-open mechanism)
                pass


def start_monitor(notify_fn=None):
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_monitor_loop(notify_fn))
        logger.info("External status monitor started")


async def get_status_summary() -> dict[str, dict]:
    result = {}
    for ptype in STATUS_PAGES:
        degraded, desc = await _check_one(ptype)
        result[ptype] = {"degraded": degraded, "description": desc}
    return result
