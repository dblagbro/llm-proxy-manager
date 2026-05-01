"""Audit log export — Wave 6 enterprise feature.

Writes ActivityLog rows to JSONL on disk, optionally uploads to S3 (or any
S3-compatible bucket: MinIO, Backblaze B2, Wasabi, etc.) via boto3 when a
bucket is configured. Compliance-friendly append-only format.

File format: one JSON object per line.

Usage (admin API):
    POST /api/audit/export        → on-demand export
    GET  /api/audit/exports       → list past exports
    GET  /api/audit/exports/{id}  → download a specific file

Scheduler (optional): call run_periodic_export() from a background task
to export at a fixed cadence.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ActivityLog
from app.config import settings
from app.utils.timefmt import utc_iso

logger = logging.getLogger(__name__)


EXPORT_DIR = Path(
    getattr(settings, "audit_export_dir", None) or "/app/data/audit_exports"
)


@dataclass
class ExportResult:
    path: Path
    row_count: int
    bytes_written: int
    s3_key: Optional[str] = None
    s3_bucket: Optional[str] = None


def _ensure_dir() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _serialize_row(r: ActivityLog) -> dict:
    return {
        "id": r.id,
        "event_type": r.event_type,
        "severity": r.severity,
        "message": r.message,
        "provider_id": r.provider_id,
        "api_key_id": r.api_key_id,
        "event_meta": r.event_meta or {},
        "created_at": utc_iso(r.created_at),
    }


async def export_activity_log(
    db: AsyncSession,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    s3_bucket: Optional[str] = None,
    s3_prefix: str = "audit/",
) -> ExportResult:
    """Dump matching ActivityLog rows to JSONL; optionally upload to S3.

    Time window:
        since = None → from the beginning
        until = None → until now

    Returns the ExportResult with local path + row count + optional S3 key.
    """
    _ensure_dir()

    query = select(ActivityLog).order_by(ActivityLog.id)
    if since is not None:
        query = query.where(ActivityLog.created_at >= since)
    if until is not None:
        query = query.where(ActivityLog.created_at <= until)

    result = await db.execute(query)
    rows = result.scalars().all()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"audit-{ts}.jsonl"
    path = EXPORT_DIR / filename

    bytes_written = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            line = json.dumps(_serialize_row(r), sort_keys=True) + "\n"
            f.write(line)
            bytes_written += len(line.encode("utf-8"))

    logger.info(
        "audit_export.wrote",
        extra={"path": str(path), "row_count": len(rows), "bytes": bytes_written},
    )

    s3_key = None
    resolved_bucket = s3_bucket or getattr(settings, "audit_export_s3_bucket", None)
    if resolved_bucket:
        s3_key = _upload_to_s3(path, resolved_bucket, s3_prefix + filename)

    return ExportResult(
        path=path,
        row_count=len(rows),
        bytes_written=bytes_written,
        s3_key=s3_key,
        s3_bucket=resolved_bucket if s3_key else None,
    )


def _upload_to_s3(path: Path, bucket: str, key: str) -> Optional[str]:
    """Upload a file to S3 (or any S3-compatible endpoint). Returns key on success."""
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.warning("audit_export.s3_skipped", extra={"reason": "boto3 not installed"})
        return None

    endpoint = getattr(settings, "audit_export_s3_endpoint", None) or None
    region = getattr(settings, "audit_export_s3_region", None) or "us-east-1"
    access_key = getattr(settings, "audit_export_s3_access_key", None) or None
    secret_key = getattr(settings, "audit_export_s3_secret_key", None) or None

    client_kwargs: dict = {"region_name": region}
    if endpoint:
        client_kwargs["endpoint_url"] = endpoint
    if access_key and secret_key:
        client_kwargs["aws_access_key_id"] = access_key
        client_kwargs["aws_secret_access_key"] = secret_key

    try:
        s3 = boto3.client("s3", **client_kwargs)
        s3.upload_file(str(path), bucket, key)
        logger.info("audit_export.s3_uploaded", extra={"bucket": bucket, "key": key})
        return key
    except Exception as exc:
        logger.error("audit_export.s3_failed", extra={"bucket": bucket, "key": key, "error": str(exc)})
        return None


def list_exports() -> list[dict]:
    """Return metadata about exports currently on disk."""
    _ensure_dir()
    out = []
    for p in sorted(EXPORT_DIR.glob("audit-*.jsonl"), reverse=True):
        try:
            stat = p.stat()
            out.append({
                "filename": p.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
        except FileNotFoundError:
            continue
    return out


def prune_old_exports(retention_days: int = 90) -> int:
    """Delete local exports older than `retention_days`. Returns delete count."""
    _ensure_dir()
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for p in EXPORT_DIR.glob("audit-*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except FileNotFoundError:
            pass
    return removed
