---
name: project-recovery
description: Reconstruct verified ground truth for llm-proxy-v2 after context loss, a stalled/restarted effort, or an incident — then produce a prioritized recovery plan. Use when status/docs may be wrong and you must verify before acting.
---

Do not trust prior plans, status reports, or "done" claims. Verify important conclusions against
the repository, tests, logs, and running system. Distinguish facts, evidence-backed inferences,
open questions, and recommendations. Do not begin broad implementation during recovery.

## Phase 1 — Reconstruct current state (evidence-based)
Inspect (targeted, not whole-repo): git history/status, `tests/` + `tests/known_failures.txt`, CI
(`.github/workflows/ci.yml`), `app/__version__.py` vs deployed `/health` vs the Docker Hub tag,
`architecture.md`/`design.md`, and live logs (`docker logs`, SIGUSR2 pool trace, `/health`, metrics).
Report: intended end state; implemented vs partial/abandoned; known failures/regressions/tech-debt;
tests that exist/fail/are missing; docs that conflict with code; live risks (security, reliability,
perf, ops). **Do not call a capability "working" because code exists — cite the evidence.**

## Phase 2 — Blameless delivery-failure analysis
For each material issue: evidence · likely root cause · impact · the detection method that should
have caught it earlier · corrective action · a prevention control in the right layer (AGENTS.md rule
/ Skill / hook-or-test / ADR).

## Phase 3 — Recovery plan (staged)
Organize as: (1) recovery & stabilization; (2) verified foundation; (3) controlled implementation;
(4) integration & hardening; (5) release readiness. Each milestone gets **measurable** acceptance
criteria, dependencies, risks, and a continue/revise/rollback checkpoint. Break only milestone 1
into executable tasks; keep later ones at planning level until earlier findings are verified.

## Deliverables (durable, reuse existing docs)
Update `docs/current-state.md` (brief live status), `docs/recovery/` (assessment), and a
`docs/remediation-plan.md`/roadmap entry. Preserve findings so they survive context compaction.

## Rules
- Small diagnostic commands, targeted experiments, and verification tests are authorized; avoid
  unrelated changes. Prefer live evidence over subprocess reproductions for async/aiosqlite behavior.
- Ask a focused question only when the answer would materially change the plan.
