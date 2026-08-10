# Project map — llm-proxy-v2

A concise onboarding map. Not an exhaustive file listing — for detail read the code, `design.md`
(layering contract), and `architecture.md` (current state).

## Entry points
- **App:** `app/main.py` (FastAPI app factory; routers, startup workers, SIGUSR2 handler).
- **HTTP surface:** `app/api/` — `messages.py` (`/v1/messages`, Anthropic), `completions.py`
  (`/v1/chat/completions`, OpenAI), `/v1/responses`, plus admin/cluster/monitoring routers.
- **Frontend:** `frontend/` (React/Vite dashboard, served under `/llm-proxy2/`).
- **Version:** `app/__version__.py` (single source of truth).

## Domains (app/ subpackages) — one-line ownership
| Package | Owns |
|---|---|
| `api/` | HTTP shape only: parse → call core → format. No business logic. |
| `routing/` (+`lmrh/`) | Provider selection, capability gate, circuit breakers, LMRH protocol. |
| `cot/` | Chain-of-thought pipeline. |
| `compliance/` | Per-key + system enforcement, audit chain, policy push. |
| `providers/` | Vendor quirks: catalog scanner, OAuth flows (claude/cursor/codex/grok-web/cohere). |
| `cluster/` | Peer sync (HMAC + LWW), oauth-pull. |
| `monitoring/` | Metrics, activity log, keepalive, ~25 background workers, pool-leak watcher. |
| `models/` | SQLAlchemy ORM + DB engine/session (`models/database.py`). |
| `config/` | Pydantic settings (env) + runtime overrides. |
| `auth/ budget/ cache/ capability_scout/ mcp_server/ memory/ middleware/ observability/ privacy/ proxy_tools/ runs/ airi/ integration/ utils/` | Supporting subsystems. |

**Dependency direction (never call upward):**
`api/ → routing/ + cot/ → monitoring/ + cluster/ + providers/ → models/ + config/`.

## Runtime boundaries
- Sub-path deploy: everything under `/llm-proxy2/` (see sub-path invariants in `AGENTS.md`).
- Single async event loop (uvicorn, 1 worker); SQLite (aiosqlite, one OS thread per connection)
  at `/app/data/llmproxy.db`; two sidecars: grok-bridge (Playwright) + cursor-bridge.
- Cluster: tmrwww01 + tmrwww02; peers sync provider/config via LWW (per-node values must match).

## Tests
- `tests/unit/` (~329 files), `tests/integration/` (~15, incl. Playwright). Known-red list:
  `tests/known_failures.txt`. Run: `make test` (unit) / `make test-all` (full).

## Key commands
`make install | dev | test | test-all | lint | format | migrate | build`. Deploy is NOT `make up`
— see `AGENTS.md` (canonical stack at `/home/dblagbro/docker/`, rolling, nginx reload).
