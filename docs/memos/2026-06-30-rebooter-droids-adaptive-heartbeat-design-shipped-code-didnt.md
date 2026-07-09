# To rebooter-droids team — Adaptive heartbeat (v0.6.48) docs landed; code did not

**From:** llm-proxy-v2 team (Claude, on behalf of Devin)
**Date:** 2026-06-30
**Re:** rebooter-droids v0.6.48 adaptive-heartbeat cadence — empirical verification (closes proxy-team #464)

---

## TL;DR

The CHANGELOG entry for v0.6.48 documents an adaptive heartbeat-cadence feature
(`heartbeat_interval_active_seconds`, `command_active_window_seconds`,
`has_recent_command_activity()`). **None of those identifiers exist in the
deployed source**, and the empirical click-to-execute latency on the
rebooter-droids-pg DB still shows the steady-state ~60 s pattern that the
v0.6.48 design was intended to break out of. Looks like a documentation-without-
implementation slip — or an intentional revert that the changelog never caught
up with.

## The finding

### What v0.6.48 changelog says shipped

> **Adaptive heartbeat cadence (relay-click responsiveness).** ... when a device
> has pending or recently-active commands ..., the heartbeat handler returns
> `heartbeat_interval_active_seconds` (default 5s) as `next_heartbeat_after_seconds`
> instead of the steady-state value. Click-to-execute latency during interactive
> sessions drops from ~35s median to ~2.5s median ...
>
> - New settings (env): `REBOOTER_HEARTBEAT_INTERVAL_ACTIVE_SECONDS`,
>   `REBOOTER_COMMAND_ACTIVE_WINDOW_SECONDS`.
> - New service helper: `services.commands.has_recent_command_activity(...)`.

### What's actually in the source

```bash
$ grep -rn "has_recent_command_activity\|heartbeat_interval_active\|command_active_window" app/
# (empty)
```

```python
# app/blueprints/device_api.py:155
return ok({
    "next_poll_after_seconds": next_poll,
    "next_heartbeat_after_seconds": settings.heartbeat_interval_seconds,  # ← steady-state, unconditional
    ...
})
```

The branch the changelog promised — returning the active-cadence value when
recent command activity is observed — does not exist. `next_heartbeat_after_seconds`
is unconditionally the steady-state value.

### What the data shows

Query against `rebooter-droids-pg` (`commands` table, last 14 days):

| Day        | n | mean | **p50** | p95   | min   | max    |
|------------|---|------|---------|-------|-------|--------|
| 2026-06-22 | 6 | 71.8 | **61.8**| 138.6 | 23.6  | 151.2  |
| 2026-06-19 | 1 | 64.4 | **64.4**| —     | 64.4  | 64.4   |
| 2026-06-18 | 2 |  6.8 | **6.8** |  12.7 |  0.22 |  13.3  |

`delivered_at - created_at` in seconds. Sample is sparse (operator-driven
interactions only), but the post-v0.6.48 medians (61.8 s, 64.4 s) sit
right at the configured `heartbeat_interval_seconds=60`. The 06-18 day with a
6.8s median is suggestive but n=2 — could just be the operator hitting two
buttons in quick succession where the *second* command's heartbeat carried
the piggyback inline (which is the v0.6.15 path, not v0.6.48).

That pattern matches "design as documented didn't ship" perfectly:
post-v0.6.48 deployments should show p50 < 5 s during operator interaction;
they show p50 ≈ steady-state instead.

## What to do (you choose)

Three plausible paths, in order of likely intent:

### A. The code was reverted and the changelog wasn't updated

If v0.6.48's adaptive branch was rolled back after a regression (e.g. firmware
flapping under 5s heartbeats, ESP8266 heap pressure spike, BearSSL allocations
climbing too fast), the cleanest action is a changelog correction —
re-stamp v0.6.48 with a "REVERTED" note pointing to the version that pulled
it. We've been there with our own changelog (see llm-proxy-v2 v5.10 → v5.12.0
versioning skip — see ours for the pattern if useful).

### B. The implementation got squashed/lost in the 0.6.49 refactor

The 0.6.49 entry says "BUG-074/078 picker-scope re-validation". If the 0.6.48
adaptive-heartbeat commit happened to live on the same branch as picker-scope
work and got accidentally squashed during the consolidation, the recovery is
to cherry-pick the original change back. Worth a `git log -S
"heartbeat_interval_active"` to see if the symbol ever existed.

### C. The feature is real but the activation predicate is wrong

If the code IS there and we just couldn't find it, two predicate gotchas to
check:
- "recently-active" might be checking `commands` for `status='pending'` only.
  In the 0.6.15 piggyback path, commands get `delivered_at` set the moment
  they're handed to the heartbeat response — so they leave the pending pool
  on the *first* heartbeat. Subsequent heartbeats don't see "pending" rows
  and revert to steady-state.
- The "active" branch may require `command_active_window_seconds` to be set
  via env (no default fallback). If `REBOOTER_HEARTBEAT_INTERVAL_ACTIVE_SECONDS`
  is unset, the branch could short-circuit to steady-state.

## A small process suggestion (optional)

For changelogs that document new behavior, consider a one-line static-grep
test in CI that asserts the documented symbol exists in the source — same
shape as our `tests/unit/test_v5141_hook_runner_pins_all_endpoints.py`. We
just shipped one yesterday after a similar near-miss where v5.14.0 documented
a hook-registry contract but only wired 2 of 7 endpoints. The test catches
"doc lied" before merge. Happy to share the pattern if useful.

## Measurement details (for your reproducibility)

```sql
-- Latency trend, daily, post-shipment of v0.6.48
SELECT
  date_trunc('day', created_at) AS day,
  COUNT(*) AS n,
  ROUND((PERCENTILE_CONT(0.5)
    WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (delivered_at - created_at))))::numeric, 2) AS p50,
  ROUND((PERCENTILE_CONT(0.95)
    WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (delivered_at - created_at))))::numeric, 2) AS p95
FROM commands
WHERE delivered_at IS NOT NULL
  AND created_at > NOW() - INTERVAL '14 days'
GROUP BY 1 ORDER BY 1 DESC;
```

If you want a denser sample, two ways to densify:
1. Operator runs a 10-click test burst (back-to-back relay toggles) — that
   should isolate the active-branch path from cold-start steady-state.
2. Hub-side instrumentation: log `next_heartbeat_after_seconds` values into
   an event so we can see whether the active branch was ever taken
   *intent*-wise vs whether the firmware honored it. If the hub never emits
   a value < 60 s, the predicate is the problem.

---

— Claude (llm-proxy-v2 team, on behalf of Devin)

P.S. — I'm relaying via the operator (Devin forwards). If you reply, tell him
where (`/mnt/s/code/rebooter-droids/docs/memos/` or wherever your team archive
lives) and he'll loop me in.
