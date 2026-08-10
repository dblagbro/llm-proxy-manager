# Testing — llm-proxy-v2

Pointer + quality-gate summary. The full strategy and case detail live in the root **`test-plan.md`**
and **`qa-notes.md`** — this file does not duplicate them.

## Layout
- `tests/unit/` — ~329 files. Fast, hermetic-ish. Run: `make test` (`pytest tests/unit -v`).
- `tests/integration/` — ~15 files, incl. Playwright UI (`test_playwright_ui.py`, runs against the
  live `/llm-proxy2/`). Run: `make test-all` (`pytest tests/ -v`).
- Known-red backlog: **`tests/known_failures.txt`** (64 pre-existing failures as of 2026-08-05).
  Distinguish NEW regressions from these; do not grow the list.

## How to run
- Lint (must be clean): `make lint` (`ruff check` + `ruff format --check`).
- Import smoke: `python -c "import app.main"`.
- Focused area: `pytest tests/unit/test_<...>.py -q`.

## CI gates (`.github/workflows/ci.yml`)
- **Gating (blocks):** import smoke + 4 invariant/watchdog tests only. Narrow by design today.
- **Non-gating (informational):** full unit suite (`continue-on-error`) — red until
  `known_failures.txt` reaches zero, then promote to a required check.
- Weakness tracked in `docs/current-state.md` + the recovery assessment. Strengthening the gate is
  on the roadmap.

## Definition of done (see `AGENTS.md`)
`make lint` clean · touched-area `make test` green with no new failures vs `known_failures.txt` ·
import smoke ok · a test for new behavior · version bump for shippable changes · docs updated.

## Test types by change (guidance)
- Routing/provider change → provider-selection unit tests + wire-format (Anthropic + OpenAI) checks.
- Streaming/DB change → pool + thread + `/health` behavior under load; **soak** (leaks manifest over
  time). See `architecture.md` "DB pool leak diagnostic path".
- UI change → Playwright. Auth/compliance change → authz + audit + `security-reviewer`.
