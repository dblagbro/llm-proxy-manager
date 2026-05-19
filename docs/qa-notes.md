# QA notes

Operational observations + environmental quirks + things-you-should-know-when-debugging that don't fit cleanly in `bug-log.md` or `test-plan.md`.

Created 2026-05-09 during the deep QA pass. Append-only ledger.

---

## 2026-05-12 — Querying cluster-replicated tables (operator/agent runbook)

### The trap

Tables that participate in `/cluster/sync` (currently `external_usage_snapshot`, `ai_review`, `blocked_ips`) get rows replicated from peer nodes with the **same `captured_at` / timestamp** as the originating row. A naive "give me the latest per provider" query like

```python
# ❌ DON'T — non-deterministic on cluster-replicated tables
q = (
    select(ExternalUsageSnapshot)
    .order_by(ExternalUsageSnapshot.captured_at.desc())
    .limit(10)
)
seen = set()
for row in (await s.execute(q)).scalars():
    if row.provider_id in seen:
        continue
    seen.add(row.provider_id)
    yield row
```

silently returns the wrong row when two siblings tie on `captured_at` — SQLA emits whichever happens first, and "first" is replica-vs-originator non-deterministic. This bit our self-monitor twice on 2026-05-12 (07:07 EDT and 10:27 EDT), reporting "worker stalled" while docker logs showed it firing on schedule.

### The fix

Use `MAX(captured_at) GROUP BY provider_id` as a subquery and join back:

```python
# ✅ DO — deterministic latest-per-provider
from sqlalchemy import select, func

latest = (
    select(
        ExternalUsageSnapshot.provider_id,
        func.max(ExternalUsageSnapshot.captured_at).label("mx"),
    )
    .group_by(ExternalUsageSnapshot.provider_id)
    .subquery()
)

q = (
    select(
        Provider.name,
        ExternalUsageSnapshot.captured_at,
        ExternalUsageSnapshot.seven_day_utilization,
        ExternalUsageSnapshot.auth_state,
    )
    .join(Provider, Provider.id == ExternalUsageSnapshot.provider_id)
    .join(
        latest,
        (latest.c.provider_id == ExternalUsageSnapshot.provider_id)
        & (latest.c.mx == ExternalUsageSnapshot.captured_at),
    )
)
```

If two rows still tie after the join (e.g. originator + replica with identical `captured_at`), add a tiebreaker like `.order_by(ExternalUsageSnapshot.id.desc()).limit(1)` per provider — but that's normally unnecessary; SQLA returns one row per provider via the join key.

### Applies to

- `external_usage_snapshot` — cluster-synced since v3.7.15
- `ai_review` — cluster-synced since v3.7.15
- `blocked_ips` — cluster-synced since v3.7.15 (use tombstone column `deleted_at IS NULL` filter)

For non-replicated tables (e.g. local-only `activity_log`), the naive `ORDER BY + LIMIT + seen-set` pattern is fine — but get in the habit of using the GROUP BY pattern uniformly; it's the same effort and immune to future replication.

---

## 2026-05-10 — QA pass v3.7.13 / v3.7.14 observations

### Admin auth: `require_admin` accepts SESSION TOKENS ONLY, not admin API keys

The `_extract_token()` helper in `app/auth/admin.py` reads the
`llmproxy_session` cookie OR the `Authorization: Bearer <token>`
header — but **the token must be a session token from the `sessions`
table**, not an API key from `api_keys`. Creating an `ApiKey` row
with `key_type='admin'` and trying to use its `llmp-...` value
returns 401 "Session expired or invalid" because `_get_session()`
finds no matching `sessions.token`.

To programmatically obtain an admin session for testing:
```python
from app.auth.admin import create_session
token = await create_session(user_id, username, role)
# token now valid as Authorization: Bearer <token>
```

The default credentials in CLAUDE.md (`admin`/`admin`) only work on
**first boot when no users exist**. Once any User row exists, the
default-admin seed is skipped — the actual admin password must be
known to use the login flow.

### Sessions table: `created_at` / `last_seen_at` are FLOAT (unix epoch)

`app/models/db.py:Session` stores timestamps as `Float` (not
`DateTime`). Inserting a `datetime.datetime` value into them via
SQLAlchemy ORM raises `TypeError: float() argument must be a string
or a real number, not 'datetime.datetime'` deep inside the SQLA
processor. Use `time.time()` directly, or use
`app.auth.admin.create_session()` which handles this correctly.

This caught me building a session manually — the helpful path is
always to use `create_session()`.

### IP block: an operator who blocks their own IP can lock themselves out (BUG-019)

The v3.7.11 IP block middleware runs at the very front of the
ASGI stack (correctly — it short-circuits expensive work for
blocked traffic). But it had no recovery exemption. v3.7.14 added
narrow exemptions for `/api/auth/login` and `/api/admin/blocked-ips`
so an operator can always sign in and remove block entries even
from a blocked IP.

If you see "Source IP is blocked by administrator." while you have
admin credentials: hit `DELETE /api/admin/blocked-ips/<your-ip>`
and you'll get through. If you're on a pre-v3.7.14 build (shouldn't
happen post-2026-05-10), direct DB recovery is:
```python
from sqlalchemy import delete
from app.models.db import BlockedIp
await db.execute(delete(BlockedIp).where(BlockedIp.ip == '<ip>'))
await db.commit()
```
Then restart the container to flush the 30s in-memory cache.

### Compose-on-peer: SSH command working directory is `/home/dblagbro`, not `/home/dblagbro/docker`

When deploying a single container to a peer via SSH, **do not** rely
on `cd /home/dblagbro/docker &&` — the Bash tool here strips `cd`
prefixes from commands by policy. Instead use `-f` + `--project-directory`:
```bash
ssh tmrwww02 'sudo docker compose \
  -f /home/dblagbro/docker/docker-compose.yml \
  --project-directory /home/dblagbro/docker \
  up -d --force-recreate --no-deps llm-proxy2'
```
Without `--project-directory`, relative paths in the compose file
(volumes, env_file, build contexts) resolve against the SSH default
home directory and break the build. This bit me twice during
v3.7.14 deploy.

### Cluster sync gap: new v3.7.x tables not auto-syncing (BUG-016)

When adding tables that should be cluster-replicated, they must be
explicitly added to the cluster-sync allowlist in
`app/cluster/sync.py`. The three v3.7.x tables that landed without
sync entries are documented in BUG-016. Pattern: new schema files
should add an `# CLUSTER-SYNC: yes/no` comment near the class
definition to make the intent explicit.

---

## 2026-05-09 — Initial QA pass observations

### Auth header conventions (do NOT confuse them)

- **`/v1/messages`**: requires `x-api-key: llmp-...` (Anthropic spec convention). `Authorization: Bearer ...` returns HTTP 401 "Missing API key". This is correct per Anthropic's API spec.
- **`/v1/chat/completions`**: accepts `Authorization: Bearer llmp-...` (OpenAI spec convention).
- **`/api/monitoring/*` admin endpoints**: require an admin SESSION cookie, not an API key. Bearer token returns 401 "Session expired or invalid". Use the admin login UI to get a session.
- **`/lmrh/*` endpoints**: API key (Bearer or x-api-key both accepted).
- **Symptom of confusion**: requesting `/v1/messages` with `Authorization: Bearer` returns 401 "Missing API key". I fell into this trap during T4 of the QA pass. The 401 is correct but misleading — there is an API key, just in the wrong header.

### Test contamination of production DB

The integration tests in `tests/integration/` hit the LIVE deployed proxy (`https://www.voipguru.org/llm-proxy2/`) by design — that's documented in CLAUDE.md. They create + soft-delete `pytest-mock` provider rows.

Consequence: every integration test run leaves cruft in:
1. `providers` table (rows with `deleted_at` set)
2. Circuit breaker `_local_states` dict (orphan entries — see BUG-012)
3. Activity log (test traffic mixed with real)

Before reasoning about provider state from a `/health` snapshot, **filter for `name NOT LIKE 'pytest%' AND deleted_at IS NULL`**. Otherwise you'll see ghost providers.

### Live HTTPS endpoint reference

- Production fleet: `https://www.voipguru.org/llm-proxy2/` (www01) and `https://www2.voipguru.org/llm-proxy2/` (www02)
- Smoke: `https://www.voipguru.org/llm-proxy2-smoke/`
- GCP: `https://34.170.189.19/llm-proxy2/` (self-signed cert; `curl -k` required)
- Bridge sidecar: `https://www.voipguru.org/grok-bridge/api/status` (token-gated)
- Test API key (in memory `feedback_paperless_token_burn`): `llmp-BfVsaDkIUjCUJymKMUcoiRcRbYHugPvP0h51hSGGCik` (key=grok-web-smoke)

### Cluster behavior — what's per-node vs cluster-replicated

Per `docs/architecture.md`:

- **Per-node** (each cluster member computes independently):
  - `activity_log` rows
  - `provider_metrics` 5-min buckets
  - `_local_states` for circuit breakers
  - LMRHv2 snapshot ETags (consequence: callers polling via load-balancer see ETag drift even when config is identical — BUG-011)
- **Cluster-replicated** (LWW with last_user_edit_at tie-break):
  - `providers` table
  - `model_capabilities` (with v3.4.1+ aliases / v3.5.0+ family/variant)
  - `model_aliases` table
  - `lmrh_dims` + `lmrh_proposals`
  - `system_settings` (the runtime-tunable settings)

### Performance observations from T-pass

- `/health` average response: ~9ms. Excellent.
- `/v1/models` with auth: ~50-200ms (depends on whether snapshot is fresh)
- `/lmrh/providers` 304 Not-Modified round-trip: ~30ms (well under the 60s recommended polling interval)
- LMRH per-key rate limit: **4 req/min** for `/lmrh/providers`. Burst of 10 → 5 succeed, 5 get 429 with `Retry-After`. The 4/min is intentionally low; SSE subscribe is the recommended path for high-volume callers.
- `/v1/chat/completions` with grok-3 via Grok-Web: 4-5s avg latency, 33% rate-limit hit rate (per the 2026-05-09 PM Grok eval bench)
- `/v1/chat/completions` with grok-4: 10s avg latency, 5x output-token bloat from reasoning trace

### Common debugging commands

```bash
# Inspect probe back-off state (admin endpoint, v3.5.4+)
curl -sk -H "Cookie: session=$ADMIN_SESSION" https://www.voipguru.org/llm-proxy2/api/monitoring/probe-state

# Or via docker exec (no admin session needed):
sudo docker exec llm-proxy2 python3 -c "
from app.monitoring.keepalive import get_backoff_state
print(get_backoff_state())
"

# Inspect circuit breaker state for all providers
sudo docker exec llm-proxy2 python3 -c "
from app.routing.circuit_breaker import get_all_states
import json; print(json.dumps(get_all_states(), indent=2))
"

# Live SSE smoke
curl -sk -N -H "Authorization: Bearer $KEY" \
  "https://www.voipguru.org/llm-proxy2/lmrh/stream?heartbeat_sec=10" -m 8

# Activity log query (note: created_at is SPACE-separated, not ISO-T)
# See reference_llm_proxy2_db_query_gotchas.md for full gotchas
sudo docker exec llm-proxy2 python3 -c "
import sqlite3
from datetime import datetime, timedelta
db = sqlite3.connect('/app/data/llmproxy.db')
db.row_factory = sqlite3.Row
cutoff = str(datetime.utcnow() - timedelta(hours=1))  # MUST be str(), not isoformat()
for r in db.execute('SELECT severity, event_type, COUNT(*) c FROM activity_log WHERE created_at >= ? GROUP BY severity, event_type', (cutoff,)):
    print(f'{r[\"severity\"]:8} {r[\"event_type\"]:20} {r[\"c\"]}')
"
```

### Environment quirks worth knowing

1. **Frontend type-check is silent on success** — `npx tsc --noEmit` returns nothing when clean; don't interpret empty stdout as a failure.
2. **Container clock is UTC** — host is EDT. `datetime.utcnow()` inside the container matches the activity_log timestamps.
3. **SQLite WAL mode is on** — readers may see slightly stale snapshots; if a query result looks impossible (e.g. all `enabled=0`), re-run before declaring it a bug. Saw this during the QA pass; first query returned all-disabled, second returned all-enabled with no DB modification between them.
4. **OpenRouter fallback rate** — Grok models route through Grok-Web (priority 1) by default. To force OpenRouter for testing, send `LLM-Hint: exclude=grok-web`.
5. **SDK `subscribe()` blocks the calling thread** — always wrap in a daemon thread (documented in `sdk/python/README.md` "SSE push" section) unless you want your main thread blocked.
6. **`/v1/models` returns 565+ entries** — most are de-duped via canonical id, a small number have aliases. Don't be alarmed by the count; the OpenRouter scan picks up everything in their catalog.

### Recently fixed (audit trail)

- v3.5.1 — R1 cache helper extraction had a NameError silently swallowed by `try/except`. Fixed mid-development by returning the cache_decision in a tuple. **Lesson**: helper extractions that change return types must verify all downstream uses; `try/except Exception` is a debugging black hole.
- v3.5.4 — `usage_weekly_limit_tokens=20M` on the VG Anthropic-Max account caused 256% reports because Anthropic's actual Pro Max allowance is unknown / higher. Added tooltip clarification that this field is operator-imposed, not provider-actual.

### Anti-patterns to avoid

- **Don't use `try: action() except Exception: pass`** — masks real errors, makes refactors dangerous (R1 incident above)
- **Don't return raw upstream exception text** — leaks stack traces (BUG-007, BUG-008)
- **Don't soft-delete in tests** — leaves orphan state across the cluster (BUG-003, BUG-012)
- **Don't poll `/lmrh/providers` faster than 4/min per key** — rate limit will fire (use SSE subscribe instead for fresher data)
- **Don't compare ETags across cluster nodes** — they're per-node by design (BUG-011)

### Future testing investments worth making

In rough order of bang-for-buck:

1. **Add Pydantic input validation** to `/v1/messages` + `/v1/chat/completions` — closes 4 bugs at once
2. **Add `pytest-randomly`** to surface test-order dependencies (BUG-001 contributor)
3. **Spin up a localhost-only mock proxy** for integration tests so they stop polluting the production DB
4. **Add Playwright tests for v3.5.x dashboard widgets** — they're complex enough to break silently
5. **Add a "release smoke" workflow** that runs the manual probes from this QA pass automatically against a candidate release

---

## v4.3.0 QA pass notes (2026-05-18)

### Method that worked well

- **Throwaway stack on a copy of the prod DB** — `docker cp` the live
  `llm-proxy2:/app/data/llmproxy.db` to a temp dir, run the released
  `dblagbro/{llm-proxy2,whisper-bridge}:4.3.0` images against it on a private
  network with `CLUSTER_ENABLED=false`. Gives realistic testing (real
  providers, real models, real AIRI) with **zero prod pollution** — every
  test artifact (incl. the AIRI conversation from the integrated-TTS test)
  dies with the throwaway. The prod fleet got read-only checks only.

### Gotchas hit this pass

1. **Mounted-DB permissions** — the image runs as `appuser` uid 1001; a
   host-dir bind-mount shadows the image's `chown`, so SQLite fails with
   "attempt to write a readonly database" on `PRAGMA journal_mode=WAL`.
   Fix: `chmod -R 777` the mounted data dir before starting the container.
2. **`page.request` bypasses `page.route`** — Playwright's API-request
   context does not go through `page.route` rewriting and does not inherit
   the page's path-scoped session cookie. For authenticated API checks
   against a no-nginx throwaway, use `page.evaluate(fetch(...))` with
   `credentials:'include'` instead — it goes through the prefix rewrite and
   the page cookies.
3. **Headless audio** — Chromium headless has no audio device; TTS *audible*
   output is unverifiable (BUG-022). The `/api/airi/speak` call + the WAV
   payload + the `<audio>` wiring are all verified; the sound is not.
4. **App theme ≠ OS theme** — the app has its own light/dark toggle
   (`aria-label="Switch to light mode"` in the top bar); `emulate_media(
   color_scheme=...)` does nothing. Click the toggle to test light mode.
5. **Console-error counting** — a browser logs every non-2xx `fetch` as a
   console error, so negative API probes inflate the count. Count console
   errors only during a clean UI run with no deliberate 4xx probes.

### v4.3 specifics

- `/api/airi/speak` and the sidecar `/speak` both cap text at 6000 chars
  (413 over). The proxy validates empty/oversize *before* forwarding, so the
  sidecar's own empty/oversize checks are only reached on a direct call.
- `_bridge_headers()` omits the `Authorization` header when no token is set
  (httpx rejects an empty `Bearer ` value); the sidecar treats no token as
  open. Prod always sets the token.
- TTS audio + text are transient — never persisted (temp file, deleted on
  context exit), so TTS testing leaves no artifacts even on a real DB.
