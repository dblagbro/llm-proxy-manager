# Run Runtime — Operator Runbook (v3.0.0)

The Run runtime is a server-mediated agent loop that replaces black-box
`claude --print` invocations with a state-machine-driven, observable, and
recoverable execution model. This doc covers operator concerns: tuning,
recovery, cluster ops, and the failure modes you'll hit at 2 AM.

## Endpoints (5)

| Verb | Path | Purpose |
|---|---|---|
| POST | `/v1/runs` | Create a Run (idempotent on `idempotency_key`) |
| GET | `/v1/runs/{id}` | Get current state |
| POST | `/v1/runs/{id}/cancel` | Cancel (idempotent) |
| POST | `/v1/runs/{id}/tool_result` | Post a tool result back into a run |
| GET | `/v1/runs/{id}/events` | SSE event stream OR `?since_ms=` polling |
| POST | `/v1/runs/{id}/adopt` | Peer takeover after owner-node failure |

Schema artifact: `runs.openapi.json` at the repo root.

## State machine

```
queued → running → requires_tool → running → … → completed
                ↘ failed
                ↘ expired
                ↘ cancelled
```

`completed`, `failed`, `expired`, `cancelled` are jointly the terminal set —
SSE consumers should treat them as a group, not gate on individual kinds.

## Settings (admin-tunable, live without restart)

| Setting | Default | Purpose |
|---|---|---|
| `runs_max_turns_ceiling` | 50 | Server-side clamp on `max_turns` per run. Hard ceiling 200; the API clamps and emits `max_turns_clamped` when capping. |
| `runs_max_model_calls_per_minute` | 5 | Per-Run rate limit. On excess: queue with exponential backoff, emit `rate_limited` event. |

Both editable from the Settings page (group: "Run runtime") — change applies
to all in-flight runs immediately; previously-limited buckets re-evaluate on
the next acquire.

## Recovery

**On proxy restart:** the lifespan hook scans `runs WHERE owner_node_id =
this_node AND status IN ('running','requires_tool')` and relaunches a worker
per row. Each emits a `run_recovered` event with `via: "recovery_sweep"`.

If a run was mid-model-call when the proxy died, the worker restarts from
the persisted message history. The in-flight call is lost (no record of
the partial response); the next iteration's model call sees the same
conversation tail and proceeds.

**On owner-node failure (cluster):** a peer can adopt the run via
`POST /v1/runs/{id}/adopt`. Refuses unless the owner peer's heartbeat is
older than `_ADOPT_OWNER_GRACE_SEC` (30s) — this is the split-brain guard.
Successful adopt: emits `run_recovered{via:"adopt", recovered_from_node_id}`,
spawns a worker locally, subsequent ops route here via the 307 redirect on
non-owner nodes.

## Cluster ops

Cluster-sync extension: every state transition replicates to peers.
- Non-terminal: 250ms debounce coalescer (overlapping transitions inside
  the window collapse into one peer push)
- Terminal: sync-acked with 2s peer-ack timeout + 5s/15s/45s background
  retry chain on failure

Peers see incoming run state via `apply_sync()`'s new `runs` section;
last-write-wins by `updated_at`. Peers do **not** auto-spawn workers from
sync — only `owner_node_id` runs the worker; ownership change requires
explicit `POST /adopt`.

Non-owner nodes return **307 Redirect** with `Location` header on
`cancel`, `tool_result`, and `events` endpoints. `GET /v1/runs/{id}` stays
local (replicated state is consistent for reads).

## Compaction

Triggers when conversation tokens reach 80% of model context window.
- Preserves: system prompt + last 8 messages (= last 4 user/assistant turns)
- Summarises: everything in between, via one shot to a cheap model
- Compaction model: per-run override (`compaction_model` field on Run create)
  > cheapest claude-haiku-* in the catalog > any economy-tier model > literal
  fallback `claude-haiku-4-5`
- Emits `context_compacted{messages_summarized, original_tokens, summary_tokens, model_used, tokens_in, tokens_out}`

If compaction fails (model unreachable, no haiku in catalog), the run
continues with the original messages; the next call hits
`context_exhausted` naturally and surfaces the right error kind.

## Per-call deadline (B.7)

Every upstream model call is wrapped in `asyncio.wait_for(..., timeout=...)`
where `timeout = min(provider.timeout_sec or 60, run.deadline_ts - now)`.
On `ConnectTimeout`, `ReadTimeout`, or `asyncio.TimeoutError`: fail-over to
the next provider **immediately**. The 600s-hang anti-pattern is structurally
impossible.

## Idempotency

`(api_key_id, idempotency_key)` → `run_id` map; 24h TTL anchored at
`created_at`. Duplicate POST within window returns the existing run regardless
of state.

In-process LRU cache (10k entries) sits in front of the DB lookup; cache
miss falls through to DB and warms cache on hit.

## Observability

Per-run state goes to:
- **SSE event stream** (real-time, sub-100ms via in-memory broker; 1000-event
  ring per run for `Last-Event-ID` resume)
- **DB rows** (`run_events` table, durable; powers polling and post-hoc
  inspection)
- **Activity log** (each `model_call_*` event also lands as a row with
  request/response payloads)
- **OTEL spans** (gen_ai semantic conventions; honors caller-provided
  `trace_id` as parent for cross-component tracing)

## Common operations

### Tune per-run rate limit
```
PUT /api/settings/llm-providers
{"runs_max_model_calls_per_minute": 10}
```

### Force-cancel a stuck run
```
POST /v1/runs/<id>/cancel
```
Idempotent. Worker exits within ~250ms via the wakeup `Event`.

### Inspect events for an in-flight run
```
GET /v1/runs/<id>/events?since_ms=0&limit=1000
```

### Adopt a run when the owner is dead
```
POST /v1/runs/<id>/adopt
```
Refuses with 409 unless the owner has been unreachable for >30s. After
adopt: 307 redirects from other peers all flow here.

### Increase max-turns ceiling for a long-running investigation
```
PUT /api/settings/llm-providers
{"runs_max_turns_ceiling": 100}
```
Hard cap remains 200; takes effect on next Run create.

## Failure modes (what each error kind means)

| `error_kind` | Cause | Operator action |
|---|---|---|
| `error_provider` | All providers in the chain returned non-retriable failures | Check provider health (CB state, recent failures); possibly enable a fallback provider |
| `tool_loop_exceeded` | Run hit `max_turns` while still emitting tool_use blocks | Inspect tool_use trail in events; either bug in the model or in the tool catalog (model is calling the same tool repeatedly because it's not getting the answer it expects) |
| `context_exhausted` | Conversation exceeds context limit even after compaction | Either pass a larger-context model in `model_preference`, or configure a more aggressive `compaction_model` |
| `bad_request` | Validation failure mid-run (rare; payload mutation drift) | Inspect Run message history; usually a malformed tool_result POST |
| `internal` | Unexpected exception in the worker loop | Logs will show the traceback; file a bug |

## What v3.0.0 ships vs deferred

**Shipped:**
- Full state machine + persistence + idempotency
- Per-call hard deadline + provider failover (B.7)
- Recovery sweep + cluster handoff via `/adopt`
- Context compaction
- SSE event broker with Last-Event-ID resume
- Per-Run rate limit
- Tool spec translation (Anthropic → OpenAI per provider native_tools)
- 792 unit tests passing

**Deferred past v3.0:**
- `model_token` per-token streaming events (UI-only, high implementation cost)
- WebSocket alternative to SSE (POST-for-write + SSE-for-read covers spec)
- Tool-scoped permissions (current auth is Bearer-only per spec B.15)

## Joint-smoke history

End-of-week-2 smoke (against `v3.0.0-r4`): 5/5 GREEN per coordinator-hub
team's verdict.
End-of-week-3 mock: contract-shape verification of `run_recovered` GREEN.
Two minor smoke-node bugs surfaced + fixed in the same release:
- Cookie path scoped to `/llm-proxy2/` (now `Path=/`)
- `spending_cap_usd=-1` enforced as $-1 (now treated as unlimited)
