"""
Redis-backed CoT-E session store with in-memory fallback.
Accumulates prior plan analyses per session to enrich multi-turn reasoning.
"""
import json
import time
import asyncio
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "llmproxy:cot:"
_fallback: dict[str, dict] = {}
_redis_client = None
_redis_ok = False


async def _get_redis():
    global _redis_client, _redis_ok
    if _redis_client is not None:
        return _redis_client if _redis_ok else None
    if not settings.redis_url:
        return None
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis_client.ping()
        _redis_ok = True
        logger.info("CoT session store: Redis connected")
    except Exception as e:
        logger.warning(f"CoT session store: Redis unavailable ({e}), using in-memory fallback")
        _redis_ok = False
    return _redis_client if _redis_ok else None


def _clean_fallback():
    cutoff = time.time() - settings.cot_session_ttl_sec
    for k in list(_fallback.keys()):
        if _fallback[k]["ts"] < cutoff:
            del _fallback[k]


async def get_session_analyses(session_id: Optional[str]) -> list[str]:
    if not session_id:
        return []
    r = await _get_redis()
    if r:
        try:
            raw = await r.get(_KEY_PREFIX + session_id)
            return json.loads(raw).get("analyses", []) if raw else []
        except Exception:
            pass
    s = _fallback.get(session_id)
    if s and time.time() - s["ts"] < settings.cot_session_ttl_sec:
        return s["analyses"]
    return []


async def save_session_analysis(session_id: Optional[str], analysis: str):
    if not session_id or not analysis:
        return
    r = await _get_redis()
    max_a = settings.cot_session_max_analyses

    if r:
        try:
            raw = await r.get(_KEY_PREFIX + session_id)
            existing = json.loads(raw).get("analyses", []) if raw else []
            analyses = [*existing[-(max_a - 1):], analysis]
            await r.set(
                _KEY_PREFIX + session_id,
                json.dumps({"analyses": analyses}),
                ex=settings.cot_session_ttl_sec,
            )
            return
        except Exception:
            pass

    _clean_fallback()
    s = _fallback.get(session_id, {"analyses": [], "ts": 0})
    s["analyses"] = [*s["analyses"][-(max_a - 1):], analysis]
    s["ts"] = time.time()
    _fallback[session_id] = s
