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

## Approved exception: Whisper 4.3.1 only

The owner approved this independent sidecar release's scoped gates and version
exception on 2026-11-09 at 16:16:04 UTC (PR11). Apply the
[approved release contract](../../../docs/whisper-4.3.1-security.md#approved-release-gates):
`make lint-whisper`, exact-image `make test-whisper`, touched voice-recipe
`make test`, isolated gateway import, secret scan and all required final-head CI
must pass. The 3,244 inherited gateway lint findings remain recorded debt;
global `make lint` is not reported as passing. Version the sidecar through
`whisper_bridge/VERSION`, leave the gateway version/code/database unchanged, and
recreate only `whisper-bridge`, sequentially with rollback. This is not a general
lint waiver or approval for later gateway releases. Existing explicit approval
covers this scoped release; do not request the same approval again.

## Prepared, then STOP for approval
Present exact commands for commit → push → (build/publish) → rolling deploy, each marked
**AWAITING APPROVAL**, plus a rollback plan. Commits carry **no Claude attribution**.

## HARD stops
- Do NOT push/tag/publish/deploy without explicit approval.
- **NEVER** `docker compose down`/`down -v`/`volume rm` or stop the full stack.
- After deploy: curl the canonical URL, verify `/health` version on each node, and soak for
  slow-degradation regressions before declaring success.
