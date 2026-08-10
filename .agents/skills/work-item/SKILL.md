---
name: work-item
description: Execute a single bounded change on llm-proxy-v2 end to end — scope, explore for reuse, implement, verify against the definition of done, and update docs. Use for a normal feature/fix once the approach is clear.
---

Drive one bounded work item from intent to a verified, reviewable change. If the approach is
ambiguous or has wide blast radius, stop and use `architect` first.

## Flow
1. **Scope** — state the single outcome + acceptance criteria. If it grows, split it.
2. **Explore for reuse** — search for existing functions/patterns/utilities (`cartographer` or
   grep). Prefer reuse over new code. Respect the layering in `design.md`
   (`api/` thin → `routing/`/`cot/` → `monitoring/`/`cluster/`/`providers/` → `models/`/`config/`).
3. **Implement** — the minimum change; match surrounding style; obey `AGENTS.md` constraints
   (Python 3.13: no `passlib`, no `await` in genexprs; no secrets; sub-path invariants).
4. **Test** — add/adjust a test for the new behavior.
5. **Verify (definition of done):** `make lint` clean; `make test` green for touched areas without
   regressing `tests/known_failures.txt`; `python -c "import app.main"` ok; bump
   `app/__version__.py` for a shippable change. Paste the actual command output.
6. **Docs** — update `architecture.md`/`docs/current-state.md`/`CHANGELOG.md` as affected.

## Output
1. What changed (files + why). 2. Verification commands + results. 3. Docs/version touched.
4. Follow-ups/risks. Stop before commit/push/deploy — those need explicit approval.

## Guardrails
- Bounded scope only; no opportunistic refactors (use `refactor-*` skills for that).
- Never claim green without running the command. Never push/tag/deploy here.
