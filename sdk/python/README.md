# lmrh_client — Python SDK for LMRH v2

Reference implementation of the LMRH v2 polling + hint-synthesis loop.
Single-file, single-dependency (`httpx`), drop into any caller that
wants to participate in the bidirectional protocol.

## Install

```bash
pip install httpx  # only runtime dependency
# Vendor lmrh_client.py into your repo, or pip-install once published.
```

## Quick start

```python
from lmrh_client import LmrhClient

client = LmrhClient(
    base_url="https://www.voipguru.org/llm-proxy2",
    api_key="llmp-...",
)
client.start()  # background polling thread

# Build an optimal hint for a request
hint = client.build_hint(
    task="chat",
    prefer="cheapest",
    model_family="claude",
)
# hint is e.g.
#   "task=chat, cost=economy, provider-hint=anthropic|claude-oauth|anthropic-oauth"

# Pass to /v1/messages
resp = httpx.post(
    "https://www.voipguru.org/llm-proxy2/v1/messages",
    headers={
        "x-api-key": "llmp-...",
        "anthropic-version": "2023-06-01",
        "LLM-Hint": hint,
    },
    json={"model": "claude-sonnet-4-6", "max_tokens": 100,
          "messages": [{"role": "user", "content": "hi"}]},
)

# Inspect the snapshot directly for custom logic
snap = client.snapshot()
for p in snap.providers:
    print(f"{p.name:30s} pri={p.priority} circuit={p.circuit}")
    for m in p.models:
        print(f"  {m.model_id} p50={m.metrics.latency_p50_ms}ms "
              f"samples={m.metrics.samples}")

client.stop()  # on shutdown
```

## SSE push (v3.5.2+) — `subscribe()`

If the proxy is **v3.4.0 or newer**, you can use Server-Sent Events
push semantics instead of polling. The proxy emits a fresh snapshot
when its underlying state changes (max ~30s latency vs polling's
60-90s) and a heartbeat (`: ping`) every 25 seconds to keep the
connection alive through proxy idle timeouts.

```python
from lmrh_client import LmrhClient
import threading

client = LmrhClient(
    base_url="https://www.voipguru.org/llm-proxy2",
    api_key="llmp-...",
)

def on_snapshot(snap):
    print(f"Got {len(snap.providers)} providers, etag={snap.etag}")
    # ... your routing logic here ...

# subscribe() blocks the calling thread; spawn it in a daemon
# thread for non-blocking usage.
threading.Thread(
    target=lambda: client.subscribe(on_snapshot=on_snapshot),
    daemon=True,
).start()
```

**Auto-reconnect**: if the connection drops (network blip, proxy
restart) the method waits `reconnect_delay_sec` (default 5s) and
retries. Loops until `client.stop()` is called or the proxy returns
a permanent 404.

**Auto-fallback**: if the proxy is older than v3.4.0 (no
`/lmrh/stream` endpoint), `subscribe()` transparently falls back
to polling — same callback signature, same caller code. You get
push when available, polling otherwise.

**When to prefer SSE over `start()`**:
- Freshest possible metrics (push latency ≈ 30s vs polling 60-90s)
- Behind a flaky NAT / firewall: one long-lived connection is
  often more reliable than many short polls
- High-volume callers where rate-limit headroom on
  `/lmrh/providers` is precious

**When `start()` (polling) is still the right choice**:
- Behind a strict corporate egress proxy that truncates long-lived
  HTTP connections
- Simpler error semantics (poll-failures don't require reconnect
  logic in your callbacks)
- Connecting to a proxy older than v3.4.0 (use `start()` directly
  to skip the well-known probe round-trip on `subscribe()`)

## Design

- **One polling thread per `LmrhClient` instance.** Polls
  `/lmrh/providers` every 60 s (proxy's recommended cadence).
  ETag-aware — most polls return 304 with no parse cost.
- **Sync httpx client.** Callers don't need an event loop. If you're
  already in async, the polling still happens on its own thread; the
  accessors (`snapshot()`, `build_hint()`) are thread-safe.
- **Graceful degradation.** If the proxy returns 404 (v2 not enabled,
  or older proxy), `is_supported()` flips to False and `build_hint()`
  still returns a valid LMRH 1.x hint string.
- **No dependency on proxy internals.** The client builds hints using
  only the public dim names. Adding a new dim on the proxy doesn't
  require an SDK update.

## Hint synthesis

`build_hint()` accepts:

| Arg | Effect |
|---|---|
| `task=` | Sets the `task=` dim |
| `prefer="cheapest"` | Sets `cost=economy` |
| `prefer="fastest"` | Sets `latency=interactive` |
| `prefer="most_reliable"` | Picks the provider with highest `success_rate × log(samples)` and pins via `provider-hint` |
| `model_family="claude"` | Adds `provider-hint=anthropic\|claude-oauth\|anthropic-oauth` |
| `region="us"` | Adds `region=us;require` |
| `require_tools=True` | Adds `tools=required` |
| `require_vision=True` | Adds `vision=required` |
| `cache="ephemeral"` | LMRH 1.2 §E2 cache mode |
| `extra={...}` | Last-write-wins arbitrary dim=value pairs |

## Backward compatibility

- LMRH 1.x callers can drop in `lmrh_client.py` without code changes
  to their existing inference calls. If `is_supported()` is False,
  `build_hint()` still returns a valid hint that v1.x proxies parse.
- `snapshot()` returns `None` until the first poll completes (~5 s
  after `start()`). Callers should null-check or set `prefer=` to a
  static value (which doesn't require snapshot data).

## Tests

```bash
python -m pytest sdk/python/test_lmrh_client.py -v
```
