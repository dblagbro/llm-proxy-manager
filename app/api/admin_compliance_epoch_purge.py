"""v5.4.3 — Admin one-shot pre-compliance data purge.

Closes the security-team-mandated cleanup of data accumulated before
the vendor-neutrality compliance subsystem was fully in force. The
operator-chosen epoch is 2026-06-06 00:00 UTC (v5.2.0 vendor-neutrality
stack live); the security team's posture is that pre-v5.2 data did
not have the policy fields evaluated, so it must be removed.

Hard rules:

- Compliance audit tables are NEVER touched (compliance_events,
  compliance_policy_changes, compliance_audit_chain). The deletion
  itself is recorded in compliance_policy_changes.
- The endpoint accepts a list of tables + cutoff_date + dry_run. Only
  the allow-listed tables (see PURGABLE_TABLES below) can be purged;
  anything else returns 400 even if dry_run=true.
- An audit row is written in compliance_policy_changes BEFORE the
  DELETE runs (so a crash mid-purge still records intent).
- Dry-run returns row counts without modifying anything.
"""
from __future__ import annotations

from datetime import datetime
import json
import secrets
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db

router = APIRouter(
    prefix="/api/admin/compliance-epoch-purge",
    tags=["admin", "compliance"],
)


# Allow-list. Each entry: (table_name, timestamp_column_name).
# If a table here is NOT in the request body's `tables`, it's left alone.
# Tables NOT in this list are rejected by the endpoint even with dry_run.
PURGABLE_TABLES: dict[str, str] = {
    "activity_log": "created_at",
    "provider_metrics": "bucket_ts",
    "provider_ai_review": "captured_at",
    # external_usage_snapshot intentionally NOT listed — operator's
    # 2026-06-12 scoping conversation excluded it from the security
    # team's mandate.
}

# Tables that MUST never appear in PURGABLE_TABLES. The endpoint
# rejects any request that names one of these even via PURGABLE_TABLES
# being mis-edited later (defence-in-depth).
FORBIDDEN_TABLES: set[str] = {
    "compliance_events",
    "compliance_policy_changes",
    "compliance_audit_chain",
    "api_keys",
    "users",
    "providers",
    "system_settings",
}


class PurgeRequest(BaseModel):
    cutoff_date: str = Field(
        ...,
        description=(
            "ISO 8601 UTC timestamp. All rows with timestamp_column < "
            "cutoff_date are deleted. Operator-chosen epoch 2026-06-06 "
            "matches the v5.2.0 vendor-neutrality stack ship date."
        ),
        examples=["2026-06-06T00:00:00Z"],
    )
    tables: List[str] = Field(
        ...,
        description="Table names to purge. Must be a subset of PURGABLE_TABLES.",
        min_length=1,
    )
    dry_run: bool = Field(
        True,
        description=(
            "When true (default): return row counts that WOULD be "
            "deleted, do not modify the DB, do not write an audit row. "
            "When false: hard-delete + write the audit row."
        ),
    )
    reason: Optional[str] = Field(
        None,
        max_length=2000,
        description=(
            "Free-text justification captured in the audit row. "
            "Recommend referencing the security-team ticket id."
        ),
    )


class TableResult(BaseModel):
    table: str
    rows_matched: int
    rows_deleted: int  # 0 in dry-run mode
    oldest_timestamp: Optional[str] = None


class PurgeResponse(BaseModel):
    ok: bool
    dry_run: bool
    cutoff_date: str
    audit_id: Optional[str] = None
    total_rows_matched: int
    total_rows_deleted: int  # 0 in dry-run
    results: List[TableResult]


@router.post("", response_model=PurgeResponse)
async def compliance_epoch_purge(
    body: PurgeRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
) -> PurgeResponse:
    # Reject any table outside the allow-list (defence-in-depth).
    for t in body.tables:
        if t in FORBIDDEN_TABLES:
            raise HTTPException(
                400, f"table '{t}' is in FORBIDDEN_TABLES; never purgeable",
            )
        if t not in PURGABLE_TABLES:
            raise HTTPException(
                400,
                f"table '{t}' not in PURGABLE_TABLES; allowed: "
                f"{sorted(PURGABLE_TABLES)}",
            )

    # Validate cutoff_date parses.
    try:
        # Strip the trailing Z and treat as UTC.
        _cutoff_raw = body.cutoff_date.rstrip("Z")
        datetime.fromisoformat(_cutoff_raw)
    except ValueError:
        raise HTTPException(
            400, f"cutoff_date must be ISO 8601; got {body.cutoff_date!r}",
        )

    # Survey what would be deleted.
    results: List[TableResult] = []
    total_matched = 0
    for t in body.tables:
        ts_col = PURGABLE_TABLES[t]
        n = (await db.execute(
            text(f"SELECT COUNT(*) FROM {t} WHERE {ts_col} < :cutoff"),
            {"cutoff": _cutoff_raw},
        )).scalar() or 0
        oldest = (await db.execute(
            text(f"SELECT MIN({ts_col}) FROM {t} WHERE {ts_col} < :cutoff"),
            {"cutoff": _cutoff_raw},
        )).scalar()
        results.append(TableResult(
            table=t,
            rows_matched=n,
            rows_deleted=0,  # set after DELETE
            oldest_timestamp=str(oldest) if oldest else None,
        ))
        total_matched += n

    if body.dry_run:
        return PurgeResponse(
            ok=True,
            dry_run=True,
            cutoff_date=body.cutoff_date,
            audit_id=None,
            total_rows_matched=total_matched,
            total_rows_deleted=0,
            results=results,
        )

    # Live mode — write the audit row FIRST (so a mid-DELETE crash
    # still leaves intent in the chain), then run the DELETEs.
    audit_id = f"ppc_{int(time.time()*1000):013x}{secrets.token_hex(6)}"
    from app.models.db import CompliancePolicyChange
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=admin.username,
        scope="system",
        target_id=None,
        before_state=json.dumps({
            "matched_per_table": {r.table: r.rows_matched for r in results},
            "total_matched": total_matched,
        }),
        after_state=json.dumps({
            "purge_intent": "delete pre-compliance epoch data",
            "cutoff_date": body.cutoff_date,
            "tables": body.tables,
        }),
        reason=(
            f"compliance_epoch_purge by {admin.username}: hard-delete "
            f"pre-v5.2.0 data per security team mandate. "
            f"cutoff={body.cutoff_date} tables={','.join(body.tables)} "
            f"matched={total_matched}"
            + (f" reason={body.reason}" if body.reason else "")
        ),
        applied_to_peers=json.dumps([]),
        pending_peers=None,
        cluster_sync_status="local_only",
    ))
    await db.commit()

    # Run DELETEs per table.
    total_deleted = 0
    for r in results:
        ts_col = PURGABLE_TABLES[r.table]
        res = await db.execute(
            text(f"DELETE FROM {r.table} WHERE {ts_col} < :cutoff"),
            {"cutoff": _cutoff_raw},
        )
        deleted = getattr(res, "rowcount", 0) or 0
        r.rows_deleted = deleted
        total_deleted += deleted
    await db.commit()

    return PurgeResponse(
        ok=True,
        dry_run=False,
        cutoff_date=body.cutoff_date,
        audit_id=audit_id,
        total_rows_matched=total_matched,
        total_rows_deleted=total_deleted,
        results=results,
    )
