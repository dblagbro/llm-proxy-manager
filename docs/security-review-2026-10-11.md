# Security review — 2026-10-11

Full read-only security self-test of llm-proxy-v2 (auth/authz, SSRF/path-traversal,
supply chain/container, data layer). Source of truth: `/mnt/s/code/llm-proxy-v2`.
Live checks were run against the `llm-proxy2` container (v5.22.25) and nginx on
tmrwww01. **No production state, secret, or config was changed by this review.**

Findings that are source defects were fixed in **v5.22.43** (see CHANGELOG / bug-log).
Everything below the line is **operator-only** — secret rotation (GPG-sealed per
`~/.claude/protocols/secret-handling/PROTOCOL.md`), production nginx/compose at
`/home/dblagbro/docker/`, and the deploy — none of which an agent may do without
explicit approval. Secret *values* are never reproduced here; only kind + location.

---

## Fixed in v5.22.43 (source; ships on next approved deploy)

| ID | Sev | What | Where |
|----|-----|------|-------|
| BUG-098 | HIGH | Unauthenticated arbitrary file read (SPA asset-rescue path traversal) → reached the SQLite credential store | `app/main.py` asset-rescue branch; confined with realpath prefix check |
| BUG-099 | HIGH | Per-key MCP tool policy never enforced (authz fail-open); `allow=[]` opt-out silently ignored | `app/auth/keys.py` `ApiKeyRecord` + `verify_api_key` |
| BUG-100 | MED (latent) | SSO open redirect via `X-Forwarded-Prefix` (SSO currently off) | `app/api/auth_sso.py` `_base_path` |
| BUG-101 | MED/LOW | Webhook SSRF to loopback + cloud metadata (RFC1918 still allowed per the documented decision) | `app/api/_input_validation.py` `validate_webhook_url` |
| BUG-102 | LOW | `/api/integration/self-update` always 500'd (`ApiKey.key` does not exist) | `app/integration/self_update.py` |
| BUG-103 | LOW (latent) | `require_catalog_auth` missing `deleted_at` tombstone check | `app/auth/catalog_scope.py` |
| BUG-104 | HIGH | ReDoS in the PII email regex — one request pins the event loop for minutes | `app/privacy/pii.py` |
| BUG-105 | MED | Epoch-purge deleted one extra day (ISO `T` vs SQLite space-form); irreversible | `app/api/admin_compliance_epoch_purge.py` |
| BUG-106 | MED | Cluster sync accepted a peer-chosen primary key on compliance tables | `app/cluster/sync_handlers.py` |
| BUG-107 | MED | Compliance cache failed OPEN on blocklist-resolution error | `app/cache/middleware.py` |
| BUG-108 | HIGH | Secret redaction missed structured bodies + modern tokens; default-on previews redacted nothing | `app/monitoring/helpers.py` |
| — | doc | Provider credential columns are PLAINTEXT, not Fernet as the comment claimed | `app/models/db_provider.py` |
| — | obs | Loud (non-fatal) boot log when `SECRET_KEY` is the public placeholder | `app/main.py` lifespan |

Each has a regression test. The SECRET_KEY boot check is intentionally **non-fatal**
so that deploying v5.22.43 before the env is set does not become an outage — the
hard remediation is O-1 below.

---

## OPERATOR-ONLY — ranked. None of these are code in this repo.

These were independently confirmed against the live host by the review. They need
your hands because they are secret rotation, production infra config, or a deploy.

### O-1 (S1) — `SECRET_KEY` is the public placeholder in production
`change-me-in-production` (the value published in `.env.example`) is live:
`LLM_PROXY2_SECRET_KEY` is not set in `/home/dblagbro/docker/.env`, so the compose
default at `docker-compose.yml:1280` wins. That value is the Fernet key-encryption
key for stored API keys (`app/auth/key_encryption.py`), so anyone with the DB file
decrypts every `llmp-…` key.
- **Do:** generate 32+ random bytes, add `LLM_PROXY2_SECRET_KEY` to `.env` (GPG-seal
  the backup), recreate `llm-proxy2` on **each** node. Rotation makes existing
  `encrypted_key` values unreadable → re-issue keys or accept that "reveal" degrades
  (auth keeps working — it uses `key_hash`, not the ciphertext).
- Not an admin-session-forgery bug: sessions are opaque DB rows, not JWTs signed
  with this key. Impact is confined to API-key at-rest encryption.

### O-2 (S1) — the `/cluster/sync` IP allowlist is NOT in the running nginx config
`projects-locations.d/llm-proxy2.conf` has the allowlist, but `nginx.conf` flattened
those includes inline and the flattened block has no such location
(`nginx -T | grep -c cluster/sync` → 0). So `/llm-proxy2/cluster/sync` (and the
`/llm-proxy/…` rewrite path) reach the app from anywhere, gated only by the cluster
HMAC — whose secret is 19 chars (< the code's own 32 floor; the app logs this at
every boot). `apply_sync` writes providers, api_keys, users, settings.
- **Do:** re-add the exact-match allowlist location into the authoritative
  `nginx.conf` (and deny `/llm-proxy/cluster/`); then resolve the include drift so
  nobody edits a file that is not being served. Reload nginx.

### O-3 (S1/S2) — rotate the short/shared/hardcoded secrets, move them to `${VAR}`
- `CLUSTER_SYNC_SECRET` 19 chars → rotate to 32+ random bytes on **both** nodes.
- Three literals in `docker-compose.yml` bypass the `${VAR}` convention the rest of
  the file uses: `AIRI_WHISPER_BRIDGE_TOKEN`, `AI_PROVIDER_SUPERVISOR_INTERNAL_API_KEY`
  (a live `llmp-` gateway key sitting in plaintext in a backup-prone path), and the
  smoke instance's `CLUSTER_SYNC_SECRET`. Move to `${VAR:?}` sourced from `.env`,
  rotate, GPG-seal.
- `WEBHOOK_SIGNING_SECRET` is unset, so caller-facing webhooks are signed with the
  *cluster* key (`app/api/webhook.py` fallback) — a free offline oracle against the
  cluster secret. Set an independent 32+ byte value; the fallback branch should then
  be deleted in source (follow-up code change, tracked separately).

### O-4 (S2) — client IP is attacker-controlled (nginx side)
nginx appends to `X-Forwarded-For` (`$proxy_add_x_forwarded_for`) and the app trusts
the **leftmost** hop, so `X-Forwarded-For: 1.2.3.4` bypasses the IP block list,
defeats the login throttle, and forges audit attribution. There is no edge
`limit_req` on the login path.
- **Do (infra):** set `proxy_set_header X-Forwarded-For $remote_addr;` on the
  llm-proxy2 locations (nginx is the only trusted hop) and add `limit_req` to
  `/llm-proxy2/login` + `/llm-proxy2/api/auth/*`.
- **Paired code follow-up (tracked):** have `extract_client_ip_from_request` prefer
  `X-Real-IP` (nginx sets it to `$remote_addr`, overwrite-not-append) over XFF-left.
  Deliberately NOT changed unilaterally — it must land together with the nginx change
  or it shifts rate-limiting/attribution behaviour.

### O-5 (S2/S3) — hardening follow-ups
- **Session cookie `Path=/`** leaks the admin session to every app on the host.
  Set `SESSION_COOKIE_PATH=/llm-proxy2` in compose on both nodes (and a paired source
  default change). Consider `SameSite=Strict` for the admin UI.
- **Session lifecycle:** password change / role change / delete do **not** invalidate
  existing sessions, and `role` is cached in the session row (demoted admin keeps
  admin for up to 7 days). This is a source change (invalidate on those events +
  re-read `User` in `_get_session`) — tracked for a follow-up, needs a careful test.
- **`admin`/`admin` bootstrap** regenerates whenever the users table is empty
  (soft-delete every user → next restart re-creates it) and never forces rotation.
  Source change tracked; combined with O-4's missing throttle this is the top
  fresh-deploy compromise path.
- **Dependencies unpinned** (~110 Python packages all `>=`, no lockfile/hashes) and
  **no CVE scanning in CI.** Add a hash-pinned lockfile + `pip-audit`/`npm audit`
  CI job + Dependabot. (Frontend already uses `npm ci` against a lockfile — good.)
- **`python-jose` + `passlib`** are declared but never imported; jose drags in
  `ecdsa` (CVE-2024-23342, won't-fix, unreachable here). Remove both from
  `requirements.txt`/`pyproject.toml` (source follow-up).
- **grok/whisper bridges run as root** on a mutable-tag base; `/grok-bridge/api/chat`
  has no `auth_request` at the edge. Add non-root `USER`, digest-pin bases, and an
  `auth_request`/IP-allowlist on the chat location.
- **Edge headers:** admin UI has HSTS only — add `X-Frame-Options`/CSP
  `frame-ancestors`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`.
- **CI:** add an explicit `permissions: contents: read`; pin actions by SHA.
- **Leaked 2026-08 admin password** remains in public git history (incl. committed
  `.pyc` in old commits). Treat as permanently public — confirm it is rotated
  everywhere. The secret scanner only sees the current tree; add a history/pickaxe
  pass.

### O-6 (S1, needs design sign-off) — BUG-109: the compliance audit chain does not protect what it claims
This is the gov-compliance evidentiary control, so I deliberately did **not**
rush a patch — changing the hash input is a versioned migration affecting every
prior day. Confirmed defects in `app/compliance/audit.py`:
- **Only 5 of 14 columns are hashed** (`id|audit_id|api_key_id|event_type|http_status`).
  `requested_model`, `served_model`, `served_provider_id`, `blocked_company`,
  `reason_code`, `matched_pattern`, `client_identity`, `created_at` etc. are **not**,
  so `UPDATE compliance_events SET blocked_company=NULL, served_model='…' WHERE …`
  rewrites the substance of an event and the stored `chain_hash` still matches.
- **Nothing ever verifies** the chain (no endpoint/worker/test recomputes),
  recomputation is **short-circuited** to the stored hash, and gaps **re-anchor
  silently** at `""`. `compliance_policy_changes` has **no chain at all**.
- Feeder bugs already fixed: BUG-106 (peer PK injection into a signed day) and
  BUG-105 (purge over-delete) both fed this.

Proposed staged plan (non-destructive):
1. Add `chain_version` to the chain row. Keep existing days valid under v1.
2. v2 hashes **all** columns, length-prefixed/JSON-canonical framed, and HMACs with
   a key the app holds but admins/DB do not (or publishes the daily tip off-box —
   S3 Object Lock / email to you).
3. Add `GET /api/admin/compliance/verify-chain?from=&to=` that recomputes and
   reports the first divergence; make `compute_daily_integrity_hash`
   recompute-and-compare instead of returning the stored value.
4. Store an explicit `prior_day` pointer; treat a missing prior as an error.
5. Extend the chain to `compliance_policy_changes`.
I can implement this on your go-ahead; it wants your sign-off because step 2
changes the attestation and step 1 fixes the format of the evidentiary record.

### O-7 (S1, decision) — provider & OAuth credentials are stored PLAINTEXT
`providers.api_key`, `providers.oauth_refresh_token`, and the `oauth_capture_logs`
bodies are cleartext in `llmproxy.db` (the schema comment claiming "encrypted
Fernet" was wrong — corrected in code). Combined with O-1 (public-default
`SECRET_KEY`) the whole DB is an unprotected upstream-credential + OAuth-token
store. **Decide:** encrypt these columns with a KMS/env key distinct from
`SECRET_KEY`, **or** formally document the DB as a cleartext credential store in
`docs/backup-plan.md` and treat it accordingly (0600, encrypted backups only, never
on the `/mnt/s` NFS share — note the stale `llmproxy.db.snap.bak-*` files beside the
live DB and the 1.1 GB v5.0.x snapshots in `~/backups/archive-snapshots/`). Also:
`oauth_capture_logs` has no prune/TTL and stores `authorization`/`cookie` headers —
add redaction + a short TTL (both are source follow-ups I can do on request).

### Open questions for you (I did not decide these)
- **Webhook private-IP policy:** I kept RFC1918 allowed (your documented decision —
  internal hubs) and blocked only loopback + link-local. Say if you want the full
  private-range block instead.
- **`/api/oauth-capture/*`** trusts "no `X-Forwarded-For`" as internal. The capture
  sidecars rely on that, so I did not change the app gate — the right fix is an nginx
  network restriction on that location. Confirm before I touch the trust model.

---

## Checked and found correct (so you know they were examined)
API-key compare is hash-equality (not a timing `==`); disabled/tombstoned/over-budget
keys enforced on one shared path incl. streaming + OpenAI-shaped; bcrypt cost 12,
256-bit session tokens, logout kills the server-side row; login enumeration-safe;
password-reset flow solid; no ordinary-key → admin escalation; no IDOR in
memory/runs/lmrh scopes; CORS refuses wildcard+credentials; OAuth/SSO state+PKCE+nonce
CSRF-protected; audit export download is admin-only + confined; no frontend XSS sink;
main Dockerfile is non-root with a separated build stage and no build secrets;
.gitignore/.dockerignore thorough; no secret values logged; container privilege
posture clean (no privileged/host-net/docker-sock on the three llm-proxy2 services).

## Not covered / per-node
tmrwww02 must be checked separately for O-1..O-5 (per-node secrets + its own
`nginx.conf.www2`). `c1conversations-avaya-01-s23` is off limits and was not touched.
Downstream consumer deployments likely inherit the O-1/O-2/O-3 compose pattern — check
their `SECRET_KEY`/`CLUSTER_SYNC_SECRET` first.
