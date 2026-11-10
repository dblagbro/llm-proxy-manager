# Roadmap — llm-proxy-v2

Milestones with measurable acceptance criteria. Reconciles with `docs/remediation-plan.md`,
`docs/recovery/01-current-state-assessment.md`, and the backlogs. Current focus: rescue → foundation.

## M1 — Recovery & stabilization (mostly done)
**Goal:** stop the recurring node degradation; establish trustworthy live status.
- [x] **DB-connection-hold leak fixed (v5.22.4, option A)** — release-boundary commit + dispatch
      re-select commits; pool 20→40. Verified: `checkedout=0` under load, 0 QueuePool errors,
      ~12 min realistic soak both nodes healthy. See `docs/current-state.md`.
      **Remaining acceptance:** a ≥72 h unattended soak under real traffic (monitor, not yet elapsed).
- [x] Unbounded aiosqlite **thread** leak fixed (self-heal dispose disabled).
- [x] fd ulimit hardening; grok-web bridge restored.
- [x] `DB_POOL_TRACE` turned back off on www1.
- [ ] **(P2, new) Single-event-loop CPU ceiling under extreme concurrency** — pre-existing, separate
      from the leak (SQLAlchemy query construction saturates the loop under abusive bursts). Not
      triggered by real traffic. Options: multiple uvicorn workers, or cache/curtail provider-select
      query building. Investigate before any high-concurrency use.
**Checkpoint:** both nodes healthy for 72 h unattended → proceed to M2.

## M2 — Verified foundation
**Goal:** a test suite you can trust; no doc drift.
- [ ] Drive `tests/known_failures.txt` to zero; promote the full unit suite to a **gating** CI check.
      **Accept:** CI red on any new unit failure; `known_failures.txt` empty.
- [ ] Commit the 9 uncommitted test files; remove version-drift in `architecture.md`.
- [ ] Consolidate duplicate docs (`bug-log.md`, `refactor-log.md` at root vs `docs/`) to one canonical
      each (mirror the `docs/architecture.md`-pointer pattern).

## M3 — Controlled implementation
Feature work resumes only on the verified foundation. Draw items from `docs/remediation-plan.md`
and the backlogs; each ships behind M1/M2 gates with a test + measurable acceptance criteria.

### Feature backlog
- [ ] **Google Gemini accounts as providers** (added 2026-10-27; design spike 2026-10-28).
      **⛔ BLOCKED on a governance ruling before any code.** A consumer-account /
      OAuth Google ingress is a *new consumer-subscription OAuth provider* — the
      exact capability the project formally decided to **stop building**:
      - `app/api/_provider_type_policy.py` is a hard write-path gate
        (`RETIRED={ChatGPT-oauth-plan}`, `DEPRECATED={claude-oauth, grok-web}`;
        `validate_provider_type()` → 422 for a new consumer-OAuth type).
      - `docs/market-review.md` (2026-09-26) recommendation **NARROW**: stop
        building consumer-subscription OAuth; M5 goal "zero providers depending on
        subscription session credentials." Driven by vendor ToS **and** telemetry
        (`Grok-Web-Devin` 0/27, `Devin-Codex-Gmail` 1/4457).
      This item therefore needs an **operator policy ruling + a superseding ADR**
      (`docs/adr/`) before a line is written. Options from the spike:
      - **Option A — `google-oauth` (new type).** Google OAuth 2.0 + the
        `generativelanguage` API. *More defensible than claude/grok*: it uses a
        **published developer OAuth scope**, not browser-session scraping — so a
        narrow, Google-only exception to NARROW may be justifiable (ADR required).
        Reuses the full `OAuthProviderSpec` pattern; **no DB migration**; Google
        refresh tokens are stable (simpler refresh than claude-oauth). Open: confirm
        whether litellm has a bearer-mode gemini binding or we need a ~150-LOC
        own-dispatcher; legal ruling on routing others' traffic through one
        consumer account.
      - **Option C — in-policy, no reversal.** Extend the existing `google`
        (AI-Studio **API-key**) type into the `ProviderOAuthAccount` fan-out so
        several Google accounts load-balance. No new provider type, no gate
        reversal, no ToS exposure — delivers "multiple Google accounts" if that is
        the real goal.
      - **Option B — gemini.google.com web bridge (grok-web style): REJECTED** —
        reproduces the most-prohibited, least-reliable pattern the project just
        retired.
      - Distinct from the existing `google`/`vertex` API-key types that already
        route Gemini via litellm.
      - Credential storage: a Google **refresh token** must be stored **encrypted**
        (ties to O-7); the spike mapped the ~5 encrypt/decrypt plug points. No new
        columns — reuses `Provider.oauth_refresh_token` + `ProviderOAuthAccount`.
      **First question to operator:** is the goal "log in with my Google account"
      (Option A — needs the NARROW reversal + ADR) or "load-balance several of my
      Google accounts" (Option C — in policy today)?
      **Accept (whichever option):** provider addable in admin UI, scans models,
      serves Anthropic- and OpenAI-shaped requests end-to-end, auth/refresh unit
      coverage, encrypted token storage. Behind M1/M2 gates.

## M4 — Integration & hardening
End-to-end verification across nodes; reliability soak; security review of the compliance +
auth + cluster surfaces (`security-reviewer`); dependency/supply-chain check.

## M5 — Release readiness
Strong CI gate green; `CHANGELOG.md` + release notes; rolling-deploy runbook validated; rollback
tested; `docs/release-readiness.md` sign-off. Ship via the `release-gate` skill (approval-gated).

## Sequencing & risks
- Do not start M3 feature work until M1 (stability) and M2 (trustworthy tests) are met.
- Biggest risk: the leak fix (A) is a handler refactor with blast radius — verify **live**, roll one
  node at a time, keep the other as a control.
