# llm-proxy v2 — Claude Code Guide

> **Canonical project contract: `AGENTS.md`** (imported below). It owns the approved commands,
> architecture boundaries, definition of done, and the prohibited/approval-required list — for
> all agents. This file adds only Claude-specific notes and hard-won gotchas; on any conflict,
> `AGENTS.md` wins. Live status is in `docs/current-state.md`; the agent roster + model-routing
> policy in `docs/agent-system.md`.

@AGENTS.md

## Cross-project knowledge base (Devin's personal KB)

**Attach this MCP server at session start** for cross-project institutional rules Devin has set:

- **MCP endpoint:** `https://www.voipguru.org/kb-mcp/mcp`
- **Web UI:** `https://www.voipguru.org/gitea/dblagbro/projects-kb` (private — auth via Gitea)
- **Local vault (on tmrwww01):** `/home/dblagbro/projects-kb/` (git-cloned)

**Highest-priority reads** — the `rules/` directory contains locked cross-project rules ("no writes to hub KB when in project role", "strict cluster separation", "no manufactured memos", etc.). Query them via the MCP `search` or `list_documents` tool at start of any non-trivial task.

**Not to be confused with:** the coordinator-hub KB (`coordinator-kb` CLI). That's the hub team's territory for their remote bots. This KB is Devin's cross-project institutional knowledge for the projects he owns.

## What this is
Python/FastAPI rewrite of llm-proxy v1. Served at `/llm-proxy2/` on 3 nodes via the main
nginx + docker-compose stack at `/home/dblagbro/docker/`.

## Current state
**Live status is now maintained in `docs/current-state.md`** (version, branch/last-good-commit,
what works/doesn't, active risks incl. the open DB-connection-hold leak, next actions). The old
inline "v5.0.15" snapshot here had drifted badly (actual is v5.22.x) — do not trust a hardcoded
version in this file; read `docs/current-state.md`.

- **READ FIRST when resuming this project:** `architecture.md` (canonical) + `design.md`
  (module-boundary contract), then `docs/current-state.md`.
- Compliance spec set (still current): `docs/5.0-compliance-design.md`, `docs/5.0-impact-map.md`,
  `docs/compliance-taxonomy-v5.0.0.md`.
- Hub-team memo trail (historical context for v5.0.7): `docs/2026-06-04-reply-{1..5}-to-hub-team-*.md`.

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
