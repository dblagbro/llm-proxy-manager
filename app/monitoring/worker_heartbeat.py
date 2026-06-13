"""v5.4.0 — WorkerHeartbeat factory: per-worker liveness telemetry.

Closes BUG-069 / BUG-074 (post-refactor sweep 2026-06-12). Every
background loop was previously invisible to a snapshot probe; this
module gives each worker a place to record ``last_run``,
``last_status``, and ``last_note`` rows in ``system_settings`` so the
``/health`` envelope can report ``workers: {name: {last_run, status,
age_sec}}`` and an operator can tell whether each loop is alive
without scraping container stderr.

Design notes:
- Each worker has ONE ``WorkerHeartbeat(name="…")`` instance, declared
  module-level. Calling ``await hb.tick(status="ok", note="…")`` once
  per iteration writes (or updates) the three rows.
- Writes use UPSERT semantics on ``system_settings.key`` so the row
  count stays bounded (3 rows per worker, ever).
- Failures inside ``tick()`` are swallowed and logged at WARNING — a
  heartbeat write failure must not crash the worker it's measuring.
- ``WorkerHeartbeat.snapshot_all()`` is the read side used by the
  ``/health`` envelope; it returns a list of ``{name, last_run,
  status, age_sec, note}`` dicts for every worker that has ever
  ticked. Cheap — single keyspace scan.
- Intentionally NOT cached. Heartbeats are written ~once/interval
  (interval ≥ 60s in practice); cache complexity outweighs the win.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)


_KEY_PREFIX = "worker."


def _key(name: str, field: str) -> str:
    return f"{_KEY_PREFIX}{name}.{field}"


@dataclass
class WorkerHeartbeat:
    """One named worker's heartbeat writer.

    Attributes:
        name: short stable identifier (``"keepalive"``,
            ``"ai_provider_supervisor"``, ``"cluster_sync_push"``, …).
            Becomes part of the ``system_settings`` key and the
            ``/health`` envelope label.
        expected_interval_sec: optional. When set, ``/health`` flags a
            stale heartbeat with ``status="stale"`` if
            ``age_sec > 3 * expected_interval_sec``. Use ``None`` for
            workers whose tick cadence is dynamic.
    """

    name: str
    expected_interval_sec: Optional[float] = None

    async def tick(
        self,
        status: str = "ok",
        note: Optional[str] = None,
    ) -> None:
        """Write the heartbeat row triplet. Failures are logged and
        swallowed — a heartbeat failure must NEVER crash its caller."""
        from app.models.database import AsyncSessionLocal
        from app.models.db import SystemSetting
        try:
            now_ts = time.time()
            async with AsyncSessionLocal() as db:
                await _upsert(db, _key(self.name, "last_run"), str(now_ts))
                await _upsert(db, _key(self.name, "last_status"), status)
                if note is not None:
                    await _upsert(db, _key(self.name, "last_note"), note[:240])
                await db.commit()
        except Exception as exc:
            logger.warning(
                "worker_heartbeat.write_failed worker=%s err=%r",
                self.name, exc,
            )


async def _upsert(db, key: str, value: str) -> None:
    from app.models.db import SystemSetting
    rs = await db.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    row = rs.scalar_one_or_none()
    if row is None:
        db.add(SystemSetting(
            key=key, value=value, value_type="str", updated_at=time.time(),
        ))
    else:
        row.value = value
        row.updated_at = time.time()


async def snapshot_all() -> list[dict[str, Any]]:
    """Read side: return one dict per worker that has ever ticked.

    Output shape (per worker):
        {
            "name": "keepalive",
            "last_run": 1780912345.6,    # unix ts
            "age_sec": 12.3,
            "status": "ok",              # last tick's status string
            "note": "12 probes ok",      # last tick's note (may be absent)
            "stale": False,              # age_sec > 3 * expected_interval_sec
        }

    Stale-detection uses each worker's ``expected_interval_sec`` if it
    has been registered via ``register_expected_interval``. Workers
    that haven't been registered get ``stale=None`` (unknown cadence).
    """
    from app.models.database import AsyncSessionLocal
    from app.models.db import SystemSetting

    now_ts = time.time()
    workers: dict[str, dict[str, Any]] = {}
    try:
        async with AsyncSessionLocal() as db:
            rs = await db.execute(
                select(SystemSetting).where(
                    SystemSetting.key.like(f"{_KEY_PREFIX}%")
                )
            )
            for row in rs.scalars().all():
                # key shape: worker.<name>.<field>
                parts = row.key.split(".", 2)
                if len(parts) != 3 or parts[0] != "worker":
                    continue
                _, name, field = parts
                w = workers.setdefault(name, {"name": name})
                if field == "last_run":
                    try:
                        w["last_run"] = float(row.value or 0)
                    except ValueError:
                        w["last_run"] = None
                elif field == "last_status":
                    w["status"] = row.value
                elif field == "last_note":
                    w["note"] = row.value
    except Exception as exc:
        logger.warning("worker_heartbeat.snapshot_failed err=%r", exc)
        return []

    out: list[dict[str, Any]] = []
    for name, w in sorted(workers.items()):
        last_run = w.get("last_run")
        if last_run is not None:
            age = max(0.0, now_ts - last_run)
            w["age_sec"] = round(age, 1)
        else:
            w["age_sec"] = None
        interval = _EXPECTED_INTERVALS.get(name)
        if interval is not None and w.get("age_sec") is not None:
            w["stale"] = w["age_sec"] > 3 * interval
        else:
            w["stale"] = None
        out.append(w)
    return out


# Module-level registry of expected intervals so snapshot_all() can
# flag stale workers. Populated at startup by each worker module via
# ``register_expected_interval``.
_EXPECTED_INTERVALS: dict[str, float] = {}


def register_expected_interval(name: str, seconds: float) -> None:
    """Declare a worker's expected tick cadence so /health can flag
    stale heartbeats. Idempotent."""
    _EXPECTED_INTERVALS[name] = float(seconds)
