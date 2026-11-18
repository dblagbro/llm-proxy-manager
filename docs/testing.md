# Testing — llm-proxy-v2

Pointer + quality-gate summary. The full strategy and case detail live in the root **`test-plan.md`**
and **`qa-notes.md`** — this file does not duplicate them.

## Layout
- `tests/unit/`: 378 test files (2026-11-18). Hermetic: no outbound connections, asserted in CI.
  Run: `make test` (`pytest tests/unit -v`).
- `tests/integration/`: 14 test files, incl. Playwright UI (`test_playwright_ui.py`). They skip
  unless a target is chosen: `LLMPROXY_TEST_EPHEMERAL=1` boots a private throwaway instance
  (`tests/_ephemeral.py`, what CI uses); `LLMPROXY_TEST_LIVE=1` + `LLMPROXY_TEST_BASE_URL=<url>`
  aims at a real deployment on purpose. Nothing targets production by default (`tests/conftest.py`).
- **`tests/known_failures.txt` is empty** (since v5.22.37) and must stay empty. Its header also
  records the few tests that fail only inside the production image. Triage lists for the other
  suites: `tests/known_integration_failures.txt`, `tests/known_ui_failures.txt`.

## How to run
- Lint (must be clean): `make lint` (`ruff check` + `ruff format --check`).
- Import smoke: `python -c "import app.main"`.
- Focused area: `pytest tests/unit/test_<...>.py -q`.

## CI gates (`.github/workflows/`)
`ci.yml` runs on every push to `main` and every pull request to `main`. All four jobs block
(Python 3.13, `ubuntu-24.04`, pinned action versions):

- **`guard`**: builds the frontend (`npm ci` + `npm run build`), import smoke
  (`python -c "import app.main"`), the **full unit suite** (`pytest tests/unit -q`, blocking since
  v5.22.38), then `tools/run_unit_suite_hermetic.py`, which fails the build on any connect to a
  non-loopback address.
- **`secrets`**: `tools/secret_scan.py` over tracked files, plus a check that no forbidden path
  (`.env*`, key material, databases, dumps, `data/`, `backups/`) is tracked.
- **`integration-ephemeral`**: `tests/integration` with `LLMPROXY_TEST_EPHEMERAL=1`, excluding the
  browser-driven files and deselecting the entries in `tests/known_integration_failures.txt`; then
  `tools/check_integration_hermetic.py`.
- **`playwright-ui`**: builds the frontend, installs Chromium, runs
  `tests/integration/test_playwright_ui.py` against the ephemeral instance (12-minute job cap,
  120 s per-test timeout), deselecting the entries in `tests/known_ui_failures.txt`.

Path-filtered workflows run only when their area (or the `Makefile`) changes:
`gateway-offline-recovery.yml` (`make test-gateway-recovery lint-gateway-recovery`),
`grok-bridge-security.yml` (`make test-grok lint-grok lint-grok-browser test-grok-release
lint-grok-release`, plus a host-compatibility job), `whisper-runtime.yml` (`make lint-whisper
build-whisper test-whisper`).

Not in CI: `make lint` for the gateway (`app/`, `tests/`). The inherited lint findings are tracked
debt (see `AGENTS.md`). Local checks that mirror CI: `make verify-ci` (unit suite on the image's
Python 3.13), `make secret-scan`, and the `.githooks/` pre-commit and pre-push hooks
(`make install-hooks`).

## Definition of done (see `AGENTS.md`)
`make lint` clean · touched-area `make test` green with no new failures vs `known_failures.txt` ·
import smoke ok · a test for new behavior · version bump for shippable changes · docs updated.

## Test types by change (guidance)
- Routing/provider change → provider-selection unit tests + wire-format (Anthropic + OpenAI) checks.
- Streaming/DB change → pool + thread + `/health` behavior under load; **soak** (leaks manifest over
  time). See `architecture.md` "DB pool leak diagnostic path".
- UI change → Playwright. Auth/compliance change → authz + audit + `security-reviewer`.
