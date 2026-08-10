# Agent system — llm-proxy-v2

How the portable agent system is laid out, how to invoke it, the model-routing policy, and the
open self-healing backlog. Installed 2026-08-09 (Core-first pass via the `agent-system-setup` skill).

## Layout
- **`AGENTS.md`** — canonical, always-loaded project contract (commands, boundaries, DoD,
  prohibitions). **`CLAUDE.md`** and **`GEMINI.md`** are thin adapters that `@import` it.
- **Skills** — canonical bodies in `.agents/skills/<name>/SKILL.md`; mirrored to
  `.claude/skills/<name>` as **relative symlinks** (verified resolving). Edit the `.agents/` copy;
  the symlink follows. If a consumer can't follow symlinks, replace with a sync script + CI drift
  check (not needed on this Linux/git repo).
- **Agents** — `.claude/agents/<name>.md` (Claude Code project subagents).
- **Durable docs** — `docs/` (see `AGENTS.md` "Documentation map").

## Skills (invoke by name / `/<name>`)
| Skill | Use when |
|---|---|
| `session-start` | Beginning a substantial session — orient on contract + live state. |
| `work-item` | Execute one bounded, approach-clear change end to end. |
| `project-recovery` | After context loss / stalled effort / incident — verify then plan. |
| `qa-master` | Comprehensive independent QA before a release or after a big change. |
| `release-gate` | Final pre-ship gate; prepares commands, stops for approval. |
| `project-bootstrap` | Stand up foundations on a new/rescued project. |
| `agent-system-setup` | Install/modernize this very system (re-runnable). |

*Deferred (follow-up pass):* `refactor-master`, `refactor-daily`, `qa-daily`, `platform-readiness`,
`debug-deep`, `market-review`, `agent-system-review`.

## Agents & model-routing policy
Route by **capability tier** (blast radius, ambiguity, reversibility, verification cost) — not by
dated model names. Tiers map to Claude aliases `haiku` / `sonnet` / `opus`.

| Agent | Tier | Writes? | Trigger |
|---|---|---|---|
| `cartographer` | haiku | read-only | locate code / trace call paths / map a subsystem |
| `architect` | opus | no | design, boundaries, tradeoffs, ADRs, migration sequencing |
| `implementer` | sonnet | code | a bounded, approved work item |
| `debugger` | opus | diagnose | reproduction + root cause (separate from remediation) |
| `qa-engineer` | sonnet | tests | independent verification (won't fix product code) |
| `security-reviewer` | opus | read-only | authz/secrets/deps/supply-chain/infra review |
| `release-engineer` | sonnet | prepare | version/changelog/build/deploy prep (approval-gated) |
| `platform-engineer` | sonnet | prepare | docker/nginx/cloud/IaC (remote read-only; approval-gated) |
| `market-analyst` | sonnet+web | no | dated external research / build-vs-buy |

**Routing rules:** low-cost tier for deterministic, easily-verified, read-only work; mid tier for
ordinary implementation/tests/refactor/docs; high tier for architecture, ambiguous debugging,
cross-system reasoning, security-sensitive analysis, and planning. Use an independent reviewer
(different agent) where a false "done" is costly. Prefer the **smallest effective number** of
concurrent agents — excess parallelism duplicates context and causes inconsistent edits.

## Hooks & CI — PROPOSED (not applied this pass)
The existing `.github/workflows/ci.yml` and git hooks were intentionally left untouched. Proposed
minimal, layered gates for a follow-up (operator to approve before wiring):
- **Fast local (pre-commit):** `ruff format --check` + `ruff check`; secret scan (e.g. gitleaks);
  a dangerous-command guard (block `docker compose down` / `volume rm` in scripts); import smoke.
- **PR gate:** the fast set + the current invariant tests; a **skill-symlink drift check**
  (`.claude/skills/*` resolve into `.agents/skills/`); a docs-drift check (version pins).
- **Daily / scheduled:** full unit suite + integration/Playwright + dependency/security scan.
- **Release:** `release-gate` checklist. Do NOT put the full regression suite on every edit.
**Promotion path:** drive `tests/known_failures.txt` to zero → make the full unit suite a required
PR check (today it is `continue-on-error`). Tracked in `docs/roadmap.md` M2.

## Self-healing backlog (drift found; record-only this pass)
- `bug-log.md` and `refactor-log.md` exist at **both** repo root and `docs/` with divergent content —
  consolidate to one canonical each (mirror the `docs/architecture.md` → root pointer pattern).
- `architecture.md` header says v5.21.8; `app/__version__.py` is v5.22.3 — refresh at arc end.
- CI is narrow (4 gating tests) with 64 known-fail tests and 9 uncommitted test files — see roadmap M2.
- `CLAUDE.md` version drift fixed this pass (now points to `docs/current-state.md`).

## Guardrails (apply to every agent/skill)
No push/tag/publish/deploy without explicit approval · no remote/cluster/DB mutation on peers
without approval · never stop the full Docker stack or destroy volumes · no secrets in code/logs ·
commits to this repo carry no Claude attribution · summarize evidence, don't flood the lead context.
