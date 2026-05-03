# LMRH 1.2 — `cache` Mode Dim (Draft)

**Status**: Draft for cross-vendor review.
**Authors**: D. Blagbrough (llm-proxy-manager).
**Companion to**: `docs/draft-blagbrough-lmrh-00.md` (LMRH 1.1), `docs/lmrh-1.2-substitution-disclosure.md` (LMRH 1.2 §E1).
**Reference implementation**: llm-proxy-manager v3.0.42+ (`app/api/_cache_inject.py`).

---

## Problem

Prompt caching cuts upstream input-token cost by ~90% for stable prefixes that exceed the provider's cache threshold (~1024 tokens for Anthropic Sonnet, ~2048 for Haiku, ~4096 for Opus). Anthropic exposes it via `cache_control: {type: "ephemeral"}` blocks; OpenAI auto-caches with no caller knob; Google/Vertex auto-caches implicitly.

The 24h activity-log audit on llm-proxy-manager v3.0.39 (2026-05-01) found cache_control adoption at exactly **0%** across 16,000+ events — including 3,005 Anthropic Pro Max OAuth calls with 50–80k-token contexts. Coordinator-hub bot daemons send the same large system prompt repeatedly; their callers don't know about caching, so the savings sit unrealized.

llm-proxy-manager v3.0.42 ships **auto-cache injection** for Anthropic-shape providers — proxy wraps the last system block and large tool definitions in `cache_control: {type: ephemeral}` opportunistically when the caller didn't. Estimated savings on current volume: ~$1500/day, verified at 24M cache-read tokens/hr in production.

But auto-injection is a behavior change with caller-visible side effects (cache headers, slightly different cost reporting). Some callers may want to opt out (compliance — they want every call to hit fresh inference; debugging — they need deterministic non-cached behavior; cost attribution — caching changes who-pays-what).

LMRH 1.2 adds a `cache` dim so callers can express their cache intent in one cross-provider shape that proxies translate per-vendor.

## Concepts

### Cache modes

A request's cache mode is one of:

| Token         | Caller intent                                                    |
|---------------|------------------------------------------------------------------|
| `auto`        | Default. Proxy may inject cache_control if it looks beneficial.  |
| `ephemeral`   | Caller explicitly requests cache_control: {type: ephemeral}.     |
| `persistent`  | Reserved. Anthropic does not yet ship a persistent cache mode.   |
| `none`        | Caller forbids proxy-side cache injection.                       |
| `off`         | Synonym for `none`.                                              |
| `disabled`    | Synonym for `none`.                                              |

`auto` and absence of the dim are equivalent — both let the proxy decide.

### Per-provider translation

| Provider family    | `cache=auto`                                            | `cache=ephemeral`                                | `cache=none`                                       |
|--------------------|---------------------------------------------------------|--------------------------------------------------|----------------------------------------------------|
| Anthropic-shape    | Inject `cache_control: {type: ephemeral}` opportunistically (≥4000 char prefix; reference impl in `_cache_inject.py`). | Force `cache_control: {type: ephemeral}` on system + last tool, even below the heuristic threshold. | Strip any `cache_control` blocks the proxy would have added. (Caller-supplied blocks pass through untouched per `;require` semantics, see "Caller obligations".) |
| OpenAI (chat)      | No-op (OpenAI auto-caches; caller has no knob).         | No-op.                                           | No-op (caller cannot disable OpenAI's auto-cache). |
| Google / Vertex    | No-op (Vertex implicit cache).                          | No-op.                                           | No-op.                                             |
| Cohere             | No-op (no caller-controllable cache).                   | No-op.                                           | No-op.                                             |

When a vendor doesn't expose a caller-side cache control, the proxy SHOULD emit `LLM-Capability` with `cache=ignored` (see §Disclosure) so caller knows the dim was honored as a no-op rather than misinterpreted.

### Substitution interaction

When LMRH 1.2 §E1 substitution disclosure is active and the proxy substitutes the requested model with one served by a different family, the cache mode applies to the **served** family. A `cache=ephemeral` request that gets substituted from Anthropic to OpenAI is a no-op on the OpenAI side — but the proxy MUST still disclose the no-op via `LLM-Capability.cache=ignored` so the caller can audit the outcome.

## Headers

### `LLM-Hint` request dim

Added to the LMRH-1.0 dim taxonomy:

```
LLM-Hint: ..., cache=auto
LLM-Hint: ..., cache=ephemeral
LLM-Hint: ..., cache=none
LLM-Hint: ..., cache=ephemeral;require
```

`;require` makes the cache mode a hard constraint:

- `cache=ephemeral;require` — caller insists on cache injection; if the served provider doesn't honor `cache_control`, return HTTP 503.
- `cache=none;require` — caller insists no proxy-side injection happens; if the proxy would have injected, suppress and emit `cache=ignored` disclosure. (Hard-failing on a non-issue would be punitive — caller already got what they asked for.)

### `LLM-Capability` response

Servers that support LMRH 1.2 cache-mode include the following in `LLM-Capability`:

| Key              | Value type | Required when                                | Description                                                                          |
|------------------|------------|----------------------------------------------|--------------------------------------------------------------------------------------|
| `cache`          | Token      | always when caller sent `cache=` dim         | One of `auto`, `ephemeral`, `none`, `ignored` — what the proxy actually applied.    |
| `cache-injected` | Boolean    | when proxy injected cache_control            | `?1` if proxy added cache_control blocks the caller didn't supply; absent otherwise. |
| `cache-tokens-read` | Integer | when upstream reports cache_read_input_tokens | Echoed from upstream usage data — caller can validate hit rate.                      |
| `cache-tokens-written` | Integer | when upstream reports cache_creation_input_tokens | Echoed from upstream usage data.                                                  |

#### Example: caller asked for ephemeral, got it on Anthropic

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Devin-Anthropic-Max-VG, model=claude-sonnet-4-6,
  task=chat, safety=4, latency=medium, cost=standard, region=us,
  chosen-because=score, cache=ephemeral, cache-injected=?1,
  cache-tokens-read=24500, cache-tokens-written=512
```

#### Example: caller asked for ephemeral, request was substituted to OpenAI

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Devin-Codex-Gmail, model=gpt-5.5,
  task=chat, safety=3, latency=medium, cost=standard, region=us,
  chosen-because=cross-family-fallback, requested-model=claude-sonnet-4-6,
  served-model=gpt-5.5, unmet=(model), cache=ignored
```

#### Example: caller opted out, proxy didn't inject

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Devin-Anthropic-Max-VG, model=claude-sonnet-4-6,
  task=chat, safety=4, latency=medium, cost=standard, region=us,
  chosen-because=score, cache=none
```

`cache-injected` is omitted when no injection happened (whether because the caller already supplied `cache_control`, the prefix was below threshold, or the caller opted out).

## Caller obligations

### Caller-supplied cache_control wins

If the request body already carries `cache_control` blocks the caller wrote themselves, the proxy MUST NOT override them — even with `cache=none`. The dim is about *proxy* injection behavior, not about scrubbing caller-controlled inputs. A caller who explicitly wrote `cache_control` and then sent `cache=none` is internally inconsistent; the body wins.

### Below-threshold no-op

Anthropic's caching threshold (~1024–4096 tokens depending on model) means small prompts skip cache silently. `cache=ephemeral;require` on a 200-token prompt is satisfied by virtue of `cache_control` being attached, even though Anthropic will return `cache_creation_input_tokens=0`. Servers SHOULD NOT 503 in this case — the dim was honored.

### Substituted-call expectations

When a `cache=ephemeral` request is substituted to a non-Anthropic family, expect `cache=ignored` rather than `cache=ephemeral` in the response header. Substitution-aware applications already check `chosen-because` per §E1; reading `cache=` is the same caller-side audit pattern.

## Server obligations

A server claiming LMRH 1.2 cache-mode compliance MUST:

1. Parse the `cache=` dim from `LLM-Hint`. Unknown values fall back to `auto` and emit `X-LMRH-Warnings: unknown-dim-value:cache=<value>`.
2. Honor the mode per the per-provider table above.
3. Emit `LLM-Capability.cache=` whenever the dim was sent — even if the result is `ignored`.
4. Emit `cache-injected` when injection happened (so callers can audit unintended caching).
5. Echo upstream `cache_creation_input_tokens` / `cache_read_input_tokens` as `cache-tokens-written` / `cache-tokens-read` when available.

A server SHOULD:

1. Use a sensible heuristic for the `auto` threshold — reference impl uses 4000 chars (~1000 tokens) and only injects when no caller-supplied `cache_control` is present.
2. Wrap the LAST stable block (system prompt, last tool definition) rather than every block — Anthropic charges per cache breakpoint, and one well-placed wrap captures the full prefix.
3. Surface `cache_class` or similar field in cost-attribution/activity logs so cache-savings can be attributed back to the LMRH dim that drove them.

## Cross-vendor adoption

- **OpenRouter / Together / Helicone** — when serving Anthropic-shape providers, adopt the same dim shape. For non-Anthropic providers, emit `cache=ignored` consistently.
- **Anthropic API direct** — caller-controlled today. Compliance is just "when caller sends `cache=ephemeral` via LMRH and we forward to Anthropic with `cache_control` blocks, emit `cache=ephemeral, cache-injected=?1`." Trivial.
- **Vertex / Bedrock managed proxies** — emit `cache=ignored` (Vertex) or `cache=ephemeral` (Bedrock with explicit cache config).
- **Cohere** — emit `cache=ignored`.

The dim is forward-compatible with future tokens (`persistent`, `semantic`, etc.) per LMRH 1.0 §3 (callers ignore unknown values).

## Estimated impact

Reference implementation observation (llm-proxy-manager production, 2026-05-02 rolling 24h):

- 3,005 Anthropic Pro Max OAuth calls with 50–80k-token contexts
- ~50% cache-hit rate after auto-injection landed (depends on caller-side prompt stability)
- ~$200/day savings on this proxy alone
- Network-wide, if other aggregators adopted the same auto-injection + dim, estimated $5–10K/day in unrealized prompt-cache savings would be captured

## References

- LMRH 1.0/1.1 spec: `docs/draft-blagbrough-lmrh-00.md`
- LMRH 1.2 §E1 substitution disclosure: `docs/lmrh-1.2-substitution-disclosure.md`
- Reference implementation: `app/api/_cache_inject.py` (auto-inject), `app/routing/lmrh/score.py` (dim parsing)
- Anthropic prompt caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- RFC 8941 — Structured Fields for HTTP

## Acknowledgments

The auto-cache feature was driven by the v3.0.39 24h audit observing 0% cache_control adoption across the proxy. Callers — coordinator-hub bot daemons, paperless-ai-analyzer, AI Tax Analyzer — got the cost reduction with no client-side change. The `cache` dim formalizes that opt-out path for callers who prefer to control caching explicitly.
