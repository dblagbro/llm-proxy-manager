# LMRH 1.1 — Self-Extension Protocol (Cross-Project Announcement)

**Status:** Shipped in llm-proxy-manager v3.0.25.
**Audience:** All teams currently using or considering LMRH (`LLM-Hint`
request header) against any deployment of the proxy.
**Spec:** see `docs/draft-blagbrough-lmrh-00.md` (now draft-blagbrough-lmrh-01,
LMRH 1.1).

---

## TL;DR

You can now invent your own LMRH dimensions and have the proxy adopt them
as canonical, cluster-replicated names — no source-code change, no spec
update, no waiting on the proxy team.

Two new built-in dimensions ship with 1.1: `exclude=` and `provider-hint=`.

A new diagnostic response header, `X-LMRH-Warnings`, tells you when the
proxy saw a dimension it does not recognize.

---

## What changed and why

LMRH 1.0 had a closed dimension registry. Adding a new dimension required
a code change to the proxy, a redeploy, and (eventually) a spec update.
Several teams hit this wall when they wanted hints that were specific to
their own application logic — `audit-mode`, `tenant-tier`, `retry-budget`,
etc.

LMRH 1.1 keeps the canonical name space (still the whole point of a
registry) but lets clients extend it via runtime endpoints with a
collision-resolving handshake. The proxy's routing scorer continues to
ignore any dimension it has no semantics for, so the registry remains a
*name space*, not a behavior dispatch — registering a new dim does not
automatically change routing decisions, it just gets the name allocated
and replicated across the cluster.

---

## New built-in dimensions

### `exclude=PROVIDER1,PROVIDER2`

Comma-separated list of provider names or provider types to steer away
from. Soft form is a strong negative bias; with `;require`, matching
providers are removed from the candidate pool entirely.

```
LLM-Hint: exclude=anthropic-direct
LLM-Hint: exclude=openai,codex-oauth;require
```

Use cases: a tenant-specific quota incident, a known refusal regression,
caller-side awareness of an upstream outage that hasn't yet been reflected
in the proxy's circuit breaker.

### `provider-hint=PROVIDER1,PROVIDER2`

The inverse: positive bias toward listed providers. With `;require`, only
those providers may be chosen.

```
LLM-Hint: provider-hint=anthropic-oauth, anthropic-direct
```

Names match against both display name and provider type, case-insensitive.

`exclude` and `provider-hint` may appear together; the proxy treats the
intersection as the candidate pool.

---

## Self-extension endpoints

All endpoints are mounted at the proxy root (e.g.
`https://www.voipguru.org/llm-proxy2/lmrh/...`).

### `POST /lmrh/register` — allocate a dimension name (auth-required)

```http
POST /lmrh/register HTTP/1.1
Authorization: Bearer <your-llmp-key>
Content-Type: application/json

{
  "name":         "audit-mode",
  "owner_app":    "ai-analyzer",
  "semantics":    "Record an audit trail row for this request",
  "value_type":   "token",
  "kind":         "advisory",
  "examples":     ["audit-mode=on", "audit-mode=off"]
}
```

Response:

```json
{
  "accepted":       true,
  "canonical_name": "audit-mode",
  "requested_name": "audit-mode",
  "suffix_applied": false,
  "note":           null
}
```

**Collision behavior:** if `audit-mode` is already registered to a
different owner, the proxy allocates `audit-mode-2`, then `-3`, etc.,
returns `suffix_applied: true`, and gives you the canonical name in
`canonical_name`. **You must use the returned `canonical_name`** in
subsequent requests — do not assume the requested name was allocated.

**Idempotency:** repeated registration with the same `(name, owner_app,
owner_key_id)` returns the existing record unchanged with a `note`
indicating idempotent re-register. Safe to call on every app startup.

**Replication:** registrations propagate to all cluster peers within ~60s
via the existing cluster-sync push.

### `POST /lmrh/propose` — request a curated dim (auth-required)

For dims you believe should live in the canonical built-in set rather than
the runtime registry, this endpoint queues a free-form rationale for
operator review:

```json
{
  "proposed_name": "session-replay-id",
  "rationale":    "Tying together a multi-turn session for audit retrieval"
}
```

Status transitions (`pending` → `accepted` | `rejected`) are operator
driven and replicate across the cluster.

### `GET /lmrh/registry` — discovery (public)

Lists every dimension the proxy currently understands — built-ins plus
runtime registrations. Useful at startup to learn what canonical names
exist before allocating your own.

```json
{
  "builtins": ["task", "safety-min", "safety-max", "modality", "region",
               "latency", "cost", "context-length", "max-ttft",
               "max-cost-per-1k", "exclude", "provider-hint", ...],
  "registered": [
    {"name": "audit-mode", "owner_app": "ai-analyzer",
     "registered_at": 1714540800.0, "kind": "advisory", ...}
  ]
}
```

### `GET /lmrh/registry/{name}` — single-dim lookup (public)

Returns a single dim's metadata, or 404. Use this to verify a name is
registered before relying on it.

---

## New diagnostic header: `X-LMRH-Warnings`

When the proxy receives an `LLM-Hint` request that contains a dimension it
does not recognize, the response carries:

```http
X-LMRH-Warnings: unknown-dim:retry-policy,unknown-dim:tenant-tier register-at:/lmrh/register spec:/lmrh.md
```

The warning is a comma-separated list of `unknown-dim:NAME` tokens, plus
two space-separated discovery hints (`register-at:` and `spec:`) so the
header carries everything a curious client needs to learn the canonical
name space without an out-of-band lookup.

This is **purely diagnostic** — unknown dims are still silently ignored by
the routing pipeline (the LMRH 1.0 rule). The warning lets you notice
typos and discover what canonical names exist before you go register your
own.

---

## Migration guide

### If you only use built-in dims

No action required. Your existing `LLM-Hint` headers continue to work.

### If you've been documenting a "future" dim in your code

Pick one:

1. **Use it as-is** — the proxy ignores unknown dims, so this is safe. The
   `X-LMRH-Warnings` header will note them.
2. **Register it** — once, on app startup, POST to `/lmrh/register` with a
   stable `owner_app` value. Subsequent runs are idempotent.
3. **Propose it** — if you believe it deserves a built-in slot, POST to
   `/lmrh/propose` and we'll review.

### If you want to steer routing per-request

`exclude=` and `provider-hint=` are now first-class. No registration step
needed; they work today against v3.0.25 deployments.

---

## What does NOT change

- The closed routing scorer. Registering a dim allocates a *name*; it does
  not register *behavior*. The proxy still only acts on dims it has built
  in scoring logic for. If you register `audit-mode`, the proxy will not
  start writing audit logs — your application reads its own dim from the
  request and acts on it.
- The `LLM-Capability` response header schema. Same keys, same semantics.
- Authentication. Registration is auth-required; both `POST /lmrh/*`
  endpoints require a valid llmp-* key. `GET /lmrh/registry*` is public.
- Backwards compatibility. LMRH 1.0 clients are 100% compatible with 1.1
  proxies and vice versa. The new endpoints are purely additive.

---

## Reference implementation

- Source: `app/api/lmrh.py` (endpoints), `app/routing/lmrh/score.py`
  (`exclude` and `provider-hint` scoring), `app/main.py`
  (`X-LMRH-Warnings` middleware).
- Models: `LmrhDim`, `LmrhProposal` in `app/models/db.py`.
- Cluster replication: `app/cluster/manager.py` (push payload),
  `app/cluster/sync.py` (receive merge).

---

## Questions, bug reports, dim proposals

Open them against the llm-proxy-manager repo or send them via the standard
project channel. Operator-side review of `/lmrh/propose` submissions is
weekly.

---

To: All proxy-using teams (DevinGPT, AI Analyzer, AI Tax Analyzer,
coordinator-hub, paperless-ai-analyzer, others)
Sign from: llm-proxy-manager team — v3.0.25 ship notes
