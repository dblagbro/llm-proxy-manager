# Roadmap — llm-proxy-v2

Milestones with measurable acceptance criteria. Reconciles with `docs/remediation-plan.md`,
`docs/recovery/01-current-state-assessment.md`, and the backlogs. Current focus: rescue → foundation.

## M1 — Recovery & stabilization (in progress)
**Goal:** stop the recurring node degradation; establish trustworthy live status.
- [ ] Land the **DB-connection-hold leak** fix (A: release session before upstream call; or B: fixed
      disconnect-watchdog; C: stopgap reaper). See `docs/current-state.md`.
      **Accept:** on a live node under real traffic, `get_db` sessions do not exceed a few seconds of
      age and `checked_out` returns to ~0 between requests; `/health` stays < 100 ms and `healthy`
      for ≥ 72 h with no restart.
- [x] Unbounded aiosqlite **thread** leak fixed (self-heal dispose disabled).
- [x] fd ulimit hardening; grok-web bridge restored.
- [ ] Turn `DB_POOL_TRACE` back off on www1 once the leak fix lands.
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
