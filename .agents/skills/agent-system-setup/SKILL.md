---
name: agent-system-setup
description: Installs or modernizes the repository's portable instructions, Agent Skills, specialized agents, hooks, and durable project memory. Use once on a new repository and again when the agent workflow has drifted.
---

Act as the lead agent-platform architect. Use planning mode and inspect the repository read-only before creating or changing files.

## Objective

Build a token-efficient, portable, auditable engineering system that works primarily with Claude Code and can also guide Codex, Gemini CLI, GitHub Copilot, and future compatible agents.

## Portable instruction layer

Create or update, without duplicating existing equivalents:

- `AGENTS.md` as the concise canonical project contract
- `CLAUDE.md` as a thin Claude adapter that imports `@AGENTS.md` and contains only Claude-specific notes
- `GEMINI.md` as a thin Gemini adapter that imports `@AGENTS.md` when Gemini is used
- existing Copilot or editor instruction files only when needed

Keep always-loaded files concise, specific, non-conflicting, and focused on:

- repository layout and ownership
- approved build, run, test, lint, format, migration, and deployment commands
- architecture boundaries and dependency direction
- coding and security constraints
- definition of done
- prohibited or approval-required operations
- pointers to on-demand skills and detailed documents

Do not place long procedures in always-loaded instruction files.

## Portable skills

Use `.agents/skills/` as the canonical location for reusable Agent Skills when practical. Create Claude-compatible adapters in `.claude/skills/` using symlinks when supported. If reliable symlinks are unavailable, create a deterministic synchronization script and CI drift check rather than maintaining divergent copies.

Create or update these skills:

- `project-bootstrap`
- `project-recovery`
- `session-start`
- `work-item`
- `refactor-master`
- `refactor-daily`
- `qa-daily`
- `qa-master`
- `release-gate`
- `platform-readiness`
- `debug-deep`
- `market-review`
- `agent-system-review`

Use standard Agent Skills frontmatter where possible. Put scripts, templates, schemas, and examples beside the relevant `SKILL.md`. Keep skill descriptions precise so skills activate only when relevant.

## Project-local Claude agents

Create or update specialized agents under `.claude/agents/`. Adapt the roster to the project, but normally include:

1. `cartographer` — Fast, read-only, narrow-scope codebase exploration. Uses code intelligence/LSP, search, Git history, and dependency metadata. Returns a concise map with exact file and symbol references, never raw file dumps.
2. `architect` — Strongest reasoning tier. Read-only planning, boundaries, interfaces, tradeoffs, ADR proposals, migration sequencing. Does not implement during architecture review.
3. `implementer` — Standard coding tier. Implements an approved, bounded work item. Runs focused verification and updates affected documentation. Uses worktree isolation when parallel edits are independent.
4. `debugger` — Strong reasoning tier. Reproduction, competing hypotheses, logs, traces, metrics, environment comparison, and root cause. Separates diagnosis from remediation.
5. `qa-engineer` — Independent test planning and execution (Playwright, API, integration, CLI/SSH, accessibility, visual, performance, regression). Does not silently fix defects during an independent QA pass.
6. `security-reviewer` — Strong reasoning tier and read-only by default. Threat modeling, auth boundaries, secrets, dependencies, supply chain, container and infrastructure review. Requires evidence and independently verifies important findings.
7. `release-engineer` — Git, GitHub, versioning, CI, release notes, Docker build and registry preparation. May prepare commands and artifacts, but may not push, publish, tag, or deploy without explicit approval.
8. `platform-engineer` — Docker, Compose, Kubernetes, Terraform/OpenTofu, AWS, Google Cloud, Azure, and self-hosted infrastructure. Remote environments are read-only first. No apply, deployment, database mutation, cluster mutation, or destructive command without explicit approval.
9. `market-analyst` — Current external research, alternatives, licensing, maintenance, security history, adoption, demand, and build-versus-buy analysis. Updates dated competitive evidence rather than relying on memory.

Give every agent: a narrow responsibility and explicit trigger; the minimum required tools and permissions; a required output format; a rule to summarize evidence rather than flooding the lead context; model routing by capability tier, not hardcoded dated model versions; no access to secrets unless strictly required; no remote mutation unless explicitly authorized.

Use subagents for context-heavy investigation and independent review. Use a scripted workflow for repeated fan-out, branching, or adversarial cross-checking. Use agent teams only when tasks are genuinely independent and agents need peer communication. Do not create a team for sequential work or overlapping edits.

## Durable project memory

Create or update existing equivalents, preserving established naming and avoiding case-duplicate documents.

Required: `README.md`, `CHANGELOG.md`, `docs/project.md`, `docs/architecture.md`, `docs/project-map.md`, `docs/current-state.md`, `docs/roadmap.md`, `docs/testing.md`, `docs/bug-log.md`, `docs/refactor-log.md`, `docs/agent-system.md`.

Create when relevant: `docs/design.md`, `docs/operations.md`, `docs/deployment.md`, `docs/security.md`, `docs/competitive-analysis.md`, `docs/remediation-plan.md`, `docs/backup-plan.md`, `docs/release-readiness.md`, `docs/adr/`.

`docs/project-map.md` must be a concise human map of entry points, domains, ownership, dependencies, tests, runtime boundaries, and important commands. Do not turn it into an exhaustive file listing.

`docs/current-state.md` must stay brief: current stage and objective; branch and last known good commit; what works and what does not; active risks and blockers; latest verification status; next three actions; exact resume commands when non-obvious.

## Self-healing rule

On every run, reconcile instructions, documentation, code, tests, CI, containers, and infrastructure definitions. When drift is found:

1. Determine whether the implementation, documentation, test, or rule is stale.
2. Correct low-risk local drift when intent is clear.
3. Escalate business-scope, architecture, data, security, production, or remote-infrastructure ambiguity for review.
4. Prevent recurrence in the correct layer: stable fact/universal rule → `AGENTS.md`; repeatable procedure → Agent Skill; deterministic prohibition/check → hook, policy, test, or CI; significant architecture decision → ADR.
5. Verify the correction and record it in the appropriate history.

Never "self-heal" by inventing product requirements or silently changing external systems.

## Hooks and CI

Propose a minimal set of deterministic hooks and CI gates for: formatting/lint/type/focused-tests; secret detection; dangerous-command protection; generated-skill synchronization when needed; documentation drift checks where practical; container build checks; dependency and security checks. Do not put expensive full regression suites on every edit. Separate fast local gates from daily, pull-request, release, and scheduled suites.

## Completion

First provide: discovered agent/tooling state; proposed files and changes; migration risks; which existing instructions conflict; the exact setup plan. Pause for review before writing the system. After approval, implement it, verify agent and skill discovery, document how to invoke everything, and make no remote changes.
