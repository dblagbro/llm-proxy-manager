#!/bin/bash
# Safe database dumps — run before nightly tar backup.
# Uses SQLite .backup API and pg_dump so copies are consistent even under active writes.
# All output goes to this script's stdout (caller should redirect to a log file).
# Dumps land in /tmp/safe-db-dumps/ — caller tars and then cleans that dir.

DUMP_DIR="/tmp/safe-db-dumps"
mkdir -p "$DUMP_DIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [safe-dumps] $*"; }
log "Starting — writing to $DUMP_DIR"

sqlite_backup() {
    # 2026-05-13 fix: switched from `.backup` to `VACUUM INTO`.
    # `.backup` acquires a long-lived read lock that conflicts with active
    # writes on busy DBs (hub.db, app.db etc.) and hangs indefinitely —
    # caught a 9-hour hang on coordinator_hub_data/hub.db twice tonight.
    # `VACUUM INTO` is atomic and snapshot-isolated; it does not block
    # writers and never hangs. The output file is a defragmented copy of
    # the DB (slightly smaller than the source — bonus).
    # Timeout still set as belt-and-suspenders.
    local src="$1" dest="$2"
    if sudo test -f "$src"; then
        # Remove any stale destination — VACUUM INTO refuses to overwrite.
        sudo rm -f "$DUMP_DIR/$dest" 2>/dev/null
        if sudo timeout 600 sqlite3 "$src" "VACUUM INTO '$DUMP_DIR/$dest'" 2>/dev/null; then
            log "OK  sqlite  $dest"
        else
            log "ERR sqlite  $dest (VACUUM INTO failed or timed out after 10min)"
        fi
    else
        log "SKIP sqlite $src (not found)"
    fi
}

pg_backup() {
    local container="$1" user="$2" db="$3" dest="$4"
    if docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
        docker exec "$container" pg_dump -U "$user" "$db" > "$DUMP_DIR/$dest" 2>/dev/null \
            && log "OK  pg_dump $dest" \
            || log "ERR pg_dump $dest"
    else
        log "SKIP pg    $container (not running)"
    fi
}

# ── SQLite ────────────────────────────────────────────────────────────────────
sqlite_backup /var/lib/docker/volumes/docker_tax_ai_data/_data/financial_analyzer.db       tax_ai_financial_analyzer.db
sqlite_backup /var/lib/docker/volumes/docker_tax_ai_data/_data/usage.db                    tax_ai_usage.db
sqlite_backup /var/lib/docker/volumes/docker_coordinator_hub_data/_data/hub.db             coordinator_hub.db
sqlite_backup /var/lib/docker/volumes/docker_coordinator_hub_data/_data/coordinator.db     coordinator.db
sqlite_backup /var/lib/docker/volumes/docker_ai_analyzer_data/_data/case_intelligence.db   ai_analyzer_case_intelligence.db
sqlite_backup /var/lib/docker/volumes/docker_ai_analyzer_data/_data/llm_usage.db           ai_analyzer_llm_usage.db
sqlite_backup /var/lib/docker/volumes/docker_ai_analyzer_data/_data/app.db                 ai_analyzer_app.db
sqlite_backup /var/lib/docker/volumes/docker_ai_analyzer_data_dev/_data/case_intelligence.db  ai_analyzer_dev_case_intelligence.db
sqlite_backup /var/lib/docker/volumes/docker_ai_analyzer_data_dev/_data/llm_usage.db          ai_analyzer_dev_llm_usage.db
sqlite_backup /var/lib/docker/volumes/docker_ai_analyzer_data_jacob/_data/case_intelligence.db ai_analyzer_jacob_case_intelligence.db
sqlite_backup /var/lib/docker/volumes/docker_devingpt_data/_data/devingpt.db               devingpt.db
sqlite_backup /var/lib/docker/volumes/docker_llm-proxy2-data/_data/llmproxy.db             llmproxy.db
# v5.1.0 / Batch A1 — clone cluster + smoke. Both are full proxy DBs;
# include them so a per-node restore can recover the clone's distinct
# api_keys + cluster_peers state (the clone forked from /llm-proxy2/
# at 2026-06-05 11:39 EDT and has evolved independently since).
sqlite_backup /var/lib/docker/volumes/docker_llm-proxy-data/_data/llmproxy.db              llmproxy-clone.db
sqlite_backup /var/lib/docker/volumes/docker_llm-proxy2-smoke-data/_data/llmproxy.db       llmproxy-smoke.db
sqlite_backup /var/lib/docker/volumes/docker_anomaly_data/_data/anomaly_detector.db        anomaly_detector.db

# v5.1.0 / Batch A1 — per-cluster cluster_peers JSON snapshots. Extracted
# from the live containers (not on-disk DB files) so the dump matches
# the in-flight state including tombstones replicated by peer sync.
# Used for selective per-node restore — a corrupted cluster_peers
# table can be repaired without restoring the whole DB.
peers_dump() {
    local container="$1" dest="$2"
    if docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
        docker exec "$container" python3 -c "
import asyncio, json
from sqlalchemy import select
from app.models.database import AsyncSessionLocal
from app.models.db import ClusterPeer
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ClusterPeer))).scalars().all()
        out = [{'id':r.id,'url':r.url,'name':r.name,
                'added_at':str(r.added_at) if r.added_at else None,
                'removed_at':str(r.removed_at) if r.removed_at else None,
                'last_user_edit_at':r.last_user_edit_at} for r in rows]
        print(json.dumps(out, indent=2))
asyncio.run(main())
" 2>/dev/null > "$DUMP_DIR/$dest" \
            && log "OK  peers   $dest ($(wc -l < "$DUMP_DIR/$dest") lines)" \
            || log "ERR peers   $dest"
    else
        log "SKIP peers   $container (not running)"
    fi
}
peers_dump llm-proxy2       cluster_peers_llm-proxy2.json
peers_dump llm-proxy        cluster_peers_llm-proxy-clone.json
peers_dump llm-proxy2-smoke cluster_peers_llm-proxy2-smoke.json

# ── PostgreSQL ────────────────────────────────────────────────────────────────
pg_backup tax-paperless-postgres  tax_paperless  tax_paperless  tax_paperless.sql
pg_backup paperless-postgres      paperless      paperless      paperless.sql
pg_backup postgres-master         flowise        flowisedb      flowise.sql
pg_backup postgres-plausible      plausible      plausible      plausible.sql

log "Done. Sizes:"
sudo du -sh "$DUMP_DIR"/* 2>/dev/null | while read size file; do
    log "  $size  $(basename $file)"
done
