"""Audit log export endpoints — Wave 6."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.models.database import get_db
from app.monitoring.audit_export import (
    export_activity_log,
    list_exports,
    prune_old_exports,
    EXPORT_DIR,
)
from app.config import settings

router = APIRouter(prefix="/api/audit", tags=["audit"])


class ExportRequest(BaseModel):
    since: Optional[str] = None   # ISO 8601 UTC timestamp
    until: Optional[str] = None
    s3_bucket: Optional[str] = None


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Accept both "...Z" and "...+00:00"
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except ValueError as e:
        raise HTTPException(400, f"Invalid timestamp {ts!r}: {e}")


@router.post("/export")
async def trigger_export(
    body: ExportRequest,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """On-demand export. Accepts optional since/until timestamps + S3 bucket override."""
    since = _parse_iso(body.since)
    until = _parse_iso(body.until)
    result = await export_activity_log(
        db, since=since, until=until, s3_bucket=body.s3_bucket,
    )
    return {
        "filename": result.path.name,
        "row_count": result.row_count,
        "size_bytes": result.bytes_written,
        "s3_key": result.s3_key,
        "s3_bucket": result.s3_bucket,
    }


@router.get("/exports")
async def list_exports_endpoint(_: AdminUser = Depends(require_admin)):
    return list_exports()


@router.get("/exports/{filename}")
async def download_export(
    filename: str,
    _: AdminUser = Depends(require_admin),
):
    """Download an export file by name. The filename is restricted to the
    audit- prefix / .jsonl suffix to avoid path traversal."""
    if not filename.startswith("audit-") or not filename.endswith(".jsonl"):
        raise HTTPException(400, "Invalid filename")
    # Path confinement: resolve and verify it's inside EXPORT_DIR
    path = (EXPORT_DIR / filename).resolve()
    if EXPORT_DIR.resolve() not in path.parents and path.parent != EXPORT_DIR.resolve():
        raise HTTPException(400, "Path traversal blocked")
    if not path.is_file():
        raise HTTPException(404, "Export not found")
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=filename,
    )


@router.post("/prune")
async def prune(_: AdminUser = Depends(require_admin)):
    """Delete local exports older than the configured retention window."""
    days = int(getattr(settings, "audit_export_retention_days", 90) or 90)
    removed = prune_old_exports(days)
    return {"deleted": removed, "retention_days": days}
