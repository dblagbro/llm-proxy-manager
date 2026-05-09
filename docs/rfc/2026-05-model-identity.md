# RFC — Model identity model for llm-proxy2 + downstream consumers

**Status**: Implemented in llm-proxy2 v3.4.1 (catalog) + v3.5.0 (LMRHv2.1).
**Author**: llm-proxy2 team.
**Date**: 2026-05-09.
**Distribute to**: coordinator-hub, DevinGPT, paperless-ai-analyzer, and any other
service that calls `GET /v1/models` or polls `/lmrh/providers` against llm-proxy2.

---

## TL;DR

The proxy used to leak the same upstream model under two list entries
(e.g. `grok-3` AND `x-ai/grok-3` both in `/v1/models`) because each
spelling was registered as a separate `ModelCapability` row. v3.4.1
changes the catalog to **one canonical name per model + an `aliases`
array** for alternate spellings. v3.5.0 adds `family` and `variant`
fields so the same physical model accessible via multiple routes
(e.g. grok-web bridge vs OpenRouter marketplace) can be grouped or
disambiguated by callers.

**Backward-compatible.** Old clients keep working. New clients can
opt into the richer identity model when they're ready.

---

## 1. Why

### The problem you may have seen

Polling `GET /v1/models` against llm-proxy2 returned entries like:

```json
{"id": "grok-3", "owned_by": "Grok-Web-Devin", ...},
{"id": "x-ai/grok-3", "owned_by": "Grok-Web-Devin", ...}
```

These are the SAME physical Grok-3 model on grok.com — just two
spellings the operator had to register because the router used
exact-string matching on `model_id`. Pre-v3.4.1 callers saw both,
couldn't tell they were the same, and might pick differently in
their UIs causing surprising routing.

### The deeper question

There are actually **two kinds of "duplicates"** to disambiguate:

1. **Same model, different naming** — `grok-3` vs `x-ai/grok-3`.
   Same physical model. Should be ONE list entry with the
   alternates as aliases.
2. **Same model, different infrastructure routes** — grok-3 served
   by the operator's grok.com web subscription ($0/req, rate-limited)
   vs. grok-3 served via OpenRouter ($0.0001/req, no rate limits).
   These ARE meaningfully different from a routing perspective and
   should remain visible to LMRH-aware callers.

The RFC handles both with one design.

---

## 2. Design

### 2.1 Canonical naming convention

For each upstream model, the proxy picks ONE canonical
`model_id`. The convention (descending priority):

1. **OpenRouter slug** when one exists: `<provider>/<model>` —
   e.g. `x-ai/grok-3`, `openai/gpt-4o`, `anthropic/claude-sonnet-4-5`.
   This is the most widely-adopted naming scheme outside vendors'
   own SDKs and we standardize on it.
2. **Bare vendor name** when no OR slug exists: `claude-sonnet-4-6`
   (Anthropic doesn't ship via OR), `gemini-2.5-flash` (when the
   operator only has a direct provider).
3. **Operator-named** (rare) — operator can override with
   `model_capabilities.model_id` set manually.

### 2.2 Aliases (v3.4.1)

Each `ModelCapability` row now carries a JSON `aliases` column:

```json
{
  "model_id": "x-ai/grok-3",
  "aliases": ["grok-3"]
}
```

The router matches on `model_id == X OR X IN aliases`
(case-insensitive). So callers who send `grok-3` get routed to the
capability whose canonical is `x-ai/grok-3`.

`GET /v1/models` returns each model ONCE under its canonical id
with the `aliases` array as a sibling field:

```json
{
  "object": "list",
  "data": [
    {
      "id": "x-ai/grok-3",
      "object": "model",
      "owned_by": "Grok-Web-Devin",
      "kind": "chat",
      "aliases": ["grok-3"]
    }
  ]
}
```

### 2.3 Family + variant (v3.5.0 / LMRHv2.1)

Each capability also carries optional `model_family` and
`model_variant` columns. They appear in the LMRHv2.1 response:

```json
{
  "model_id": "x-ai/grok-3",
  "aliases": ["grok-3"],
  "family": "grok-3",
  "variant": "web",
  "metrics": { ... }
}
```

When the operator hasn't classified, `family` defaults to the bare
upstream name (strip provider prefix from canonical id). `variant`
is `null` when there's only one route to this family.

Concrete example from the production catalog:

| `model_id` | `family` | `variant` | Provider |
|---|---|---|---|
| `x-ai/grok-3` (Grok-Web entry) | `grok-3` | `web` | Grok-Web-Devin |
| `x-ai/grok-3` (OpenRouter entry) | `grok-3` | `openrouter` | OpenRouter-Devin-Personal |

Same `family`, different `variant`. SDK callers can:

- **Group** (collapse multi-route into one decision point)
- **Pick a specific route** (operator wants grok-web's $0 cost; the
  caller specifies `variant: "web"` or scores by reading metrics)

### 2.4 Versioning

| Surface | Old behavior | New behavior | Breaking? |
|---|---|---|---|
| `GET /v1/models` | Each spelling as separate entry | One entry per canonical id + `aliases` array | **No** — `aliases` is additive; old clients ignore it. Total entry count drops. |
| `GET /lmrh/providers` | (LMRHv2.0) no identity fields | Each model has `aliases`, `family`, `variant` | **No** — unknown JSON keys are ignored. Body `version` bumps from `2.0` to `2.1`. |
| `/.well-known/lmrh-config` | `versions: ["1.2", "2.0"]` | `versions: ["1.2", "2.0", "2.1"]` | **No** — additive |
| Routing — caller sends `grok-3` | Matched only providers with literal `grok-3` capability | Matches canonical `x-ai/grok-3` via alias | **No** — both spellings still resolve |

---

## 3. What downstream consumers should do

### 3.1 If you ONLY consume `GET /v1/models` (most clients)

**No changes required.** The list will get cleaner — you'll see one
`grok-3` instead of two — but the `id` field still works as a
selector for `/v1/messages` and `/v1/chat/completions`.

**Optional improvements**:

- If your UI shows model lists with `aliases` displayed, render the
  alias array as a "Also known as: ..." hint so users understand
  why their preferred spelling resolves correctly.
- If you maintain a hardcoded list of model names in your code, the
  canonical names are now stable: `x-ai/grok-3`, `openai/gpt-4o`,
  `anthropic/claude-sonnet-4-5`, etc. Ditch any `grok-3` / `gpt-4o`
  bare-name strings and use the canonical (or both — both still
  route).

### 3.2 If you poll `/lmrh/providers` for routing decisions

**Recommended**: upgrade to LMRHv2.1 client logic to use `family` for
multi-route awareness. Two patterns:

#### Pattern A — collapse multi-route (simplest)

```python
# Group by family; pick the cheapest variant per family
by_family = {}
for provider in snapshot.providers:
    for model in provider.models:
        by_family.setdefault(model.family, []).append((provider, model))

# Pick variant by your policy — e.g. cheapest
for family, candidates in by_family.items():
    cheapest = min(
        candidates,
        key=lambda c: c[1].metrics.cost_per_1m_input_usd or 0,
    )
```

#### Pattern B — explicit variant choice

```python
# Operator wants grok-3 specifically via the web subscription
desired = next(
    (m for p in snapshot.providers for m in p.models
     if m.family == "grok-3" and m.variant == "web"),
    None,
)
```

Both patterns are stable across proxies older than v3.5.0 — `family`
will be `None`, the SDK applies defaults, your code falls through to
treating `model_id` as the unique key (legacy behavior).

### 3.3 If you maintain a Python SDK consumer

The reference SDK (`sdk/python/lmrh_client.py`) ships with
`ModelEntry` extended for v2.1:

```python
@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    kind: str
    # ... existing fields ...
    metrics: ModelMetrics
    # v3.5.0 / LMRHv2.1
    aliases: tuple[str, ...] = ()
    family: Optional[str] = None
    variant: Optional[str] = None
```

If you've subclassed or copy-pasted this dataclass, mirror the
additions.

### 3.4 If you maintain a Hub UI / admin frontend

The Hub's "Scan Models" view should:

1. Display `aliases` next to each canonical id.
2. Allow operator-driven `model_family` / `model_variant` editing
   on the ModelCapability admin form (currently editable via direct
   DB; UI affordance is a v3.5.x backlog item on the proxy side
   but the Hub team is welcome to ship a UI that PUTs the values
   via the existing capability-edit endpoint).

---

## 4. Migration path

### 4.1 For llm-proxy2 itself

- v3.4.1 ships the schema migration (`ALTER TABLE`s are idempotent)
  and starts populating `aliases` for the grok-web provider type.
  Existing capability rows keep working unchanged with empty
  aliases.
- v3.5.0 adds `family` / `variant` to the schema and the LMRHv2
  output. Existing rows have NULL family/variant; the snapshot
  derives `family` from canonical id at read time so callers
  ALREADY see meaningful values.
- The deprecated bare-name `ModelCapability` rows for grok-web are
  cleared on the next "Scan Models" run (v3.4.1's
  `scanner._fetch_model_list` returns canonical-only).
- For other provider types (OpenAI / Anthropic / Cohere / etc.) no
  alias work is required — their model names are already canonical.

### 4.2 For consumers

- **Phase 0 (now)**: nothing required. Old clients keep working.
- **Phase 1**: when convenient, update SDK to LMRHv2.1 and start
  using `family` for grouping where useful.
- **Phase 2**: drop hardcoded bare-name strings (`grok-3`,
  `gpt-4o`) in favor of canonical (`x-ai/grok-3`,
  `openai/gpt-4o`). Both still route, but canonical is the
  durable choice.

There is **no deadline**. The legacy paths stay working
indefinitely; this is purely a clarity-and-observability
improvement.

---

## 5. Reference: how other tools handle this

| Tool | Approach |
|---|---|
| **OpenRouter** | One canonical slug per model (`<provider>/<model>`). Centralized registry. No alias spellings. |
| **LiteLLM** | Provider-prefixed (`xai/grok-2` — note `xai/` not `x-ai/`). Internal `model_alias_map` for normalization; catalog is canonical-only. |
| **Anthropic / OpenAI direct** | Bare names (`gpt-4o`, `claude-sonnet-4-5`). No prefix. |
| **Vercel AI Gateway / Portkey** | Provider-prefixed; `aliases` field on each model entry listing alternate accept-spellings. |

llm-proxy2 v3.4.1+ aligns with the OpenRouter / Portkey pattern:
canonical slug + aliases array. We add `family` / `variant` because
neither of them models multi-route access to the same upstream
model (which is a llm-proxy2-specific feature given how operators
combine subscription + marketplace + direct routes).

---

## 6. Open questions for downstream teams

1. **Hub team**: do you want a "scan refresh" trigger via
   `coordinator-hub` after v3.4.1 ships so the bare-name capability
   rows get replaced cleanly fleet-wide? Or should we run it
   manually post-deploy?
2. **DevinGPT**: when does it make sense to pin to canonical
   names? If never (you want to keep using `grok-3`), that's fine
   — both spellings keep working — but we'd recommend canonical
   for new code.
3. **Paperless-AI-Analyzer** (currently paused): on resume, please
   confirm if your model selection logic still relies on bare
   names or if it has been updated to use OpenRouter slugs.
4. **All consumers**: any field name pushback? `family` and
   `variant` are llm-proxy2-specific — a different name (e.g.
   `model_group` / `route_kind`) is on the table if it conflicts
   with terminology in your existing UI.

Reply on the coordinator channel or via direct message; the
implementation is shipped but the API shape can still iterate
before it's relied upon at scale.

---

## 7. Appendix — full LMRHv2.1 response example

```json
{
  "version": "2.1",
  "as_of": "2026-05-09T20:00:00+00:00",
  "window_sec": 3600,
  "providers": [
    {
      "id": "8beb17c4bd11de26",
      "name": "Grok-Web-Devin",
      "type": "grok-web",
      "priority": 1,
      "cost_class": "subscription",
      "circuit": "closed",
      "regions": [],
      "models": [
        {
          "model_id": "x-ai/grok-3",
          "kind": "chat",
          "context_length": 128000,
          "native_tools": false,
          "native_reasoning": false,
          "aliases": ["grok-3"],
          "family": "grok-3",
          "variant": "web",
          "metrics": {
            "cost_per_1m_input_usd": null,
            "cost_per_1m_output_usd": null,
            "rated_quota_per_1m_input_usd": null,
            "latency_p50_ms": 2500.0,
            "latency_p95_ms": 6800.0,
            "ttft_p50_ms": null,
            "ttft_p95_ms": null,
            "success_rate": 1.0,
            "samples": 154,
            "probe_success_rate": 1.0,
            "probe_samples": 12
          }
        }
      ]
    },
    {
      "id": "abc123openrouter",
      "name": "OpenRouter-Devin-Personal",
      "type": "openrouter",
      "priority": 6,
      "cost_class": "per_call",
      "circuit": "closed",
      "regions": ["us"],
      "models": [
        {
          "model_id": "x-ai/grok-3",
          "kind": "chat",
          "context_length": 128000,
          "native_tools": true,
          "native_reasoning": false,
          "aliases": [],
          "family": "grok-3",
          "variant": "openrouter",
          "metrics": {
            "cost_per_1m_input_usd": 3.0,
            "cost_per_1m_output_usd": 15.0,
            "rated_quota_per_1m_input_usd": null,
            "latency_p50_ms": 1200.0,
            "latency_p95_ms": 2400.0,
            "ttft_p50_ms": null,
            "ttft_p95_ms": null,
            "success_rate": 0.99,
            "samples": 12,
            "probe_success_rate": null,
            "probe_samples": 0
          }
        }
      ]
    }
  ]
}
```

Both models share `family: "grok-3"`. Caller can:

- Pick `variant: "web"` to use the operator's $0/req subscription
  with rate-limit risk.
- Pick `variant: "openrouter"` for the paid marketplace route with
  smoother latency (1.2s vs 2.5s p50).
- Pick by family + cheapest cost (yields web).
- Pick by family + best success_rate × samples (yields web because
  more samples).

---

**Implementation pointers** (for proxy collaborators or curious
clients):

- Canonical helpers: `app/routing/canonical.py`
  (`matches_capability`, `derive_family`, `collect_canonical_aliases`).
- Catalog endpoint: `app/api/models.py`.
- LMRHv2 snapshot: `app/routing/lmrh/snapshot.py` (`_ModelSnap`
  dataclass + `_build_snapshot`).
- LMRHv2 endpoints: `app/api/lmrh_v2.py` (`_render_provider`,
  `well_known_config`).
- SDK reference: `sdk/python/lmrh_client.py`.
- Tests: `tests/unit/test_v341_v350_canonical_aliases.py`.

---

End of RFC.
