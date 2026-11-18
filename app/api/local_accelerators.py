"""Read-only local accelerator snapshot (v5.23 slice 1).

``GET /api/local/accelerators`` — admin-gated, no routing impact.
When ``LOCAL_ACCEL_ENABLED`` is false the probe is a no-op and this
returns ``enabled: false`` with empty accelerators.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.admin import AdminUser, require_admin

router = APIRouter(prefix="/api/local", tags=["local-accelerator"])


@router.get("/accelerators")
async def list_accelerators(_: AdminUser = Depends(require_admin)) -> dict[str, Any]:
    from app.resources.probe import collect_snapshot, snapshot_as_api
    snap = await collect_snapshot()
    return snapshot_as_api(snap)
