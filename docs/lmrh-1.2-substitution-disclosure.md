# LMRH 1.2 — Substitution Disclosure Standard (Draft)

**Status**: Draft for cross-vendor review.
**Authors**: D. Blagbrough (llm-proxy-manager).
**Companion to**: `docs/draft-blagbrough-lmrh-00.md` (LMRH 1.1).
**Reference implementation**: llm-proxy-manager v3.0.36+ (cross-family fallback) and v3.0.46+ (capability-empty fallback).

---

## Problem

Aggregating LLM proxies routinely substitute the caller's requested model with a different one — for cost, capacity, family-availability, or compliance reasons. Today this is done in inconsistent, vendor-specific ways:

- **OpenRouter** silently routes through fallback providers when the primary fails; no standard disclosure header.
- **Together / Helicone-style routers** redirect models to internal aliases without telling the caller.
- **Anthropic / OpenAI direct APIs** do not substitute — they 4xx if the model is unavailable.
- **llm-proxy-manager** (this implementation) emits `LLM-Capability` with `chosen-because=cross-family-fallback, requested-model=X, served-model=Y` (v3.0.36+).

A caller building against multiple proxy backends has to write per-vendor substitution detectors. DevinGPT v2.74.5 ships such a detector for our proxy specifically. Substitution-aware applications need a standard so they can write *one* detector that works across providers.

LMRH 1.2 standardizes the disclosure shape via the existing `LLM-Capability` response header, leveraging the structured-fields format already defined in LMRH 1.0/1.1.

## Concepts

### Substitution

A substitution occurs when the response to a request was served by a model whose identifier differs from the value the caller specified in `body.model`. This includes (but is not limited to):

- **Family substitution**: caller asked for `claude-sonnet-4-6`, server served by an OpenAI-compatible provider with `gpt-4o`.
- **Variant substitution**: caller asked for `gpt-4o`, server served by `gpt-5.5` because the upstream subscription tier doesn't enumerate `gpt-4o`.
- **Version-pin substitution**: caller asked for `claude-sonnet-4-6`, server served by `claude-sonnet-4-6-20250514` (date-versioned variant). This is generally NOT considered a substitution worth disclosing — the version-pinning is implicit in the contract.
- **Capacity / fallback substitution**: caller asked for `model-X` with priority-1 routing, primary provider was unavailable, served by a fallback provider with the same model identifier. NOT considered a substitution (model identifier matched).

### Disclosure semantics

Substitution disclosure is **mandatory** when the model identifier in the response body's `model` field (or upstream's chunk `model` field on streaming responses) differs from the caller's requested `body.model`, EXCEPT in the version-pin case described above.

Servers MUST NOT silently substitute. If substitution is performed, the response MUST include the disclosure header defined below.

## Headers

### `LLM-Capability` extension

Servers that support LMRH 1.2 substitution disclosure include the following keys in `LLM-Capability` (which is already defined as a Structured Fields Dictionary in LMRH 1.1):

| Key                  | Value type | Required when substituting | Description |
|----------------------|------------|----------------------------|-------------|
| `chosen-because`     | Token      | always                     | One of: `score`, `hard-constraint`, `fallback`, `cheapest`, `p2c`, **`cross-family-fallback`** (NEW), **`capability-substitute`** (NEW) |
| `requested-model`    | Token      | always when substituting   | The bare model identifier the caller sent in `body.model` |
| `served-model`       | Token      | always when substituting   | The bare model identifier of the model that actually served the request, with vendor prefix stripped (e.g. `gpt-5.5`, not `openai/gpt-5.5`) |
| `unmet`              | Inner-list | always when substituting   | Names of LMRH dimensions that could not be satisfied by the chosen candidate. Always includes `model` for substitution events. |

#### `chosen-because` token additions

LMRH 1.0 defines: `score`, `hard-constraint`, `fallback`, `cheapest`, `p2c`.

LMRH 1.2 adds:

- `cross-family-fallback` — the requested model belongs to one provider family (e.g. `claude-*`, `gpt-*`, `gemini-*`) and no candidate in that family was available; the proxy served the request from a different family with model substitution.
- `capability-substitute` — the requested model belongs to a family for which a provider was available, but that provider does not list the specific model in its scanned capabilities; the proxy served using the provider's default model (or a same-family equivalent).

Both indicate substitution. `cross-family-fallback` is a stronger signal (different family) than `capability-substitute` (same family, different specific model).

### Example: substitution

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Devin-Codex-Gmail, model=gpt-5.5,
  task=chat, safety=3, latency=medium, cost=standard, region=us,
  chosen-because=cross-family-fallback, unmet=(model),
  requested-model=gpt-4o, served-model=gpt-5.5
Content-Type: application/json

{
  "model": "gpt-5.5",
  "choices": [{"message": {"content": "..."}}],
  ...
}
```

### Example: no substitution

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Devin-Anthropic-Max-VG, model=claude-sonnet-4-6,
  task=reasoning,analysis,code,chat, safety=4, latency=medium, cost=standard,
  region=us, chosen-because=score
Content-Type: application/json

{
  "model": "claude-sonnet-4-6",
  ...
}
```

`requested-model` and `served-model` MUST NOT be emitted when no substitution occurred.

## Caller behavior

### Detection

Callers detect substitution by checking either:

1. **Response body** — does `body.model` (or chunk `chunk.model` on streaming) match the request's `body.model`? (Strict; works without LMRH support.)
2. **Response header** — is `LLM-Capability.chosen-because` one of `cross-family-fallback`, `capability-substitute`?

The body-level check is the canonical signal. The header is additive disclosure for callers that want to differentiate substitution *types* (family-level vs capability-level) or read out the original requested-model after the body is consumed.

### Opt-out

Callers that cannot tolerate substitution opt out via LMRH 1.1's existing `;require` modifier on a `provider-hint=` dim. This is unchanged from LMRH 1.1:

```
LLM-Hint: provider-hint=anthropic-direct,anthropic-oauth,claude-oauth;require
```

When a `;require` constraint is present and no candidate satisfies it, servers MUST return HTTP 503 instead of substituting (per LMRH 1.0 §6).

### Failure on substitution

Some applications (especially those using LLM output for compliance-sensitive workflows) may want to fail-fast on any substitution rather than silently accept. This is not a server obligation — it's a caller-side choice. Recommended pattern:

```python
# Pseudo-code, applies regardless of SDK
def call_with_strict_model(model, ...):
    resp = client.chat.completions.create(model=model, ..., extra_headers={
        "LLM-Hint": "..."  # without ;require — substitution permitted unless we detect it
    })
    served = resp.model  # body-level
    if served != model and not _is_version_pin_variant(model, served):
        raise LLMUnavailableError(f"requested {model!r}, served {served!r}; substitution not accepted")
    return resp
```

A single line of caller code converts permissive routing into strict routing. Reference implementation: AI Analyzer team's v3.9.20 (2026-05-02) ships exactly this guard. DevinGPT v2.74.5's substitution detector reads `chunk.model` for the same reason.

## Server obligations summary

A server claiming LMRH 1.2 substitution-disclosure compliance MUST:

1. Emit `LLM-Capability` on every successful response (already required by LMRH 1.0).
2. When the served model differs from the caller's `body.model` (excepting version-pin variants), include `chosen-because=cross-family-fallback` OR `chosen-because=capability-substitute`, with `requested-model=` and `served-model=` populated.
3. Set the response body's `model` field to the actual served model — not the caller's request.
4. Honor `;require` opt-out by returning 503 instead of substituting.
5. NOT silently substitute — i.e. if substitution occurs, the disclosure header is mandatory.

A server claiming compliance SHOULD:

1. Surface the substitution in any metrics/activity-log event for caller-side audit (e.g. our `event_meta.requested_model` + `event_meta.served_model`).
2. Document which substitution algorithms it employs (priority-based, capability-fallback, family-fallback, etc.) so callers can predict behavior.

## Cross-vendor adoption

The proposal here is grounded in shipping behavior of llm-proxy-manager v3.0.36 onwards. Convergence with other proxies:

- **OpenRouter / Together / Helicone** — would need to add the disclosure when their fallback or pinning logic substitutes. The `chosen-because` taxonomy is open to adding vendor-specific tokens (e.g. `chosen-because=capacity-fallback` for capacity-driven routing).
- **Anthropic API direct** — N/A, no substitution today. Compliance is trivial (always emit `chosen-because=score` or omit the header).
- **OpenAI API direct** — N/A. Same as above.
- **Vendor-managed proxy (Bedrock, Vertex, etc.)** — already emit some response-side metadata; aligning on the header schema would reduce client-side fragmentation.

We propose a 90-day comment window before LMRH 1.2 finalization. Implementations may opt in to draft semantics today; the header schema is forward-compatible (callers ignore unknown keys per LMRH 1.0 §3).

## References

- LMRH 1.0/1.1 spec: `docs/draft-blagbrough-lmrh-00.md`
- Reference implementation: `app/routing/router.py:select_provider` (cross-family logic), `app/routing/lmrh/headers.py:build_capability_header` (header construction)
- Caller-side detector: AI Analyzer team's v3.9.20 substitution guard, DevinGPT v2.74.5 chunk.model detector
- RFC 8941 — Structured Fields for HTTP

## Acknowledgments

The two reference caller implementations (AI Analyzer v3.9.20 and DevinGPT v2.74.5) shipped within hours of the v3.0.36 fallback feature. Their independent implementations using the same disclosure header validate that the schema is straightforward to consume. Both teams are credited as draft reviewers when 1.2 finalizes.
