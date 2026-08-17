---
name: release-gate
description: Final pre-ship gate for llm-proxy-v2 — verify readiness, prepare the version bump/changelog/build and rolling-deploy commands, and STOP for explicit approval before any push, tag, publish, or deploy.
---

The checkpoint between "implemented" and "shipped". Delegate to the `release-engineer` agent for
preparation. Nothing here pushes, tags, publishes, or deploys without explicit human approval.

## Gate checklist (all must pass, with evidence)
1. **Green:** `make lint` clean; touched-area `make test` green; `python -c "import app.main"` ok;
   CI on `origin/main` green. No new entries vs `tests/known_failures.txt`.
2. **Versioned:** `app/__version__.py` bumped; `CHANGELOG.md` entry written; release note drafted.
3. **Docs current:** `architecture.md`, `docs/current-state.md` reflect the change (no version drift).
4. **Safety:** no secrets added; sub-path deploy invariants intact; migrations idempotent.
5. **Deploy plan confirmed:** canonical stack `/home/dblagbro/docker/` (not the repo); **rolling, one
   node at a time**, `sudo docker compose up -d --force-recreate --no-deps llm-proxy2`, then
   `sudo docker exec nginx nginx -s reload`; per-node DB changes applied to each node.

## Prepared, then STOP for approval
Present exact commands for commit → push → (build/publish) → rolling deploy, each marked
**AWAITING APPROVAL**, plus a rollback plan. Commits carry **no Claude attribution**.

## HARD stops
- Do NOT push/tag/publish/deploy without explicit approval.
- **NEVER** `docker compose down`/`down -v`/`volume rm` or stop the full stack.
- After deploy: curl the canonical URL, verify `/health` version on each node, and soak for
  slow-degradation regressions before declaring success.
