# LMRH 2.0 — Bidirectional Routing Metadata

> **Status**: Ships v3.3.0+ (endpoints, snapshot, Link/Version headers).
> v3.3.1 adds `/lmrh/quotes` dry-run scoring + Python SDK reference.
> Default-off via the `lmrh_v2_enabled` runtime flag.
>
> **Audience**: callers of llm-proxy2 who already use LMRH 1.x and want
> to participate in the new feedback channel.

---

## Why bidirectional

LMRH 1.x is a one-way protocol: clients send the `LLM-Hint:` request
header (RFC 8941 Structured Fields) describing what they want — task
class, cost tier, region, latency expectation, cache mode — and the
proxy decides which provider+model serves the call.

This works but the caller is guessing at every dimension. They don't
know:

- Whether their preferred provider is currently available, degraded,
  or circuit-broken.
- How much rated cost a request would burn against the operator's
  subscription quota right now.
- Which provider is currently fastest at p50 latency for this region.
- Which models the proxy actually has scanned + scored.

LMRH 2.0 closes that gap. The proxy publishes the same data it uses
internally for routing, and clients periodically pull a snapshot to
craft optimal `LLM-Hint` headers for their next request.

This is the LLM-routing analog of:
- HTTP `Vary` + content negotiation (server tells client what axes
  it varies along)
- DNS SRV records (clients pick backends by priority + weight)
- Service-mesh telemetry (latency/error data flows out from
  routers to consumers)

## Backward compatibility

Every LMRH 1.x client keeps working. v2 is purely additive:

- All v1.x dim names, the request-header parser, and routing
  behavior are unchanged.
- New response headers (`Link`, `LMRH-Version`, `LMRH-Hint-Echo`)
  are ignored by v1.x clients.
- New endpoints are 404 unless `lmrh_v2_enabled=true` on the proxy.

You can ignore v2 indefinitely. Or pull `/lmrh/providers` once,
synthesize hints, and your existing v1.x request shape stays the
same — only the value of `LLM-Hint` changes.

## Endpoints

All under the proxy's existing base URL (e.g. `/llm-proxy2/`).

### `GET /.well-known/lmrh-config`

**Auth**: public (RFC 8615 well-known URI). Returns server metadata
so clients discover what version + endpoints + polling cadence the
proxy supports.

```http
GET /.well-known/lmrh-config HTTP/1.1
```

```json
{
  "version": "2.0",
  "supported_versions": ["1.2", "2.0"],
  "endpoints": {
    "registry": "/lmrh/registry",
    "providers": "/lmrh/providers",
    "health": "/lmrh/health"
  },
  "polling": {
    "providers_min_interval_sec": 15,
    "providers_recommended_interval_sec": 60,
    "providers_max_rate_per_minute": 4,
    "quotes_max_rate_per_minute": 60
  },
  "cache": {
    "providers_max_age_sec": 30,
    "registry_max_age_sec": 3600
  },
  "supported_dims": [
    "task", "cost", "latency", "region",
    "cache", "provider-hint", "exclude",
    "cost-class", "tools", "vision"
  ]
}
```

When `lmrh_v2_enabled=false`, the response advertises only
`supported_versions: ["1.2"]` and a smaller `endpoints` map. v1.x
clients see the v1.x surface and never know v2 is installed.

### `GET /lmrh/providers`

**Auth**: API key via `Authorization: Bearer ...` or `x-api-key`.
Returns a snapshot of providers the caller's key can route to,
each with live metrics.

Default rate limit: **4 requests / minute / key**. Override per-key
via the `ApiKey.lmrh_polling_rpm` admin column. The underlying
snapshot only refreshes every 30 s, so polling faster returns
duplicates.

ETag-cacheable. Pass `If-None-Match: <last-etag>` for `304 Not Modified`
between snapshot refreshes.

```http
GET /lmrh/providers HTTP/1.1
Authorization: Bearer llmp-...
If-None-Match: "abc123def"
```

Optional query params:
- `?type=` — restrict to a single provider_type (e.g. `claude-oauth`)
- `?capability=` — restrict to providers offering a given kind
  (`chat`, `embedding`, `image`, `audio`)

```json
{
  "version": "2.0",
  "as_of": "2026-05-09T05:21:43Z",
  "window_sec": 3600,
  "providers": [
    {
      "id": "8beb17c4bd11de26",
      "name": "Grok-Web-Devin",
      "type": "grok-web",
      "priority": 1,
      "cost_class": "subscription",
      "circuit": "closed",
      "regions": ["us"],
      "models": [
        {
          "model_id": "grok-3",
          "kind": "chat",
          "context_length": 128000,
          "native_tools": false,
          "native_reasoning": false,
          "metrics": {
            "cost_per_1m_input_usd": 0.0,
            "cost_per_1m_output_usd": 0.0,
            "rated_quota_per_1m_input_usd": null,
            "latency_p50_ms": 2682,
            "latency_p95_ms": 3850,
            "ttft_p50_ms": 800,
            "ttft_p95_ms": 1840,
            "success_rate": 0.998,
            "samples": 16,
            "probe_success_rate": 0.74,
            "probe_samples": 23
          }
        }
      ]
    }
  ]
}
```

Field semantics:

- `cost_class: "subscription"` → `cost_per_1m_*_usd` is the actual
  $ charged (`0.0` for OAuth/web subscriptions). The
  `rated_quota_per_1m_input_usd` field, when present, is what the
  same usage would have cost on per-call billing — useful for
  "would I save by routing here" analysis.
- `cost_class: "per_call"` → `cost_per_1m_*_usd` reflects the actual
  rate this provider charges.
- `circuit`: `closed` (healthy), `open` (failed; will be skipped
  for the breaker hold-down period), `half-open` (probing recovery).
  When `open`, sending traffic that requires this provider will
  503 until the breaker closes.
- `samples`: number of requests recorded in the window (default 1h).
  Treat metrics with `samples < 5` as noise.

#### Probe vs user-traffic metrics *(v3.3.3+ semantics)*

The `metrics` block separates **user traffic** from **synthetic
keep-alive probe outcomes**:

| Field | What it measures | Source |
|---|---|---|
| `success_rate` | User-traffic success rate (real callers via `/v1/messages`, `/v1/chat/completions`, etc.) | `provider_metrics` aggregate, probe outcomes excluded since v3.3.3 |
| `samples` | User-traffic request count | `provider_metrics`, probes excluded |
| `probe_success_rate` *(v3.3.4+)* | Synthetic keep-alive probe outcome rate over the same window | `activity_log` rows with `event_type='keepalive_probe'` |
| `probe_samples` *(v3.3.4+)* | Synthetic probe count | same |

Why split:
- A provider can have `success_rate=1.0` (every user request
  succeeded) while `probe_success_rate=0.74` (probes are hitting a
  rate-limit window). That's a **leading indicator**: the
  upstream is throttling, real traffic just hasn't tripped the
  limit yet. Smart callers can use the gap to start gradually
  steering traffic away.
- Conversely, `probe_success_rate=1.0` with `success_rate=0.7`
  signals a user-input quality issue (bad prompts, oversized
  context) rather than an infrastructure problem.
- Probes are spaced 5 min apart and back off exponentially after
  rate_limit (since v3.3.3). `probe_samples` will typically be
  ~12 per hour per probed provider when healthy, lower when in
  back-off.

`probe_success_rate` is `null` and `probe_samples` is `0` for
providers the proxy doesn't probe (per-call providers like Cohere
/ OpenAI are not probed by default — see
`KEEPALIVE_PROBE_PER_CALL_PROVIDERS`). SDK callers should treat
`probe_samples < 3` as too-small-to-trust.

Older proxies (<v3.3.4) omit both fields entirely; SDK callers
must handle that gracefully (the reference Python SDK defaults to
`None` / `0`).

### `GET /lmrh/providers/{id}`

**Auth**: API key. Single-provider deep view. Returns 404 if the
provider doesn't exist OR the caller's key isn't allowed to route
to it (don't leak existence of operator-private providers).

```json
{
  "version": "2.0",
  "as_of": "2026-05-09T05:21:43Z",
  "provider": { ... }
}
```

### `GET /lmrh/quotes?model={model}` *(v3.3.1+)*

**Auth**: API key. "What would happen if I sent a request for `model`
right now?" Returns the proxy's ranked candidate list — same scoring
that `/v1/messages` uses — without dispatching the call.

Default rate limit: **60 requests / minute / key**. Override via
`ApiKey.lmrh_quotes_rpm`.

Optional query params:
- `?hint=task=chat,cost=economy` — full LMRH 1.x hint string,
  applied to scoring
- `?has_tools=true` / `?has_images=true` — capability gates

```http
GET /lmrh/quotes?model=claude-sonnet-4-6&hint=cost%3Deconomy HTTP/1.1
Authorization: Bearer llmp-...
```

```json
{
  "version": "2.0",
  "as_of": "2026-05-09T05:21:43Z",
  "requested": {
    "model": "claude-sonnet-4-6",
    "hint": "cost=economy",
    "has_tools": false,
    "has_images": false
  },
  "candidates": [
    {
      "rank": 1,
      "provider_id": "...",
      "provider_name": "Devin-Anthropic-Max-Gmail",
      "model_id": "claude-sonnet-4-6",
      "score": 0.94,
      "unmet_hints": [],
      "cost_class": "subscription",
      "circuit": "closed",
      "predicted_latency_p50_ms": 1400,
      "predicted_latency_p95_ms": 2200,
      "predicted_cost_per_1m_input_usd": 0.0,
      "predicted_quota_per_1m_input_usd": 3.0,
      "success_rate": 0.998,
      "samples": 600
    },
    { "rank": 2, ... }
  ]
}
```

`unmet_hints` lists the dims this candidate doesn't fully satisfy
(e.g. `["region"]` if you asked for `region=eu;require` and this
provider only serves `us`). When `unmet_hints` is non-empty for
all candidates, `/v1/messages` would still pick rank 1 and emit
`X-Resolved-Provider` + the standard substitution disclosure
headers from LMRH 1.2 §E3.

If no provider can satisfy the hard constraints, `/lmrh/quotes`
returns `503` with the same error message `/v1/messages` would
return for the same request.

### `GET /lmrh/health`

**Auth**: API key. Aggregate health summary for the caller's
visible providers.

```json
{
  "version": "2.0",
  "as_of": "2026-05-09T05:21:43Z",
  "last_snapshot_age_sec": 12,
  "total_providers": 7,
  "circuit_open_count": 0,
  "degraded_count": 0
}
```

`degraded_count` = providers with at least one model showing
`success_rate < 0.95` and `samples >= 10` in the snapshot window.

## Discovery

Every `/v1/messages` and `/v1/chat/completions` response carries
two new headers when `lmrh_v2_enabled=true`:

```
Link: </.well-known/lmrh-config>; rel="lmrh-config", </lmrh/providers>; rel="lmrh-providers"
LMRH-Version: 2.0
```

Clients can read these on any normal inference response and follow
`rel="lmrh-providers"` to bootstrap polling — no out-of-band docs
needed.

When the flag is off, only `LMRH-Version: 1.2` is emitted; the
`Link` header is omitted.

### Per-node ETag — what to know

Each cluster node maintains its own snapshot, derived from the node-
local `provider_metrics` aggregates (which are NOT cluster-replicated;
each node tracks its own dispatch metrics independently). As a
consequence:

- **ETags differ across cluster nodes** for the same logical
  configuration. www01's snapshot ETag will not match www02's even
  when the underlying `Provider` + `ModelCapability` rows are
  identical, because traffic distribution differs and so the
  aggregated latency / success rate / sample counts diverge.
- **Implication for callers behind a load balancer**: round-robin
  DNS or a load-balancer with multiple upstream cluster nodes will
  see ETag drift on every poll that lands on a different node,
  forcing a re-download and defeating the 304 optimization.
- **Recommendation**: pin polling clients to a single cluster node
  (sticky-session or single hostname) for the lifetime of their
  polling session. The `subscribe()` SSE consumer (v3.5.2+) is
  immune to this issue because it uses a single long-lived
  connection.
- **Also note**: `Provider` rows, `ModelCapability` (incl. aliases /
  family / variant), and circuit-breaker state ARE cluster-replicated.
  The ETag drift is purely about metrics aggregates.

## Polling guidance

- **Recommended interval: 60 seconds.** The underlying snapshot
  refreshes every 30 s; polling faster returns duplicates and may
  hit the rate limit.
- **Use ETag conditional GETs**: pass `If-None-Match: <last-etag>`
  → expect `304 Not Modified` between snapshot refreshes. Saves
  bandwidth and reduces proxy load.
- **Minimum interval: 15 s.** Faster polling will hit `429` with
  `Retry-After`.
- **Add jitter**: don't poll on the top of every minute. 50 callers
  polling at `:00` is a thundering herd. Prefer something like
  `60 + random(0, 30)` seconds.

## Per-key overrides

Operator can tune polling rates per API key:

```sql
UPDATE api_keys
   SET lmrh_polling_rpm = 30,    -- this key may poll /lmrh/providers 30/min
       lmrh_quotes_rpm = 600     -- and /lmrh/quotes 600/min
 WHERE name = 'high-volume-orchestrator';
```

`NULL` in either column = use the default (4 / 60).

Setting either to `0` disables that endpoint for that key without
touching the global flag — useful for tenants on a no-LMRH-v2 plan.

## Privacy / scope

The snapshot is **scoped per API key**:

- Only providers whose `Provider.owned_by_key_id` is `NULL` (shared)
  or matches the caller's key id appear in the response.
- Operator-private providers (e.g. someone's personal Claude OAuth)
  remain invisible to other tenants.
- Internal-only fields (`owned_by_key_id`) are stripped from the
  wire response.

The data exposed is what any caller could approximate by sending
many real requests. v2 just makes the trial-and-error step explicit
and structured.

## Reference SDK

A single-file Python SDK lives at
[`sdk/python/lmrh_client.py`](../sdk/python/lmrh_client.py).
It handles polling, ETag round-trip, graceful 404 degradation, and
hint synthesis from caller preferences:

```python
from lmrh_client import LmrhClient

client = LmrhClient(
    base_url="https://your-proxy/llm-proxy2",
    api_key="llmp-...",
)
client.start()

hint = client.build_hint(
    task="chat",
    prefer="cheapest",       # or "fastest" / "most_reliable"
    model_family="claude",
    region="us",
)
# hint = "task=chat, cost=economy, provider-hint=anthropic|claude-oauth|anthropic-oauth, region=us;require"

# Send to /v1/messages with that hint header
# ...

client.stop()
```

`prefer="most_reliable"` weights `success_rate × log(samples)` so
1.0 with 1 sample doesn't beat 0.99 with 600 samples.

If the proxy doesn't support v2 (`/lmrh/providers` returns 404),
`is_supported()` flips to `False` and `build_hint()` still emits
a valid LMRH 1.x hint string — your inference calls keep working.

## Change log

- **v3.10.6** (2026-05-15): Phase 4 begins — v2 endpoints enabled on
  www01 via `LMRH_V2_NODE_OVERRIDE=on`; `/.well-known/lmrh-config`,
  `/lmrh/providers`, `/lmrh/health`, `/lmrh/quotes` + the reference SDK
  validated live; coordinator-hub adoption outreach sent.
- **v3.3.0** (2026-05-09): Phase 1 ships — `/.well-known/lmrh-config`,
  `/lmrh/providers`, `/lmrh/providers/{id}`, `/lmrh/health`. Snapshot
  module, ETag round-trip, per-key rate limits, `Link` header,
  feature flag default-off.
- **v3.3.1** (2026-05-09): Phase 2 ships — `/lmrh/quotes` dry-run
  scoring + Python SDK reference at `sdk/python/lmrh_client.py`.
- **v3.3.3** (2026-05-09): `success_rate` and `samples` are now
  user-traffic only. Synthetic keep-alive probe outcomes are
  excluded from the `provider_metrics` aggregate that feeds these
  fields. Existing fields keep their semantics for callers; the
  apparent reliability of providers that get rate-limited probes
  (e.g. grok-web) goes UP because probe-only failures stop dragging
  the metric down.
- **v3.3.4** (2026-05-09): new `probe_success_rate` +
  `probe_samples` fields exposed on each model — synthetic probe
  outcomes over the same window. Lets callers read connectivity
  health alongside user-traffic reliability. Backward-compatible:
  older clients ignore the extra fields, older proxies omit them.
- **v3.5.0** (2026-05-09): LMRHv2.1 — model identity model. New
  fields on each model entry:
  - `aliases: list[str]` — alternate spellings the proxy will
    accept; `/v1/models` and `/lmrh/providers` list each model once
    with its aliases as a sibling array.
  - `family: str | null` — upstream physical model identity. Two
    `_ModelSnap` entries with the same `family` but different
    `variant` represent multi-route access to the SAME upstream
    model (e.g. grok-3 via the operator's web subscription vs via
    OpenRouter).
  - `variant: str | null` — route flavour ("web", "openrouter",
    "direct", "vertex", etc.).
  Backward-compatible: LMRHv2.0 clients see the new fields as
  unknown JSON keys (ignored). Proxies older than v3.5.0 omit
  them and the SDK applies defaults (`()`, `None`). Drives a small
  bump to `version: "2.1"` in the response body. Both `2.0` and
  `2.1` are advertised in `supported_versions` of
  `/.well-known/lmrh-config`. See the cross-project RFC at
  `docs/rfc/2026-05-model-identity.md` for the full taxonomy and
  how downstream consumers should adopt.
- **v3.4.0** (2026-05-09): Phase 3 ships —
  - **Per-direction cost split**: `cost_per_1m_input_usd` and
    `cost_per_1m_output_usd` are now independently computed from
    new `input_cost_usd` / `output_cost_usd` /
    `input_tokens` / `output_tokens` columns on
    `provider_metrics` (was placeholder same-rate before). Lets
    callers optimize input-heavy or output-heavy workloads
    differently. Schema migration is idempotent + nullable; legacy
    rows fall back to the combined rate.
  - **`GET /lmrh/stream`**: Server-Sent Events endpoint pushes
    full snapshot when ETag changes. Eliminates the polling-then-
    304 dance for clients that prefer push semantics. Same auth +
    scope filter as `/lmrh/providers`. Heartbeat configurable via
    `?heartbeat_sec=` (10-120, default 25) to defeat proxy idle
    timeouts. `/.well-known/lmrh-config` advertises the new
    endpoint and `polling.stream_recommended: true`.
  - **Subscription quota disclosure**: verified working end-to-end
    on three providers (data flow shipped in v3.3.0; this is
    operational confirmation).

## Future work (deferred)

**Status note (2026-05-10)**: the items previously listed here as
"Phase 3 future work" have all shipped between v3.3.0 and v3.4.0
(subscription-quota disclosure, per-direction cost split, SSE
stream endpoint). The remaining real-world gap is **adoption**:
the spec is complete, the SDK is in `sdk/python/`, but zero
downstream callers polled `/lmrh/providers` in 24 h as of
2026-05-10 22:00 EDT. Updating this section to reflect that.

- **Phase 4 — downstream adoption** (in progress, 2026-05-15):
  - v2 endpoints **enabled on www01** via `LMRH_V2_NODE_OVERRIDE=on`
    — the v3.7.18 per-node staged-rollout mechanism. `/.well-known/
    lmrh-config`, `/lmrh/providers`, `/lmrh/health`, `/lmrh/quotes`
    all verified live; the reference SDK validated end-to-end against
    www01 (`is_supported()` → True, hints synthesized for cheapest /
    fastest / most_reliable).
  - Coordinator-hub is the heaviest LMRH 1.x consumer (524 req/24h)
    and the adoption target — outreach sent 2026-05-15.
  - Once the hub is live on the v2 SDK and stable for 7 days: flip the
    cluster-wide `lmrh_v2_enabled` flag (only the www01 node-override
    is on today) and publish the SDK to PyPI under a stable name.

- **Phase 5 — proxy→caller bidirectional metrics feedback**
  (caller-side AI rate limiter design from the 2026-05-10
  7-question discussion — Q5 answer already shipped in v3.7.10/11/12;
  remaining LMRHv2 design questions Q2/Q3/Q4/Q6/Q7 "all 4 as
  proposed" need re-surfaced to operator since the original
  question set isn't in a committed doc).

- **Server-Sent Events push**: today's polling is sufficient at
  60 s cadence; SSE-driven updates are a v3.4+ enhancement if
  callers ask.

## See also

- [`docs/draft-blagbrough-lmrh-00.md`](draft-blagbrough-lmrh-00.md) — original
  LMRH 1.x spec (the foundational protocol)
- [`docs/lmrh-1.1-announcement.md`](lmrh-1.1-announcement.md)
- [`docs/lmrh-1.2-cache-mode-dim.md`](lmrh-1.2-cache-mode-dim.md)
- [`docs/lmrh-1.2-region-pinning.md`](lmrh-1.2-region-pinning.md)
- [`docs/lmrh-1.2-substitution-disclosure.md`](lmrh-1.2-substitution-disclosure.md)
- `architecture.md` — module map (snapshot.py + lmrh_v2.py)
