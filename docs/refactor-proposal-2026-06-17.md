# Refactor Proposal — 2026-06-17

Per operator brief: "incremental architectural refactor focused on maintainability and future development speed". Read architecture / design / refactor-log first; propose top 1-3 highest-value targets before implementing; preserve behavior; don't over-fragment.

## Current top-15 file sizes (LOC)

```
1180  app/api/messages.py                   ← top target
1118  app/cluster/sync_handlers.py
1021  app/cluster/manager.py
 931  app/api/completions.py                ← second target (symmetric to messages.py)
 868  app/routing/router.py
 830  app/main.py
 825  app/providers/grok_web.py
 781  app/api/monitoring.py
 749  app/runs/worker.py
 746  app/api/providers.py
 725  app/monitoring/helpers.py
 725  app/api/_messages_streaming.py
```

The v4.4.38 refactor pass (2026-06-02) flagged `messages.py` (then 861 LOC) and `completions.py` (then 742 LOC) as next targets. Both have GROWN since then: messages.py +319 LOC (1180), completions.py +189 LOC (931). The 800-line trigger from `design.md` is now exceeded by ~50% on `messages.py` alone.

## Proposed targets (top 2)

### Target #1 — `app/api/messages.py` 1180 → ~700 LOC

The handler is a **single 1100-line function** with no internal helper splits. Each version's comment-section markers identify natural extract boundaries. Pull THREE cohesive sub-blocks into helpers under `app/api/_messages_pre_route.py`:

| Sub-block | Approx LOC | Concern |
|---|---|---|
| Compliance UA pre-check + LLM emergency stop + ctx-vars + audit-id setup | ~50 | "decide whether to even try" + observability setup |
| Request normalization (suffix-strip, embedding guard, `model: "auto"` resolve, original-model capture) | ~70 | Body massaging before routing |
| Cross-family fallback + Anthropic↔OpenAI body translation | ~120 | Wire-shape adaptation after route is known |

Each extract returns either `None` (handler continues) or an `HTTPException` to raise. No new types. Test-side: the existing source-grep pins in the v5.x test files continue to work because we don't rename functions — we move them.

**Why this is highest value:**
- File is the highest-traffic code path in the whole proxy. Every PR that touches v1/messages currently brings the whole 1100-line context with it.
- I just touched it in v5.7.17 (watchdog wire-up) and verified the structure during v5.7.13/14/15 — the natural boundaries are clear.
- Three discrete extracts × ~50-120 LOC each, all behavior-preserving — exactly the v4.4.38 incremental shape.

**Risk:** Medium. Hot path. Mitigation: each extract gets a source-grep pin + a behavior assertion (e.g. "compliance UA pre-check raises 451 for banned UA"). The full test suite (~3000 tests) runs after each extract.

### Target #2 — `app/api/completions.py` 931 → ~600 LOC

Same shape as `messages.py`: a single 900-line function with version-comment-section markers. Most sub-blocks are **near-duplicates** of `messages.py` (the symmetry was noted in v4.4.38's next-targets section). Strategy:

- Extract the same three concerns from `completions.py`.
- For each, if the extracted helper differs from messages.py's only in wire-format (Anthropic vs OpenAI), co-locate the shared logic in `app/api/_handler_shared.py` and have each handler thin-wrap.
- For each that's truly different (the OpenAI Responses translation, the OpenAI Chat streaming shape), keep handler-local.

**Why second:** Same v4.4.38 reasoning + the symmetry means doing both together gives shared-helper consolidation that doing one in isolation would miss. Done after #1 lands clean so the message.py extractions inform completions.py's shape.

**Risk:** Medium. Same as #1; mitigated by doing #1 first.

## Explicitly NOT proposed (and why)

- **`cluster/sync_handlers.py` (1118 LOC)** — second-biggest file. Higher-risk: cluster replication; a misstep causes silent sync drift across nodes. Defer to a focused pass after #1+#2 soak.
- **`cluster/manager.py` (1021 LOC)** — same reasoning; cluster code.
- **`routing/router.py` (868 LOC)** — recently touched (v5.7.13 added `exclude_provider_ids` param). v4.4.38 already extracted the litellm-binding sub-module. Diminishing returns.
- **`main.py` (830 LOC)** — startup hooks; flat structure, low complexity. The "natural" extract is to move each `_start_worker` call into a `monitoring/__init__.py` registry, but that's reshape, not refactor.

## Implementation plan if approved

1. **Phase 1 — messages.py extracts** (this pass):
   1. Create `app/api/_messages_pre_route.py`.
   2. Move sub-block 1 (compliance + emergency + ctx setup) → `prepare_request_context(...)`. Update messages.py call site. Run full test suite. Verify smoke.
   3. Move sub-block 2 (request normalization) → `normalize_request_body(...)`. Same loop.
   4. Move sub-block 3 (cross-family + translation) → `adapt_wire_format(...)`. Same loop.
   5. messages.py target: ~700 LOC.
2. **Phase 2 — completions.py mirror** (next pass, after #1 soak):
   1. Identify duplicate sub-blocks vs `messages.py` helpers.
   2. For each duplicate, lift the helper to `app/api/_handler_shared.py`.
   3. completions.py target: ~600 LOC.
3. After each phase: update `architecture.md` module map, append a numbered entry to `refactor-log.md` with files changed / LOC delta / test outcome / next-target notes.

## Behavior preservation checklist

- No public API surface changes (URLs, headers, response shapes).
- No new types in the public layer — extracted helpers return `Optional[HTTPException]` or mutate dicts, both already-used patterns.
- Every extracted function gets:
  - Source-grep pin in a new `tests/unit/test_v5718_messages_extract.py` (and `test_v5719_completions_extract.py` for phase 2).
  - One behavioral test confirming the extracted concern still raises / returns / mutates as before.
- The full ~3000-test suite must pass after each individual sub-block extract, not just at the end.

## Sign-off ask

OK to proceed with Phase 1 (messages.py three extracts) now? Phase 2 is a separate ship after soak.
