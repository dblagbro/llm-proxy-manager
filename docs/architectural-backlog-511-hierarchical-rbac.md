# #511 — Hierarchical RBAC (org / workspace / team / user) — parked

**Status:** design + rationale — parked until we have a second org consumer.
**Driver:** Portkey (competitor OSS proxy) ships a 4-level RBAC model. Would be genuinely useful if we ever onboard a second org whose users need isolation from Devin's.

## Current state (v5.14.x)

Two-tier: `ApiKey` (per-caller) + admin/non-admin flag on users. Compliance policy at ApiKey scope. No org / workspace / team notion.

## Proposed model

Four scopes, each a parent of the next:

```
Org (root)
 └─ Workspace (billing + settings unit)
      └─ Team (grouping of users + shared providers)
           └─ User (individual auth identity)
```

Every resource (provider, api_key, activity_log row, mcp_tool grant) belongs to exactly one Org. Access checks walk up: user → team → workspace → org, with policy at each level.

**Concrete features enabled:**
- Two orgs can share the deployment without seeing each other's providers.
- Team-level shared credentials (e.g. "eng team's shared Anthropic key").
- Workspace-level compliance policy (Legal-orgs can lock provider allowlists differently from R&D).
- Per-org audit trails.

## Why parked

Devin is the only operator today. Everything ships in one org implicitly. Adding a 4-level model without a second org means:
- Every table gets an `org_id` column (~15 tables) with a required backfill
- Every read query gets an `org_id` filter (~200 sites)
- Admin UX has to render org-switcher chrome that nobody uses
- Testing surface doubles for the org-boundary paths

Cost: ~2-3 weeks of engineering, high-risk migration.
Benefit today: zero (one org).
Benefit at N=2 orgs: obvious.

## Trigger criteria to un-park

Any of:
1. Operator opens a conversation with a second org / consumer team about deployment access.
2. A compliance ask requires per-tenant policy isolation (e.g. a customer's HIPAA data can't co-mingle with Devin's ChatGPT logs at the same DB level).
3. Portkey's OSS model gains public patterns worth mirroring (we cited Portkey but haven't audited their exact schema — that research is part of the un-parking).

## Slice preview (when un-parked)

Phase 1 (2 weeks):
1. New tables: `orgs`, `workspaces`, `teams`, `team_memberships`.
2. `org_id` column on 15 owning tables. Backfill to a single "default" org.
3. Read-query filter helpers with a `require_org_scope(request)` dependency.
4. Admin UI: org switcher chrome, workspaces list.
5. Tests: two-org fixture that verifies isolation.

Phase 2 (1 week):
6. Per-scope compliance policy.
7. Cluster-sync of org/workspace/team.
8. Retention & audit-log scoping.

Total ~500 LOC + 15-table migration + 200-site scope-filter refactor.

---

— Claude (llm-proxy-v2 team), 2026-06-30 (design only; parked)
