# #507 — DAG-driven hook pipeline refactor (parked)

**Status:** design + rationale — parked until we have 4-5 hooks to justify the ceremony.
**Driver:** ccproxy's typed DAG pipeline is a real upgrade on our current priority-list. Right shape once we have enough hooks; premature today with only 1 (compliance substitution).

## Current state (v5.14.x)

`app/api/_response_hook_runner.py` runs hooks in a sorted list by `priority`. Every hook gets the same `HookContext` and emits response headers. One built-in hook today: `compliance_substitution_header_hook`. Hub-team can register more via the callback registry.

## Proposed DAG shape

Each hook declares `inputs` + `outputs`:

```python
register_hook(
    name="compliance_substitution",
    fn=compliance_substitution_header_hook,
    inputs={"substituted", "key_record"},
    outputs={"X-Compliance-Substitution"},
)
```

Registry builds a topological sort at boot. Hooks with disjoint I/O run in parallel. A hook whose declared input isn't produced by any upstream hook fails registration (structural guarantee that "hook silently skipped" is impossible).

**Concrete gains once we have ≥4 hooks:**
1. **Parallelism** — hooks with no shared I/O run concurrently instead of serially. Latency improvement scales with hook count.
2. **Structural safety** — impossible to ship a hook whose declared input nobody produces. Compile-time catch instead of runtime silent-skip.
3. **Explicit dependencies** — reviewing a new hook's PR shows exactly which upstream data it needs, not just "priority=10 because reasons."

## Why parked

Cost/benefit at hook count N:
- N=1: pure ceremony, no gain
- N=2: marginal gain, still not worth
- N=3: break-even (parallelism starts helping)
- N=4-5: clear win (safety catches justify themselves)
- N≥6: shape-defining

We're at 1. Cost of the refactor is real (~300-500 LOC + migration of the existing hook's registration). Waiting for the hub team + DevinGPT to register 2-3 more hooks before pulling the trigger.

## Trigger criteria to un-park

Any of:
1. Hub team registers a compliance-mirror hook that reads `X-Compliance-Substitution` (would force us to reason about hook ordering explicitly — DAG becomes the natural expression).
2. DevinGPT registers ≥2 hooks (they've floated intent).
3. A hook-related incident (silent skip, ordering surprise, ordering rollback) that would have been caught structurally by a DAG.

## Slice preview (when un-parked)

1. Add `inputs` + `outputs` args to `register_hook`. Default to empty sets = "no declared dependencies" — backward compat with the v5.14 priority-list mode.
2. New `dag_mode` setting (default `false`). When on: registry uses topological sort instead of priority.
3. Parallel execution via `asyncio.gather` on siblings within a topological level.
4. Migration path: mark each existing hook's I/O without changing behavior (still priority-sorted). Flip to `dag_mode=true` on a canary. Verify no drift. Roll out.
5. Retire priority-mode after 30 days of DAG-mode canary green.

Estimated LOC: ~500 for registry + tests + migration.

---

— Claude (llm-proxy-v2 team), 2026-06-30 (design only; parked)
