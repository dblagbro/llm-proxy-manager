# Bug log — moved

**The canonical bug log is [`../bug-log.md`](../bug-log.md) at the repo root.**

This file used to hold a *second*, separate bug log. Despite the shared name the two files
were **fully disjoint** — 10 sections at the root, 14 here, with zero overlap:

- root `bug-log.md` — the regression-sweep stream (v2.7.5 → v5.22.6)
- `docs/bug-log.md` — the QA-pass finding stream (v3.5.7 → v4.4.28)

Anyone reading one and not the other was seeing half the history. On 2026-08-17 the contents of
this file were merged into the root log under "Archived — QA-pass findings"; nothing was dropped.

`AGENTS.md` names `bug-log.md` as the history file, and `tools/cut-release.sh` greps commits for
`^bug-log\.md$` when checking that BUG-### references were tracked — so the root file is the one
that has to stay current. **Add new findings there, not here.**
