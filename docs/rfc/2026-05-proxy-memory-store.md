# RFC: Proxy-side memory store (proposed v3.9.0)

**Status**: draft for operator review
**Issue**: #267 (operator-set 2026-05-13)
**Owner**: llm-proxy2

## Background

LLM providers expose persistent memory features that survive across
separate API requests:

| Provider | Memory surface | Where it lives |
|---|---|---|
| Anthropic | `memory_blocks` / `memory tool` in /v1/messages | Per-conversation, server-side, scoped to org API key |
| OpenAI | Assistants API threads + the new `chat.completions` memory tool | Per-thread, server-side, scoped to org API key |
| Google (Gemini) | Implicit memory via Vertex agent endpoints | Server-side |
| Grok | None first-party; only inline conversation history works |
| Cohere | Conversational memory in Chat v2 API | Per-conversation_id, server-side |

The llm-proxy2 routes requests across providers per-call (LMRH scoring,
fallback chains, cost-tier swaps, etc.). When a caller relies on
provider A's memory and the next request lands on provider B, the
memory state is invisibly fragmented:

- Caller sends "remember X" → routed to provider A → A's memory stores X
- Next request from same caller → router picks provider B (different) → B has no memory of X
- Caller sees inconsistent answers; the proxy has masked the fragmentation
- Operator's API key is paying for memory storage at provider A while the caller is talking to B

**Operator directive** (2026-05-13): proxy-side memory store as KING of
memory state. Cluster-replicated across nodes for full-mesh redundancy.
Provider-side memory is either flushed or kept in sync with our king
store. Redis or similar.

## Goals

1. Caller-visible memory state is consistent across all routed providers
2. Each node has a full copy of the memory store (mesh redundancy)
3. Memory writes survive any single node failure
4. No memory state stranded at a provider after we route away from it
5. Operator can inspect / clear memory per api_key from admin UI

## Non-goals

- Replacing per-conversation history (the `messages[]` array). Callers
  still pass that. Memory is *across-conversation* persistent state.
- Embeddings / semantic search over memory. That's separate
  infrastructure (we already have semantic cache for similar requests).
- Cross-tenant memory sharing. Memory is strictly scoped per api_key.

## Architecture

### Storage backend

We already use Redis in 2 places (`app/cot/session.py`, `app/cache/semantic.py`)
so Redis is the established backend. **Proposal**: per-node Redis with
cluster-sync orchestrating cross-node replication.

```
[caller] → [www01 proxy] ──┬─→ www01 Redis (local write)
                           └─→ POST /cluster/sync (payload includes memory updates)
                                ↓
                           [www02 proxy] → www02 Redis (apply)
                           [c1conv proxy] → c1conv Redis (apply)
```

**Why per-node Redis instead of shared central**:
- Reads are local → zero network hop in the hot path
- Single Redis is a single point of failure; per-node + sync = mesh
- Matches the existing Provider / ApiKeyAiReview / ExternalUsageSnapshot
  sync pattern that's been proven across 30+ ships

**Why not Redis Cluster / Sentinel**:
- Adds operational complexity (Sentinel quorum, cluster slot
  management)
- Our scale is small (3 nodes); app-level LWW sync is sufficient
- Easier to debug — each node is identical

**Fallback when local Redis is down**: SQLite-backed in-process store,
same fallback pattern `app/cot/session.py` already uses. Stale-data
risk during fallback is accepted (operator alerted via existing health
check + notification path).

### Schema

New table `caller_memory` (durable, also cached in Redis for read perf):

```python
class CallerMemory(Base):
    __tablename__ = "caller_memory"
    id = Column(Integer, primary_key=True)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)  # operator/caller-supplied tag
    memory_tag = Column(String, nullable=False, default="default")  # semantic label
    content = Column(Text, nullable=False)  # the memory text/JSON
    content_format = Column(String, default="text")  # "text" | "json" | "anthropic_memory_blocks"
    updated_at = Column(Float, nullable=False)  # unix ts for LWW
    updated_by_node = Column(String, nullable=True)  # which node wrote it
    # Optional metadata
    source_provider_id = Column(String, nullable=True)  # which provider wrote it last
    source_request_id = Column(String, nullable=True)
    # Soft delete for tombstone-propagation through cluster sync
    deleted_at = Column(Float, nullable=True)
```

PK is auto-int per node; cluster sync dedups by
`(api_key_id, conversation_id, memory_tag)`. LWW by `updated_at`.

Redis key shape: `llmproxy:mem:{api_key_id}:{conv_id_or_default}:{tag}`.

### Memory keying

3-tuple: `(api_key_id, conversation_id, memory_tag)`.

- **api_key_id** — the operator's proxy api key (NOT the upstream
  provider's api key). Scopes memory per-caller automatically.
- **conversation_id** — optional, caller-supplied via `X-Conversation-Id`
  header. Lets a single api_key have multiple parallel conversations.
  Default: literal `"default"`.
- **memory_tag** — semantic bucket. Defaults: `"default"`, but callers
  can use `"system_prefs"` / `"user_facts"` / etc. for namespacing.

### Request flow

For every /v1/messages or /v1/chat/completions request:

1. **Resolve memory key** from `api_key_id` + `X-Conversation-Id`
   header + optional `X-Memory-Tag` header
2. **Inject memory** into the outgoing upstream request:
   - For Anthropic providers: inject as `memory_blocks` in body
   - For OpenAI providers: inject as a system message prefix
   - For Gemini / others: same system-prefix injection (translation
     to the upstream's native shape happens at the dispatch layer)
3. **Call upstream** as usual
4. **Capture memory updates** from response:
   - For Anthropic memory tool: parse `tool_use` blocks where
     `name == "memory"` from the response, apply the
     create/update/delete operation to our store
   - For OpenAI Assistants memory: TBD (likely need a separate
     polling path against their `/threads/{id}/messages` endpoint)
   - For implicit memory: NO automatic capture (caller-side concern)
5. **Persist update** to local Redis + SQLite + sync payload

### Provider-side memory flush

When a caller's request routes to a NEW provider (different from the
last provider that wrote memory):

1. Read `source_provider_id` from the previous CallerMemory row
2. If non-null AND ≠ current routed provider:
   - **Anthropic**: emit a memory-clear request to provider A's
     /v1/messages with empty memory_blocks. Confirms our store is
     authoritative.
   - **OpenAI Assistants**: delete or archive the upstream thread.
   - **Cohere**: emit a delete to their `/v2/chat/clear`.
   - **Others without explicit clear**: noop (memory is bound to
     conversation_id we provide; new conversation = fresh state).
3. Update `source_provider_id` to the current provider after the
   successful call.

### Cluster sync

Reuses the existing `/cluster/sync` endpoint:
- Outgoing payload (in `_build_sync_payload`): add `caller_memory`
  section, last 7 days of rows (older rows are operator-audit-only)
- Incoming apply path (in `apply_sync`): new
  `_apply_caller_memory` handler with LWW merge by
  `(api_key_id, conversation_id, memory_tag)` and `updated_at`
- Redis cache invalidation: when an incoming sync writes a row, also
  set/del the corresponding Redis key

### Admin surface

- `GET /api/keys/{id}/memory` — list memory rows for a key
- `GET /api/keys/{id}/memory/{conversation_id}/{tag}` — read one row
- `DELETE /api/keys/{id}/memory/{conversation_id}/{tag}` — clear one row
- `DELETE /api/keys/{id}/memory` — clear all memory for a key
- New "Memory" tab on the API Keys detail page in the UI

## Locked decisions (2026-05-13 operator review)

1. ~~Storage backend~~: **Per-node Redis + SQLite fallback**. Each node
   has its own Redis (host.docker.internal:6379 already used by
   cot_session). Reads local, writes go via cluster-sync to peers.
   SQLite `caller_memory` is durable source-of-truth + cluster-sync
   transport; Redis is hot read-cache.
2. ~~Memory keying~~: 3-tuple `(api_key_id, conversation_id, memory_tag)`
   (no objection from operator → proceed with proposed default).
3. ~~Provider flush behavior~~: **Active flush** (best fidelity). Proxy
   emits clear-memory to provider A when routing to provider B.
   Anthropic: memory_blocks=[]. OpenAI Assistants: thread delete.
   Cohere: /v2/chat/clear. Best-effort; log + continue on failure.
4. ~~Implicit cross-conversation memory~~: **Disable at provider-config**.
   When the proxy first uses a new provider's API key, attempt to
   disable account-wide memory via the vendor's API. Re-disable
   periodically to catch drift. Operator gets a warning if a provider
   can't be disabled (e.g. closed-API providers).
5. ~~TTL~~: **Never expire automatically**. Memory rows persist until
   admin DELETE. **Plus** a new requirement (operator-added):
   **back-pressure recovery** — if the proxy loses local memory state
   (DB restore, upgrade with migration drop, container wipe), the
   proxy must be able to ask the upstream provider "give me full
   history for this conversation_id" and reconstruct what it can.
6. Header names: proceed with `X-Conversation-Id` + `X-Memory-Tag`
   (no operator objection → defaults stand).

## Back-pressure recovery (decision 5b)

**Problem**: never-expire memory means a DB restore / upgrade that
drops the `caller_memory` rows leaves the proxy thinking each request
is fresh, while the upstream provider may still have the memory state
on their side. Caller's conversation continuity is silently broken.

**Solution**: persistent "memory marker" row per `(api_key_id,
conversation_id, memory_tag)` recording **that memory exists** even
if the content blob is empty/missing. On a request where the marker
exists but content is empty/null, the proxy triggers a recovery path:

1. Look up `last_known_provider_id` from the marker
2. Call that provider's "list / view memory" endpoint (vendor-specific):
   - **Anthropic**: memory tool's `view` action
   - **OpenAI Assistants**: `GET /threads/{thread_id}/messages`
   - **Cohere**: `GET /v2/chat/conversations/{conv_id}`
   - **Gemini**: depends on Vertex agent endpoint — TBD
3. Reconstruct the local content from what the provider returns
4. Mark the marker as `recovered_at=now()`
5. Continue with the request, using the recovered content

**Schema addition** for back-pressure:

```python
class CallerMemoryMarker(Base):
    """Persistent existence-marker for back-pressure recovery.

    Lives separately from the content rows so a DB restore that loses
    `caller_memory` content rows can still recover via the marker
    + the upstream provider's surviving state."""
    __tablename__ = "caller_memory_marker"
    id = Column(Integer, primary_key=True)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    memory_tag = Column(String, nullable=False, default="default")
    first_seen_at = Column(Float, nullable=False)
    last_known_provider_id = Column(String, nullable=True)  # for vendor-specific recovery call
    last_known_external_ref = Column(String, nullable=True)  # provider's thread_id / conversation handle
    recovered_at = Column(Float, nullable=True)  # set when a recovery succeeded
```

The marker is created on first memory write and updated whenever
memory crosses to a new provider. It's INTENTIONALLY simple and
small so it survives backups easily (one row per conversation, ~100
bytes each). On lossy upgrades the operator would explicitly re-import
the marker table from a snapshot if even those rows are missing.

## Effort estimate

| Phase | Scope | Estimate |
|---|---|---|
| 1 | RFC review + lock decisions | ✅ DONE 2026-05-13 |
| 2 | `CallerMemory` + `CallerMemoryMarker` tables + migrations + cluster sync entry | 1 ship |
| 3 | Redis read/write layer + SQLite fallback (`app/memory/store.py`) | 1 ship |
| 4 | Memory-injection middleware on /v1/messages + /v1/chat/completions | 2 ships |
| 5 | Anthropic memory-tool extraction + write-back | 1 ship |
| 6 | Provider-side flush handlers (per-vendor) | 1-2 ships |
| 7 | Back-pressure recovery path (vendor-specific list/view endpoints) | 1-2 ships |
| 8 | Implicit-memory disable-at-config (per-vendor "turn off account-wide memory" calls) | 1 ship |
| 9 | Admin API + UI panel | 2 ships |
| 10 | Operator opt-in flag default OFF + observation period | 1 ship |
| **Total (Phases 2-10)** | | **11-13 ships ≈ 2 working days** |

## Risks

| Risk | Mitigation |
|---|---|
| Memory leak across api_keys | Strict scoping by api_key_id; no shared state; per-row deletion path |
| Stale memory in fallback (Redis down) | Existing fallback pattern in cot/session.py — accept staleness, alert operator |
| Cluster-sync amplification | Memory updates are low frequency (op-level, not message-level); same sync cadence (60s) absorbs the load |
| Provider-side flush fails | Best-effort: log + continue. Worst case is duplicate memory on provider side, not data loss |
| Caller doesn't know memory exists | Memory is OFF by default per api_key. Caller must set a header to opt in |

## Migration / rollback

- Tables / migrations are additive; safe to deploy ahead of feature flag
- Feature flag `caller_memory_enabled` default OFF → no behavior change at deploy
- Memory injection middleware is gated on flag; existing requests untouched
- Rollback: flip flag off → middleware no-ops; data preserved in
  `caller_memory` table for forensic review

## Effort by phase (concrete)

Phase 2 (1 ship): `CallerMemory` SQLAlchemy model + idempotent ALTER
TABLE migration + cluster-sync `caller_memories` payload section +
`_apply_caller_memory` handler with LWW merge. No behavior change yet.

Phase 3 (1 ship): `app/memory/store.py` — read-through / write-through
to Redis with SQLite fallback. Reuses `app/cot/session.py` Redis client
pattern. Settings: `caller_memory_enabled`, `caller_memory_redis_ttl_sec`
(default null = no expiry).

(Phases 4-8 sketched in the estimate table above.)

---

**Operator action requested**: review Open Questions 1-6 above. Once
locked, ship Phase 2 + 3 first (data layer + storage backend, no
behavior change), observe for a day, then continue with Phase 4
(memory-injection middleware).
