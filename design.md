# llm-proxy-v2 Design Principles

This document is the **contract** that future refactors and feature additions are
checked against. `architecture.md` describes *what is*; this describes *what
should be* and *why*.

## North Star

A self-hosted LLM routing gateway that an operator can read end-to-end in an
afternoon. We optimize for legibility-by-the-next-engineer, not for cleverness
or theoretical maximum throughput.

## Layering

```
┌─────────────────────────────────────────────────────────────────┐
│  api/         HTTP shape only — parse → call core → format       │
├─────────────────────────────────────────────────────────────────┤
│  routing/     Provider selection + LMRH protocol (pure logic)    │
│  cot/         CoT pipeline (pure logic + side-effecting LLM I/O) │
├─────────────────────────────────────────────────────────────────┤
│  monitoring/  Metrics, activity log, keepalive (side effects)    │
│  cluster/     Peer sync (side effects)                           │
│  providers/   Vendor-specific quirks: scanner, OAuth flows       │
├─────────────────────────────────────────────────────────────────┤
│  models/      SQLAlchemy ORM + DB session                        │
│  config/      Pydantic settings (env) + runtime overrides        │
└─────────────────────────────────────────────────────────────────┘
```

**Direction of dependency**: top → bottom only. `routing/` may not import
`api/`. `models/` may not import anything else.

## Module-boundary rules

1. **One responsibility per file.** When a file grows past ~600 lines OR
   accumulates `if x: do_a(); elif y: do_b()` chains over more than three
   branches, it's time to split.

2. **Extract logic that's been duplicated 3x.** Two copies = coincidence,
   three = pattern. Don't pre-extract on the second copy; do extract on
   the third. (See: v3.0.27/30/31 → `resolve_chat_model_for_provider()`
   was the right time, not before.)

3. **Provider-type quirks live in `providers/`, not `routing/`**. The
   router asks "give me a chat-capable model for this provider"; the
   answer (preferring `command-*` over `gpt-*` for cohere, etc.) lives
   close to the provider scanner.

4. **HTTP-shape concerns stay in `api/*`**. SSE generators, body-shape
   parsing, image stripping — all in `api/`. The routing layer never
   sees an `aiohttp` request.

5. **Side effects vs pure logic.** Pure logic (parsers, scorers,
   capability inference) is sync, returns values, raises on bad input.
   Side-effecting logic (DB writes, network I/O, metrics) is async and
   uses the standard `record_outcome()` pipeline so observability is
   uniform.

## Anti-patterns to call out

- **Lazy imports inside functions** — only acceptable when avoiding a
  genuine circular import. Otherwise hoist to file top. (See refactor-log
  S6 for cleanup.)
- **Adding a new "endpoint kind" boolean to `select_provider()`** —
  signals the function is being asked to do too much. Split first.
- **Touching `config_runtime.SCHEMA` to gate a behavior change** — fine
  for runtime knobs, but every new schema entry is a permanent debt.
  Prefer dim/feature registration via LMRH if the choice is request-scoped.
- **Wire-format conversion in routing/** — translation between Anthropic
  and OpenAI shapes belongs in `api/`, not `routing/`.
- **Hardcoded provider names anywhere outside `providers/` and the
  routing family-filter map** — those are the only two places that
  legitimately know about specific upstream brands.

## Cluster sync invariants

- Push payloads are authoritative for `(table, primary_key)` pairs the
  source node owns; merge logic is "newest-wins" by `updated_at` or a
  domain-specific timestamp (`registered_at` for LMRH dims,
  `last_user_edit_at` for providers).
- Soft-delete tombstones (`deleted_at`) are required for any table that
  participates in cluster sync. Without them, the next push from a peer
  resurrects deleted rows. Pattern: see `Provider` (v2.8.2), `ApiKey`
  (v3.0.20), `LmrhDim` + `LmrhProposal` (v3.0.29).
- Insert-if-missing skips materialization of peer tombstones — there's
  no point creating a row only to mark it deleted.

## Observability invariants

- Every dispatch terminates in **`record_outcome()`** (in
  `monitoring/helpers.py`). All metrics, activity log, and circuit
  breaker state flow through that one function. New code paths must
  not bypass it.
- Log levels: `INFO` is for events an operator wants to see during a
  normal day. Per-request lines (`router.selected`, `request`) belong
  at `DEBUG`. The `request` access-log line is the canonical
  request-level INFO.
- Structured-log `extra={...}` fields are *additive*; the message string
  must still be useful to a human tailing stdout. (`circuit_breaker.opened`
  fix in v3.0.30.)

## Versioning + deploy invariants

- Every code change increments `app/__version__.py`. The git tag, the
  Docker Hub tag, and the `/health` response all read this same string.
- Rolling deploy: one node at a time, verify `/health` shows the new
  version before moving on. Never update the whole fleet simultaneously
  (memory: `feedback_rolling_deploy.md`).
- Cluster-sync schema changes ship with idempotent ALTER TABLEs in
  `app/models/database.py:init_db` (try/except per statement).

## Test discipline

- Unit tests cover the pure-logic layer (`routing/lmrh/*`, parsers,
  scorers).
- Integration tests run against the live deployment via Playwright
  (`tests/integration/test_playwright_ui.py`); each test gets its own
  browser context so cookie state doesn't leak.
- New routing behavior gets a curl repro line in the commit message.
  This has been the diff between "ships" and "regresses" three times
  in the v3.0.x stretch.

## When to refactor

The bar for adding to `refactor-log.md`:

- ✅ Three or more code paths share a 10+ line block (extract).
- ✅ A file passes 800 lines or accumulates >5 distinct concerns (split).
- ✅ A bug class has been re-fixed in two different files (extract the
  guard).
- ❌ A file *could* be split but isn't painful — leave it.
- ❌ A pattern *might* generalize someday — wait until the third caller.

## Refactor change-control

1. Read this doc + `architecture.md`.
2. Identify top 1–3 targets by `(value × confidence) / risk`.
3. Propose before implementing — even a single-paragraph note in the
   PR/commit body counts.
4. Preserve behavior — all existing tests + manual repros pass.
5. Update `architecture.md` if module boundaries moved.
6. Append a one-paragraph entry to `refactor-log.md`.
