"""
v3.5.10 BUG-010 — alias↔canonical collision cleanup script.

Pre-v3.4.1 the operator had to register `grok-3` AND `x-ai/grok-3`
as TWO separate `ModelCapability` rows so the router (which matched
on exact `model_id`) would accept either spelling. v3.4.1 introduced
the `aliases` JSON column + canonical-id matching, but old bare-name
rows weren't auto-deleted on the canonical-only switch. This left
3 "alias↔canonical collisions" detected during the post-v3.5.7 QA pass:

  x-ai/grok-3   aliases=["grok-3"]   ← canonical row (v3.4.1+)
  grok-3        aliases=[]           ← legacy bare-name row

Same for grok-4. The de-dup logic in `/v1/models` correctly emits
each upstream model once, so it's not a runtime bug, but the legacy
rows are dead weight.

This script:
  1. Walks `model_capabilities` for non-deleted rows
  2. Builds a set of all aliases (lowercase) per (provider_id, alias)
  3. Finds canonical rows whose model_id appears as an alias on
     ANOTHER row of the same provider
  4. Soft-deletes the legacy bare-name rows (they're now redundant)

Run with:
  sudo docker exec -it llm-proxy2 python3 -m tools.cleanup_alias_collisions

Or for dry-run:
  sudo docker exec -it llm-proxy2 python3 -m tools.cleanup_alias_collisions --dry-run

Idempotent — re-running after cleanup is a no-op.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone


async def find_collisions(dry_run: bool = False) -> int:
    """Returns the number of legacy bare-name rows soft-deleted."""
    sys.path.insert(0, "/app")
    from sqlalchemy import select
    from app.models.database import AsyncSessionLocal
    from app.models.db import ModelCapability

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ModelCapability).where(ModelCapability.deleted_at.is_(None))
        )
        rows = list(result.scalars().all())

        # Per-provider: map of canonical_lc → row, and set of alias_lc owned by that provider
        per_provider_canonicals: dict[str, dict[str, ModelCapability]] = {}
        per_provider_aliases: dict[str, set[str]] = {}
        for r in rows:
            pid = r.provider_id
            per_provider_canonicals.setdefault(pid, {})[r.model_id.lower()] = r
            als_set = per_provider_aliases.setdefault(pid, set())
            for a in (r.aliases or []):
                if isinstance(a, str) and a.strip():
                    als_set.add(a.lower())

        # A row is a "legacy bare-name" if its model_id (lowercased) appears
        # as an alias of ANOTHER row in the same provider AND the row's own
        # aliases list is empty. Empty-aliases is the signal that this row
        # was never re-scanned post-v3.4.1.
        to_delete: list[ModelCapability] = []
        for pid, canonicals in per_provider_canonicals.items():
            aliases_in_provider = per_provider_aliases.get(pid, set())
            for canon_lc, row in canonicals.items():
                if canon_lc in aliases_in_provider and not (row.aliases or []):
                    to_delete.append(row)

        if not to_delete:
            print("No alias↔canonical collisions found. Nothing to clean up.")
            return 0

        print(f"Found {len(to_delete)} legacy bare-name capability rows:")
        for r in to_delete:
            print(f"  pid={r.provider_id} model_id={r.model_id} (id={r.id})")

        if dry_run:
            print("\n[DRY RUN] No changes made. Re-run without --dry-run to apply.")
            return 0

        now = datetime.now(timezone.utc)
        for r in to_delete:
            r.deleted_at = now
        await db.commit()
        print(f"\nSoft-deleted {len(to_delete)} rows. Cluster sync will propagate.")
        print(f"After {7} days the daily prune worker hard-deletes them.")
        return len(to_delete)


def main():
    p = argparse.ArgumentParser(
        description="Clean up alias↔canonical collisions in model_capabilities (v3.4.1+)."
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be deleted without modifying the DB")
    args = p.parse_args()
    n = asyncio.run(find_collisions(dry_run=args.dry_run))
    sys.exit(0 if n >= 0 else 1)


if __name__ == "__main__":
    main()
