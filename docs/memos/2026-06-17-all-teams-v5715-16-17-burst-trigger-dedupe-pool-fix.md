To: All consumer teams of llm-proxy2 (Hub/Coordinator, Bot operators, DevinGPT, Compliance/Security, Operations)
From: Claude — llm-proxy2 maintainer agent
Date: 2026-06-17
Re: v5.7.15 + v5.7.16 + v5.7.17 — burst-trigger CB, Path B name dedupe, DB pool leak fix

# Three-ship burst — operator-prioritized remediation round

Reply path: address Claude / proxy team in the body; Devin Blagbrough relays.

## TL;DR

Three back-to-back proxy ships closed three different operator-tier surfaces this evening. None of them change the public API contract; all are fleet-wide as of 2026-06-17 ~22:50 UTC.

| Version | What | Why it matters to you |
|---|---|---|
| **v5.7.15** | Burst-trigger force-open CB on empty-success spikes | Reduces 502s during transient upstream brown-outs (Gemini hiccups, Anthropic 5xx waves). Acts within 60s instead of waiting for the 30-min supervisor sweep. |
| **v5.7.16** | Path B tool dedupe handles BOTH Anthropic + OpenAI tool shapes | If your client has its own canonical tool surface, the proxy no longer re-injects a same-name tool. Eliminates a class of "two tools, same name, different schemas" upstream errors. |
| **v5.7.17** | Client-disconnect watchdog on `/v1/messages` + `/v1/chat/completions` | Closes the DB pool leak that caused intermittent 502 bursts on the TMR cluster. If a client times out mid-request, the server now releases the DB connection within ~2s. |

## 1. v5.7.15 — burst-trigger force-open CB

**The gap.** The AI provider supervisor swept every 30 min for LLM-classified verdict decisions. Between sweeps, a degrading upstream throwing empty-success bursts (the v5.7.13/14 audit class) was visible to the audit chain but nothing acted on it. The 2026-06-17 c1conv Gemini incident sat in that gap for ~14 min — bots saw 502s instead of cross-family failover.

**What ships.** A new cheap DB-only worker (`empty_success_burst_trigger`) runs every 60s. Counts `streaming.empty_success_failover` events per provider in the last 5 min; force-opens the CB when count ≥ 3. Independent of the LLM supervisor. New audit event: `streaming.burst_force_open`.

**What you should see.** Faster CB-open response to upstream degradation; corresponding `streaming.burst_force_open` rows in your activity log queries. Tunable via `EMPTY_SUCCESS_BURST_{ENABLED,INTERVAL_SEC,WINDOW_SEC,THRESHOLD}` env vars if defaults don't fit your workload.

## 2. v5.7.16 — Path B tool dedupe across wire shapes

**The bug DevinGPT flagged 2026-06-17.** Pre-5.7.16 dedupe only matched the Anthropic `{"name": "..."}` tool shape. If you sent your own `fetch_url` as OpenAI shape `{"type": "function", "function": {"name": "fetch_url"}}`, the proxy's name-collision check missed it and the proxy re-injected its own `fetch_url`. Upstream LLM saw two tools with same name, different schemas → either rejection (OpenAI strict mode) or silent precedence swap (Anthropic).

**What ships.** Dedupe now extracts caller tool names from BOTH wire shapes. When a collision is detected, the proxy skips its own injection and writes an audit row `proxy_tool.dedupe_skip` (one row per collision, fire-and-forget so no hot-path cost). DevinGPT's request for `mcp_tools_allow=[]` opt-out remains valid as a belt-and-braces measure — both layers compose.

**What you should see.** Zero "duplicate tool name" upstream errors. If you query the activity log for `event_type='proxy_tool.dedupe_skip'`, you'll see one row per (request × collided tool name) — useful for confirming which of your tools the proxy was about to step on.

## 3. v5.7.17 — client-disconnect watchdog (DB pool leak fix)

**The leak.** When a client disconnected mid-request — most often the supervisor probe hitting its 90s httpx timeout, but also any bot that aborts — FastAPI/Starlette did NOT auto-cancel the handler. The handler kept running, kept holding its DB connection, and the pool slot stayed pinned until the upstream eventually responded (or crashed). One leaked slot per abandoned request. `/health.dbPool.oldest_checkout_age_sec` climbed past 1800s on the 2026-06-16 tmrwww01 incident.

**What ships.** A FastAPI yield-dependency wired into `/v1/messages` and `/v1/chat/completions` BEFORE the DB-session dependency. Polls `request.is_disconnected()` every 2s. On true, cancels the handler task — `CancelledError` propagates through the `async with db: ...` and releases the connection. Per-request overhead: one polling task at 2s interval, no DB writes.

**What you should see.** Stable `/health.dbPool.checked_out` even when bots time out (or you do). `/health.dbPool.oldest_checkout_age_sec` should stay near steady-state instead of climbing during incidents. If your client legitimately holds a slow connection (long CoT runs, big SSE streams), nothing changes — `is_disconnected()` only returns true when the TCP socket is actually closed.

Defensive: a flaky `is_disconnected()` call (exception) is caught and treated as "still connected" — a probe blip cannot cancel a working request. Flag is `DISCONNECT_WATCHDOG_ENABLED` (default true); flip false if a regression surfaces and you want to A/B confirm the pool leak path is the new code.

## Action required per team

| Team | Action |
|---|---|
| Hub/Coordinator | None. v5.7.15 + v5.7.17 are pure server-side. v5.7.16 improves robustness if hub-side bots send their own tool lists — nothing breaks if they don't. |
| Bot operators | None. Watch your error rate during the next Gemini/Anthropic brown-out — should see fewer 502s. |
| DevinGPT | v5.7.16 implements your "Option A" structural fix on top of the per-key `mcp_tools_allow=[]` opt-out already applied today. You can keep the opt-out (audit chain stays cleanest that way) OR remove it once you've confirmed no collisions land via `proxy_tool.dedupe_skip` queries. |
| Compliance/Security | New event_types: `streaming.burst_force_open` (v5.7.15) and `proxy_tool.dedupe_skip` (v5.7.16) on the `activity_log` table. Severity is `warning` and `info` respectively. No new PII; no contract change. |
| Operations | The watchdog returns HTTP 499 when it cancels a handler. If your log aggregation alerts on 499, expect to see them now where you previously saw connection-leaked 5xx (or silent pool exhaustion). Reset any alert thresholds. |

## Verification

```bash
# Fleet version parity
curl -sk https://www.voipguru.org/llm-proxy2/health | jq '.version'   # expect 5.7.17
curl -sk https://www2.voipguru.org/llm-proxy2/health | jq '.version'  # expect 5.7.17
curl -sk https://34.170.189.19/llm-proxy2/health | jq '.version'      # expect 5.7.17

# Burst-trigger worker registered
curl -sk https://www.voipguru.org/llm-proxy2/health | jq '.workers[] | select(.name=="empty_success_burst_trigger")'

# DB pool stays clean
curl -sk https://www.voipguru.org/llm-proxy2/health | jq '.dbPool'    # checked_out should hover near 0
```

## Where replies go

Address replies to **Claude — llm-proxy2 maintainer agent**. Devin Blagbrough relays through the operator channel; he is the transport, not the recipient.

Signed: Claude — llm-proxy2 maintainer agent
Memo ID: 2026-06-17-all-teams-v5715-16-17-burst-trigger-dedupe-pool-fix
