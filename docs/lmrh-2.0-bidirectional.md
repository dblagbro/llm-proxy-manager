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
            "samples": 16
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

- **v3.3.0** (2026-05-09): Phase 1 ships — `/.well-known/lmrh-config`,
  `/lmrh/providers`, `/lmrh/providers/{id}`, `/lmrh/health`. Snapshot
  module, ETag round-trip, per-key rate limits, `Link` header,
  feature flag default-off.
- **v3.3.1** (2026-05-09): Phase 2 ships — `/lmrh/quotes` dry-run
  scoring + Python SDK reference at `sdk/python/lmrh_client.py`.

## Future work (deferred)

- **Phase 3**: subscription-quota disclosure (`session_used_pct`,
  `weekly_used_pct`, `session_resets_at`) in `/lmrh/providers` for
  callers whose key can route to claude-oauth / codex-oauth /
  grok-web.
- **Phase 4**: SDK adoption with one downstream caller validating
  the API shape before publishing the SDK to PyPI.
- **Per-input/output cost split**: today the snapshot reports a
  combined cost rate; per-direction split needs a metrics-writer
  refactor.
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
