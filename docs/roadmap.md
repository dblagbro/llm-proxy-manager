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
- [ ] **Google Gemini accounts as providers** (added 2026-10-27). Add Gemini
      backed by a Google **account** (consumer OAuth), following the existing
      account-provider pattern (`claude-oauth`, `ChatGPT-oauth-plan`, `grok-web`,
      `cursor-oauth`) rather than the already-shipped API-key paths.
      **Scope to confirm with operator before starting:**
      - Distinct from the existing `google` (AI Studio / Gemini API key) and
        `vertex` (GCP service-account) provider types, which already route Gemini
        models via litellm. The new work is the *account/OAuth* ingress.
      - Decide the account surface: AI Studio OAuth, a gemini.google.com
        web-session bridge (à la `grok-web`), or Google OAuth + the
        generativelanguage API. Each differs in auth, quota, and ToS shape.
      - Credential storage touches the open O-7 decision (provider creds are
        PLAINTEXT at rest today) — resolve storage policy before persisting a
        Google refresh token.
      **Accept:** a Gemini-account provider can be added in the admin UI, scans
      its models, serves an Anthropic- and OpenAI-shaped request end-to-end, and
      has unit coverage for the auth/refresh flow. Behind M1/M2 gates.

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
