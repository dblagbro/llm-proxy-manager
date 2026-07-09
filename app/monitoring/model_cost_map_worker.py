"""v5.20.4 — Model cost-map sync worker.

Ports the ccflare/LiteLLM pattern from the 2026-06-30 peer-comparison
roadmap: ingest LiteLLM's ``model_prices_and_context_window.json`` on
a cron so day-zero-released models have accurate pricing in the
proxy's cost calculations, even when the installed ``litellm`` Python
package is on an older version.

Design:

- Daily fetch (default 24h) from
  ``https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json``
- Upsert into ``model_pricing_catalog`` — keyed on model name
- ``pricing.estimate_cost_split`` checks the catalog BEFORE calling
  ``litellm.cost_per_token`` so a new model in litellm@main lands in
  the proxy without a package upgrade
- Fetch failures are non-fatal: worker logs, sleeps until next tick,
  the existing lookup path (litellm.cost_per_token + local overrides)
  continues to work

The URL is configurable (``model_cost_map_url`` setting) so a fork /
mirror can be swapped in without a code change.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
DEFAULT_INTERVAL_SEC = 86400  # 24h
_HTTPX_TIMEOUT_SEC = 30

_TASK: Optional[asyncio.Task] = None


def _parse_entry(name: str, entry: dict) -> Optional[dict]:
    """Normalize a single model entry from the LiteLLM JSON.

    LiteLLM's JSON has a top-level ``sample_spec`` key that's just an
    example; skip it. Other entries have varied fields — we only care
    about the pricing + context-window ones.
    """
    if name == "sample_spec" or not isinstance(entry, dict):
        return None
    # Skip entries with no pricing at all — LiteLLM includes some
    # metadata-only ones (e.g., "flat-fee" or "not yet supported").
    input_c = entry.get("input_cost_per_token")
    output_c = entry.get("output_cost_per_token")
    if input_c is None and output_c is None:
        return None
    try:
        return {
            "model_key": name,
            "input_cost_per_token": float(input_c or 0.0),
            "output_cost_per_token": float(output_c or 0.0),
            "max_input_tokens": entry.get("max_input_tokens") or entry.get("max_tokens"),
            "max_output_tokens": entry.get("max_output_tokens"),
            "provider_family": entry.get("litellm_provider"),
        }
    except (TypeError, ValueError):
        return None


async def _fetch_and_upsert() -> tuple[int, int, int]:
    """Fetch the JSON + upsert into ``model_pricing_catalog``.

    Returns ``(added, updated, skipped)`` counts. Raises on network /
    parse failure so the caller can log + back off.
    """
    from app.config import settings
    from app.models.database import AsyncSessionLocal
    from app.models.db_model_pricing import ModelPricingEntry
    from sqlalchemy import select

    url = getattr(settings, "model_cost_map_url", None) or DEFAULT_URL

    import httpx
    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT_SEC) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()

    if not isinstance(payload, dict):
        raise ValueError(f"unexpected root type: {type(payload).__name__}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    added = 0
    updated = 0
    skipped = 0
    async with AsyncSessionLocal() as db:
        # Preload existing rows once — the catalog is small (<2k rows)
        # so we can hold it in memory instead of one round-trip per
        # model.
        rs = await db.execute(select(ModelPricingEntry))
        existing = {row.model_key: row for row in rs.scalars().all()}

        for name, entry in payload.items():
            normalized = _parse_entry(name, entry)
            if normalized is None:
                skipped += 1
                continue
            existing_row = existing.get(normalized["model_key"])
            if existing_row is None:
                db.add(ModelPricingEntry(
                    model_key=normalized["model_key"],
                    input_cost_per_token=normalized["input_cost_per_token"],
                    output_cost_per_token=normalized["output_cost_per_token"],
                    max_input_tokens=normalized["max_input_tokens"],
                    max_output_tokens=normalized["max_output_tokens"],
                    provider_family=normalized["provider_family"],
                    source="litellm_upstream",
                    synced_at=now,
                ))
                added += 1
            else:
                # Only mark as updated when values actually differed —
                # this keeps the ``synced_at`` timestamp meaningful.
                changed = (
                    existing_row.input_cost_per_token != normalized["input_cost_per_token"]
                    or existing_row.output_cost_per_token != normalized["output_cost_per_token"]
                )
                existing_row.input_cost_per_token = normalized["input_cost_per_token"]
                existing_row.output_cost_per_token = normalized["output_cost_per_token"]
                existing_row.max_input_tokens = normalized["max_input_tokens"]
                existing_row.max_output_tokens = normalized["max_output_tokens"]
                existing_row.provider_family = normalized["provider_family"]
                existing_row.synced_at = now
                if changed:
                    updated += 1

        await db.commit()

    # v5.20.4 — invalidate the in-process pricing cache so subsequent
    # cost lookups pick up the fresh values without a proxy restart.
    try:
        from app.monitoring.pricing import invalidate_catalog_cache
        invalidate_catalog_cache()
    except Exception:
        pass

    return added, updated, skipped


async def _sync_worker_loop(interval_sec: int) -> None:
    from app.models.db import ActivityLog
    from app.models.database import AsyncSessionLocal

    while True:
        try:
            added, updated, skipped = await _fetch_and_upsert()
            logger.info(
                "model_cost_map.synced added=%d updated=%d skipped=%d",
                added, updated, skipped,
            )
            try:
                async with AsyncSessionLocal() as db:
                    db.add(ActivityLog(
                        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        severity="info",
                        event_type="model_cost_map.synced",
                        message=(
                            f"Model cost-map synced: added={added} "
                            f"updated={updated} skipped={skipped}"
                        ),
                        event_meta={
                            "added": added,
                            "updated": updated,
                            "skipped": skipped,
                            "interval_sec": interval_sec,
                        },
                    ))
                    await db.commit()
            except Exception:
                # activity_log write failure is non-fatal
                pass
        except Exception as exc:
            logger.warning("model_cost_map.sync_failed err=%r", exc)
        await asyncio.sleep(interval_sec)


def start(interval_sec: Optional[int] = None) -> None:
    """Kick off the sync loop as a background task. Idempotent — if the
    task is already running, returns without re-scheduling."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(
        _sync_worker_loop(interval_sec or DEFAULT_INTERVAL_SEC),
        name="model-cost-map-sync",
    )
    logger.info(
        "model_cost_map.started interval_sec=%d",
        interval_sec or DEFAULT_INTERVAL_SEC,
    )
