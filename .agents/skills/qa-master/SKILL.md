---
name: qa-master
description: Comprehensive independent QA pass on llm-proxy-v2 before a release or after a significant change — plan coverage across dimensions, run it, and report defects with evidence without silently fixing them.
---

Independent, evidence-driven verification separate from the change author. Delegate execution to
the `qa-engineer` agent for context isolation on large passes.

## Dimensions (pick those relevant to the change)
- **Unit/regression:** `pytest tests/unit -q` — separate NEW regressions from `tests/known_failures.txt`.
- **Integration/API:** wire-format correctness for Anthropic (`/v1/messages`) and OpenAI
  (`/v1/chat/completions`, `/v1/responses`) shapes; provider routing/failover; empty-completion guards.
- **Playwright UI:** `tests/integration/test_playwright_ui.py` against the live `/llm-proxy2/`.
- **Reliability/perf:** DB pool + thread behavior under load, `/health` latency, streaming (SSE) lifetime
  (a known leak class — see `architecture.md` "DB pool leak diagnostic path" + `docs/recovery/`).
- **Security-adjacent:** authz on admin/cluster endpoints, secret non-leakage (→ `security-reviewer`).

## Rules
- Test against `AGENTS.md` DoD + `docs/testing.md`/`test-plan.md`.
- **Never** touch real conversations/provider data; **never** mutate the operator's admin user rows.
- Report reproducible failures (commands + expected vs actual + severity). **Do not fix product code**
  during an independent pass — a failing test that reveals a real defect is a finding.
- Soak for slow-degradation bugs (pool/thread leaks manifest over time, not in a quick check).

## Output (→ feeds `docs/testing.md` / release-readiness)
1. Coverage matrix (dimension → run → result). 2. Defects with repro + severity. 3. New vs known-failure
delta. 4. Coverage gaps + recommended tests. 5. Go/No-Go recommendation for release.
