# AGENTS.md — llm-proxy-v2 project contract

Canonical, always-loaded contract for any coding agent (Claude Code, Codex, Gemini CLI, Copilot).
Keep this concise. Long procedures live in Agent Skills (`.agents/skills/`) and `docs/`.

Adapters: `CLAUDE.md` (Claude) and `GEMINI.md` (Gemini) import this file and add only tool-specific notes.

## What this is
A self-hosted FastAPI (Python 3.13) multi-provider LLM routing gateway. Accepts Anthropic
(`/v1/messages`), OpenAI (`/v1/chat/completions`), and OpenAI Responses (`/v1/responses`)
shapes, routes to the best available upstream via litellm + the LMRH protocol, and returns
the caller's expected wire format. Ships a per-key + system-wide compliance enforcement layer.
Served at sub-path `/llm-proxy2/` on tmrwww01 + tmrwww02; distributed as Docker Hub image
`dblagbro/llm-proxy-manager`. **Design north star: an operator can read it end-to-end in an
afternoon — optimize for legibility, not cleverness.**

## Repository layout & ownership
- `app/` — application. Key subpackages (see `design.md` for the layering contract):
  - `api/` — HTTP shape only: parse → call core → format. No business logic.
  - `routing/` — provider selection + LMRH protocol (pure logic); `routing/lmrh/`.
  - `cot/` — chain-of-thought pipeline. `compliance/` — enforcement + audit chain.
  - `providers/` — vendor quirks: scanner, OAuth flows (claude/cursor/codex/grok-web/cohere).
  - `cluster/` — peer sync (HMAC + LWW). `monitoring/` — metrics, activity log, keepalive, workers.
  - `models/` — SQLAlchemy ORM + DB session (`models/database.py`). `config/` — Pydantic settings.
  - others: `auth/ budget/ cache/ capability_scout/ mcp_server/ memory/ middleware/ observability/ privacy/ proxy_tools/ runs/ airi/ integration/ utils/`.
- `frontend/` — React/Vite dashboard (built into the image, served under `/llm-proxy2/`).
- `tests/` — `tests/unit/` (~329 files) + `tests/integration/` (~15, incl. Playwright).
- `docs/` — durable memory (see "Documentation map"). Root `architecture.md` + `design.md` are canonical.

## Approved commands (use the Makefile — do not invent commands)
| Task | Command |
|---|---|
| Install (editable + dev) | `make install` (`pip install -e ".[dev]"`) |
| Run locally | `make dev` (`uvicorn app.main:app --reload --port 3000`) |
| Fast unit tests | `make test` (`pytest tests/unit -v`) |
| Full suite | `make test-all` (`pytest tests/ -v`) |
| Lint | `make lint` (`ruff check` + `ruff format --check`) |
| Format | `make format` (`ruff format`) |
| DB migrate / new revision | `make migrate` / `make migrate-new MSG="..."` (alembic) |
| Build image (local) | `make build` |
| Import smoke | `python -c "import app.main"` |

**Deploy (TMR) is NOT `make up`.** The canonical compose stack lives at `/home/dblagbro/docker/`
(not the repo — see CLAUDE.md BUG-056). Rolling, one node at a time:
`cd /home/dblagbro/docker && sudo docker compose build llm-proxy2 && sudo docker compose up -d --force-recreate --no-deps llm-proxy2 && sudo docker exec nginx nginx -s reload`.
Version bump requires editing `app/__version__.py`. Deploy/push/tag require explicit approval.

## Architecture boundaries (dependency direction is top→down)
`api/ → routing/ + cot/ → monitoring/ + cluster/ + providers/ → models/ + config/`.
Never call upward. `api/` holds no business logic. Provider quirks stay in `providers/`.
Full contract: `design.md`. Current state of the code: `architecture.md`.

## Coding & security constraints
- Python 3.13: **do not use `passlib`** (crashes) — use `bcrypt` directly. No `await` inside generator expressions.
- Secrets: never hardcode, log, or commit credentials/tokens. Use env/Pydantic settings. Do not print OAuth tokens or cluster HMAC keys.
- Sub-path deploy invariants (breaking any sends API calls to the wrong nginx location): `frontend/vite.config.ts` `base:'/llm-proxy2/'`; `frontend/src/App.tsx` `<BrowserRouter basename="/llm-proxy2">`; `frontend/src/api/client.ts` `BASE = import.meta.env.BASE_URL.replace(/\/$/,'')`.
- Match surrounding style; keep changes bounded and reviewable. `api/` stays thin.

## Definition of done
1. `make lint` clean; `make test` green for touched areas (respect `tests/known_failures.txt` — do not regress it).
2. `python -c "import app.main"` succeeds.
3. New behavior has a test; the version pin (`app/__version__.py`) is bumped for a shippable change.
4. Affected docs updated (esp. `architecture.md`, `docs/current-state.md`, `CHANGELOG.md`).
5. No secrets added; no sub-path invariant broken; no unrelated changes.

## Prohibited / approval-required operations
- **NEVER** `docker compose down`, `docker compose down -v`, `docker volume rm`, or anything stopping the full stack / destroying volumes. Target single containers by name only (`make down-container` is llm-proxy2-only).
- **No push, tag, publish, or deploy without explicit approval.** No remote/cluster/DB mutation on peers without approval.
- Per-node DB changes must be applied to each node (cluster sync propagates `extra_config`/providers via LWW — divergent per-node values ping-pong).
- Do not touch other teams' repos or the coordinator-hub KB from this project role.
- Commits to this repo carry **no Claude attribution** (no `Co-Authored-By`, no Claude in contributors).

## Documentation map (reuse these — do not duplicate)
- Charter/onboarding: `docs/project.md`, `docs/project-map.md` · Live status: `docs/current-state.md`
- Architecture (canonical): `architecture.md` · Design contract: `design.md` · Roadmap: `docs/roadmap.md`
- Testing: `docs/testing.md` → root `test-plan.md`, `qa-notes.md` · History: `CHANGELOG.md`, `bug-log.md`, `refactor-log.md`
- Ops/recovery: `docs/backup-plan.md`, `docs/remediation-plan.md`, `docs/recovery/`, runbooks in `docs/*runbook*.md`
- Agent system + model routing: `docs/agent-system.md` · ADR/RFC: `docs/rfc/`

## Agent skills & specialized agents
On-demand skills in `.agents/skills/` (mirrored to `.claude/skills/`): `session-start`, `work-item`,
`project-recovery`, `qa-master`, `release-gate`, `project-bootstrap`, `agent-system-setup`.
Specialized Claude agents in `.claude/agents/` (cartographer, architect, implementer, debugger,
qa-engineer, security-reviewer, release-engineer, platform-engineer, market-analyst) — see
`docs/agent-system.md` for triggers, scope, model tier, and permissions.
