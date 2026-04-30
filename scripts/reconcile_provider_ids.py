"""One-shot reconciliation: align this node's provider IDs to a peer's.

Pre-cluster-sync legacy left www2 with different provider IDs than www1/GCP
for the same-named providers. Cluster sync then matches by id-first,
name-fallback — but every metric/CB/activity row is keyed by the local id, so
divergent ids fragment all per-provider history across the cluster.

Run inside the container of the node whose ids should be REWRITTEN to match
the canonical source. Mappings are hard-coded for this one event because the
operator already knows which side to keep; turning this into a runtime API
isn't worth the surface area.

Usage:
    docker exec llm-proxy2 python3 /app/scripts/reconcile_provider_ids.py [--dry-run]
"""
from __future__ import annotations

import sqlite3
import sys

# (old_id_on_this_node, canonical_id_from_www1) — name in comment for sanity.
MAPPINGS: list[tuple[str, str, str]] = [
    ("e5a0073591e07713", "da9fb8d610e5ccfa", "C1 Vertex AI / Google AI"),
    ("29a86c3ff4a3246e", "08200bcfb03a2d5a", "Google Generative LLM"),
    ("a3768d2e7edbace8", "08ba3d40140abecc", "Devin Personal OpenAI ChatGPT"),
    ("b4b62b509d777f9d", "abd423a5acdaf01b", "Anthropic Claude Code #3"),
    ("f7821cc156419ed7", "7eeb065dd8c88e84", "C1 Anthropic Claude"),
    ("cb49aeff54d40fe7", "33376a3c04336723", "Google Generative LLM 2"),
]

# All tables that store a provider_id reference (FK or plain string).
REFERENCING_TABLES: list[tuple[str, str]] = [
    ("model_capabilities", "provider_id"),
    ("provider_metrics",   "provider_id"),
    ("activity_log",       "provider_id"),
    ("model_aliases",      "provider_id"),
    ("runs",               "last_provider_id"),
]

DB_PATH = "/app/data/llmproxy.db"


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    summary: list[str] = []
    for old, new, name in MAPPINGS:
        cur.execute("SELECT name FROM providers WHERE id=?", (old,))
        old_row = cur.fetchone()
        cur.execute("SELECT name FROM providers WHERE id=?", (new,))
        new_row = cur.fetchone()

        if old_row is None and new_row is None:
            summary.append(f"  SKIP {name!r}: neither id present")
            continue
        if old_row is None:
            summary.append(f"  SKIP {name!r}: old id {old} not on this node (already canonical?)")
            continue
        if new_row is not None:
            summary.append(f"  SKIP {name!r}: canonical id {new} ALREADY exists on this node — manual merge required")
            continue

        if old_row[0] != name:
            summary.append(f"  SKIP {name!r}: id {old} now belongs to {old_row[0]!r} — mapping stale")
            continue

        # Count downstream rows for visibility.
        ref_counts = {}
        for table, col in REFERENCING_TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (old,))
                ref_counts[table] = cur.fetchone()[0]
            except sqlite3.OperationalError as e:
                ref_counts[table] = f"err:{e}"
        summary.append(
            f"  RENAME {name!r}: {old} -> {new}; refs={ref_counts}"
        )

        if dry_run:
            continue

        # Update the PK first; with foreign_keys=OFF, this won't break the
        # existing FK references (model_capabilities, model_aliases). Then
        # rewrite each referencing table.
        cur.execute("UPDATE providers SET id=? WHERE id=?", (new, old))
        for table, col in REFERENCING_TABLES:
            try:
                cur.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
            except sqlite3.OperationalError as e:
                summary.append(f"    !! {table}.{col} update failed: {e}")

    if dry_run:
        print("DRY RUN — no changes written")
    else:
        conn.commit()
        print("COMMITTED")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()

    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
