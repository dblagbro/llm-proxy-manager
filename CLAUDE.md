# llm-proxy v2 — Claude Code Guide

## What this is
Python/FastAPI rewrite of llm-proxy v1. Served at `/llm-proxy2/` on 3 nodes via the main
nginx + docker-compose stack at `/home/dblagbro/docker/`.

## Current state (2026-06-04)
- Live version: **v5.0.15** on all 3 dev-cluster nodes + smoke
- **v5.0.x compliance enforcement shipped** (v5.0.0 → v5.0.15 over 2026-06-03/04). New `app/compliance/` subpackage; 3 new tables (`compliance_events`, `compliance_policy_changes`, `compliance_audit_chain`); 6 new columns; `/v1/responses` translation shim; `/api/admin/policy-snapshot` for the hub team's hub-side enforcement layer. v5.0.9 + v5.0.10: incremental refactor sweep (messages.py + completions.py shrunk via `_compliance_handler.py` extraction; sync.py 1024 → 573 LOC via `_apply_api_keys` + `_apply_providers` extraction). v5.0.11: limits + compliance edit modal merged. v5.0.12: dropped mid-stream `event: budget` SSE frame (hub-team-filed, Vercel-AI-SDK consumers). v5.0.13: `ComplianceEvent.matched_pattern` now carries rejected path for `path_not_allowed` rows. v5.0.14: `/metrics` Accept-header-disambiguates between Prometheus and React SPA. v5.0.15: rotation also clamps on `five_hour_utilization` (caught the VG session-max incident).
- **READ FIRST when resuming this project:**
  - `architecture.md` (has a full `## Compliance enforcement (v5.0.0+)` section now)
  - Spec set: `docs/5.0-compliance-design.md`, `docs/5.0-impact-map.md`, `docs/compliance-taxonomy-v5.0.0.md`
  - Hub-team memo trail: `docs/2026-06-04-reply-{1..5}-to-hub-team-*.md` (chronological — read in order to understand the back-and-forth that led to v5.0.7)
- **Open items watched by monitor:**
  - Hub team's bot canary delayed by their daemon-cache bug (COORDINATOR_AGENT_CLI cached at boot vs read-at-runtime). Zero hub-key traffic yet.
  - Hub v2.1.0 hub-side enforcement build pending (polls `/api/admin/policy-snapshot` we shipped in v5.0.7).

## Deployment topology
- **dev cluster (3 nodes):** `tmrwww01`, `tmrwww02`, `c1conversations-avaya-01-s23`. Operator uses these for development; if proxy work breaks them, only dev users affected.
- **smoke instance:** `llm-proxy2-smoke` container on tmrwww01, served at `/llm-proxy2-smoke/`. `CLUSTER_ENABLED=false`, separate DB, separate volume. Used as the sandbox for downstream teams (e.g., Coordinator Hub) to validate before promotion.
- **production (downstream):** separate deployments operated by consumer teams (gov-compliance use case lives here). Pull QA'd Docker Hub image (`dblagbro/llm-proxy-manager:<version>`).

## Sidecars (in compose, isolated from each other)
- `llm-proxy2-grok-bridge` — grok.com Playwright session holder (grok-web provider)
- `llm-proxy2-cursor-bridge` — Cursor-To-OpenAI adapter (cursor-oauth provider, v4.4.31+)

## Critical paths
- App source: `/home/dblagbro/llm-proxy-v2/`
- Docker compose: `/home/dblagbro/docker/docker-compose.yml` (container: `llm-proxy2`)
- nginx location: `/home/dblagbro/docker/config/nginx/projects-locations.d/llm-proxy2.conf`
- Frontend build output: `frontend/dist/` (built inside Docker image)

## Sub-path deployment
The app runs at `/llm-proxy2/`, NOT at root. Three things must stay in sync:
1. `frontend/vite.config.ts` — `base: '/llm-proxy2/'`
2. `frontend/src/App.tsx` — `<BrowserRouter basename="/llm-proxy2">`
3. `frontend/src/api/client.ts` — `const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')`

Breaking any of these causes API calls to go to the wrong nginx location or React Router
to resolve paths incorrectly.

## Docker rules (from global CLAUDE.md)
- NEVER run `docker compose down` or touch other containers
- **ALWAYS `cd /home/dblagbro/docker` first.** The repo root used to
  hold a `docker-compose.yml` that only defined `llm-proxy2` and
  silently masked the canonical stack — see BUG-056. As of v5.0.21+
  remediation Batch 1, that file was renamed to
  `docker-compose.yml.example.dev`. Compose commands from anywhere
  other than `/home/dblagbro/docker/` will either fail explicitly OR
  pick up an unrelated dev file. Both are wrong.
- To rebuild: `cd /home/dblagbro/docker && sudo docker compose build llm-proxy2 && sudo docker compose up -d --force-recreate --no-deps llm-proxy2`
- To reload nginx: `sudo docker exec nginx nginx -s reload`

## Python 3.13 notes
- Do NOT use `passlib` — it crashes on Python 3.13. Use `bcrypt` package directly (see `app/auth/admin.py`)
- Do NOT use `await` inside generator expressions — use explicit async for loops

## FastAPI SPA routing
- `/assets` is mounted as StaticFiles for JS/CSS
- All other paths use `/{full_path:path}` catch-all returning `FileResponse(index.html)`
- `StaticFiles(html=True)` at root does NOT work for SPA routing — don't use it

## Testing
- Integration tests: `python -m pytest tests/integration/test_playwright_ui.py -v`
- Tests run against live deployment at https://www.voipguru.org/llm-proxy2/
- Install playwright first: `playwright install chromium`
- Each test gets its own browser context (no shared cookie state between tests)

## Default credentials
- admin / admin (created on first boot if no users exist)

## v5.0.x findings to remember
- **Slow-degradation cluster sync bug (v5.0.5 fix).** Pre-v5.0.5 `apply_sync` wrapped 12+ table sub-applies in ONE transaction → 19.6s SQLite write lock → "database is locked" on per-request writers. Now committed per-section. If sync timings creep back over ~5s avg, suspect a new section was added without its `_section_commit("label")` call.
- **Audit field mislabeling (v5.0.6 fix).** `compliance_events.requested_model` used to capture the SERVED model because `body["model"]` is rewritten at messages.py:355 BEFORE the audit row writes. Fix: capture `_orig_request_model = body.get("model")` at the top of the handler. Static-grep tests in `test_v506_audit_preserves_requested_model.py` catch regressions.
- **Bedrock-Anthropic dual-tag (v5.0.1 fix).** `anthropic.claude-*` is in BOTH `anthropic.model_prefixes` AND `aws.model_prefixes`; `model_family_companies()` returns the SET so banning either company drops Bedrock-Anthropic providers. `model_family_to_company()` (singular) returns only the first match — used for disclosure surfaces where one label is enough.
- **Hub team coordination is via the operator.** All memos go `proxy team → operator → hub team` (and back). I cannot send memos directly. The operator is the human-trust point for both sides. Draft, never send.
- **OpenCode UA is `opencode/<semver>` empirically.** Captured 2026-06-04 against opencode-ai@1.15.13. Test pin in `test_v5_opencode_ua_compatibility.py` prevents accidental taxonomy refinements from false-positiving on it.
