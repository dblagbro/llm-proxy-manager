---
name: session-start
description: Orient at the beginning of a work session on llm-proxy-v2 — load the contract and live state, verify the branch/build, and confirm what is safe to touch before doing anything.
---

Run this first thing in a substantial session. Goal: reconstruct accurate state cheaply, avoid
acting on stale assumptions.

## Steps
1. **Read the contract & live state (in order):** `AGENTS.md`, `docs/current-state.md`,
   then `architecture.md` / `design.md` as needed for the task. Note the open risks/blockers.
2. **Confirm the code state:** `git status -sb`, `git log --oneline -5`, current branch (should be
   `main`), and whether HEAD matches `origin/main`. Note any uncommitted/untracked work.
3. **Confirm it builds/imports:** `python -c "import app.main"`; skim `tests/known_failures.txt`
   so you distinguish known-red from new failures.
4. **Confirm deploy reality if infra-adjacent:** which version is live (`/health`), on which nodes;
   remember the canonical stack is `/home/dblagbro/docker/`, not the repo (CLAUDE.md BUG-056).
5. **Restate the objective + the 3 next actions** from `docs/current-state.md`, and the
   prohibited/approval-required list from `AGENTS.md`.

## Output
A 5-line orientation: stage/objective · branch + last-good-commit · what works/doesn't ·
top risk/blocker · the next action. Then proceed (or hand to `work-item` / `project-recovery`).

## Notes
- Do not rediscover what `docs/` already records — verify only what matters for today's task.
- If `docs/current-state.md` looks stale vs reality, fix it (self-healing) before proceeding.
