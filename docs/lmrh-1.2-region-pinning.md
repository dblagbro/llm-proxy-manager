# LMRH 1.2 — Region Pinning Semantics (Draft)

**Status**: Draft for cross-vendor review.
**Authors**: D. Blagbrough (llm-proxy-manager).
**Companion to**: `docs/draft-blagbrough-lmrh-00.md` (LMRH 1.1), `docs/lmrh-1.2-substitution-disclosure.md` (LMRH 1.2 §E1), `docs/lmrh-1.2-cache-mode-dim.md` (LMRH 1.2 §E2).
**Reference implementation**: llm-proxy-manager v3.0.x (`region` dim wired through `app/routing/capability_inference.py`, `app/routing/lmrh/score.py`).

---

## Problem

LMRH 1.0 already defines a `region` dim. It's currently scored as advisory soft-constraint:

```python
# app/routing/lmrh/score.py:81
case "region":
    if not profile.regions or dim.value in profile.regions:
        score += WEIGHTS["region"]
```

— a candidate provider gets a score boost when its `regions` list matches, but a non-matching candidate is not eliminated. That's fine for cost-or-latency-driven hints but insufficient for compliance-driven workloads:

- **HIPAA**: protected health information must be processed in US-region infrastructure (US datacenters with BAA in place).
- **EU GDPR / data residency**: EU customer data must be processed in EU regions; US fallthrough is a compliance violation.
- **Sovereign clouds (DE, FR, JP)**: regulated industries require provider regions inside specific national borders.

Existing LMRH 1.1 `;require` modifier syntax already supports hard constraints on dims — but vendors don't agree on how to plumb region-pinning down to the upstream provider call. Anthropic doesn't expose region in its API at all. OpenAI exposes region via deployment ID. Vertex pins region per provider config (one provider record per region). Bedrock pins via endpoint URL.

LMRH 1.2 §E3 standardizes the **caller-side hint shape** and the **server-side enforcement contract** so callers can write one `region=eu;require` constraint that proxies translate per-vendor.

## Concepts

### Region taxonomy

A region value is one of:

| Value      | Meaning                                       |
|------------|-----------------------------------------------|
| `us`       | United States (any US datacenter)             |
| `us-east`  | US East coast (Virginia / Ohio etc.)          |
| `us-west`  | US West coast (Oregon / California etc.)      |
| `eu`       | European Union (any EU datacenter)            |
| `eu-west`  | EU West (Ireland / France / Belgium)          |
| `eu-central`| EU Central (Frankfurt / Zurich)              |
| `uk`       | United Kingdom                                |
| `ca`       | Canada                                        |
| `asia`     | Asia (any APAC datacenter)                    |
| `asia-east`| Tokyo / Seoul                                 |
| `asia-southeast` | Singapore / Sydney                      |
| `local`    | Caller-local infrastructure (self-hosted)     |
| `any`      | No region preference (LMRH 1.0 default)       |

Compound values: comma-separated list expresses "any-of" — `region=us,ca` matches US OR Canada. The canonical wire form is the RFC 8941 InnerList: `region=(us ca)`. Bare-comma `region=us,ca` requires the 8941 parser path (proxy-side `http_sfv` available); the LMRH 1.0 legacy split-on-comma parser will treat the second value as an unkeyed dim and drop it. Implementations SHOULD accept both shapes.

Granularity: `eu` matches any of `eu-west`, `eu-central`. The region taxonomy forms a hierarchy: `*` > `<continent>` > `<continent>-<area>`. Servers MUST honor hierarchy — a provider tagged `eu-west` matches a `region=eu` query.

### Soft vs hard

| Form                  | Behavior                                                                   |
|-----------------------|----------------------------------------------------------------------------|
| `region=us`           | LMRH 1.0 advisory — score boost only. Compliance not guaranteed.           |
| `region=us;require`   | Hard constraint. Server MUST NOT serve from a non-matching region.         |
| `region=us;sovereign` | NEW — region cannot be satisfied via cross-region fallback or provider chain that crosses borders. Stricter than `;require`; surface implementation. |

`;sovereign` is the new modifier. It exists because some providers (e.g. some Vertex deployments, AWS Bedrock) silently failover across regions for resilience. `;require` matches at routing time but the upstream provider may still re-route the call mid-stream. `;sovereign` requires the server to refuse providers it cannot guarantee won't cross borders.

In practice today, `;sovereign` is satisfiable only by:
- Self-hosted models in caller-controlled infrastructure.
- Vendor offerings with explicit single-region SLAs (Bedrock with provisioned throughput in one region, Vertex single-region endpoints with no failover).

A server that cannot guarantee sovereignty MUST return HTTP 503 to a `;sovereign` request rather than serve and cross fingers.

## Headers

### `LLM-Hint` request dim

Existing usage continues to work:
```
LLM-Hint: region=us
```

Hard constraint:
```
LLM-Hint: region=us;require
```

Sovereign:
```
LLM-Hint: region=eu-central;sovereign
```

Multiple acceptable regions:
```
LLM-Hint: region=us,ca;require
```

### `LLM-Capability` response

Existing LMRH 1.0 emits `region=...` in `LLM-Capability` reflecting the served provider's regions. LMRH 1.2 §E3 adds:

| Key                | Value type | Required when                      | Description                                                                          |
|--------------------|------------|------------------------------------|--------------------------------------------------------------------------------------|
| `served-region`    | Token      | always when caller sent `region=` dim | The actual region the upstream provider is serving from, more specific than `region` (e.g. `us-east-1`). |
| `region-honored`   | Token      | always when `;require` or `;sovereign` was used | One of `strict`, `loose`, `failed`. `strict` = pinned at provider config; `loose` = matched via hierarchy; `failed` = not honored (paired with HTTP 503). |
| `cross-border-risk`| Boolean    | when `;sovereign` was satisfied via best-effort | `?1` if the proxy could not guarantee no upstream failover. Caller decides whether to retry elsewhere. |

#### Example: hard pin satisfied by single-region provider

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Bedrock-EU-Central-1, model=claude-sonnet-4-6,
  task=chat, safety=4, latency=medium, cost=standard, region=eu-central,
  served-region=eu-central-1, region-honored=strict, chosen-because=score
```

#### Example: hard pin failed (no candidate in region)

```http
HTTP/1.1 503 Service Unavailable
LLM-Capability: v=1, region=eu, region-honored=failed, unmet=(region)
Content-Type: application/json

{
  "error": "no provider matches region=eu;require constraint"
}
```

#### Example: sovereign satisfied with caveat

```http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=Vertex-EU-West-4, model=gemini-2.5-flash,
  region=eu-west, served-region=eu-west-4, region-honored=loose,
  cross-border-risk=?1, chosen-because=score
```

The `cross-border-risk=?1` lets the caller decide whether `eu-west-4` is acceptable given Vertex's documented multi-region failover behavior.

## Caller obligations

### Read region-honored before trusting the result

A caller asking for compliance-grade region pinning MUST inspect `LLM-Capability.region-honored` (and `cross-border-risk` for `;sovereign`) — not just the HTTP status. A 200 response can still carry `region-honored=loose` if the caller used `;require` and the proxy matched via hierarchy; a paranoid caller with stricter requirements would reject and retry with `;sovereign`.

### Don't conflate region with vendor

`region=eu;require` does not mean "EU-based vendor" — it means "data processed in EU." OpenAI's `eu` deployment is satisfiable; so is Vertex with EU regions; so is a self-hosted model in an EU datacenter. The dim is about data location, not corporate jurisdiction.

## Server obligations

A server claiming LMRH 1.2 §E3 region-pinning compliance MUST:

1. Honor the region hierarchy when matching candidates (`eu-west` satisfies `region=eu`).
2. Treat `;require` as a hard filter — exclude non-matching candidates from selection. If the resulting candidate set is empty, return HTTP 503 with `LLM-Capability` carrying `region-honored=failed, unmet=(region)`.
3. For `;sovereign`, exclude providers known to cross-region failover. If the proxy cannot determine failover behavior with confidence, treat as non-sovereign and exclude.
4. Emit `served-region` with the most specific region available from the upstream provider response.
5. Emit `region-honored=strict` only when the served provider is configured with a single fixed region (no failover possible).

A server SHOULD:

1. Maintain a `region` field per provider config that defaults to the upstream's documented region(s).
2. Document which providers it considers `;sovereign`-eligible.
3. Surface region match/miss in activity-log events for caller-side audit.

## Cross-vendor adoption

- **OpenRouter / Together / Helicone** — already aggregate across vendor regions; honoring the hint becomes a routing-table filter. The `served-region` disclosure helps callers verify.
- **Anthropic API direct** — currently no region exposure. Compliance is "always emit `region-honored=loose, cross-border-risk=?1`" until Anthropic publishes regional endpoints.
- **OpenAI API direct** — exposes regional deployment IDs via deployment URLs. `served-region` derivable from the URL or `model` returned.
- **Vertex / Bedrock** — already region-pinned per endpoint; can emit `region-honored=strict` when running in a single-region deployment, `loose` otherwise.
- **Self-hosted (Ollama, vLLM, etc.)** — `region=local;sovereign` is the canonical case; the deployment is by definition in caller-controlled infrastructure.

## Estimated impact

Compliance-driven workloads (healthcare, finance, EU customer data) currently work around LMRH's lack of hard region semantics by:

- Picking a single region-locked provider URL and hard-coding it (no proxy benefit).
- Falling back to direct vendor APIs when proxies don't pin (loses cost/latency optimization).
- Manually splitting traffic across proxy and direct based on data classification.

LMRH §E3 lets these workloads use proxies as routers without losing the compliance guarantee. Reference implementation in llm-proxy-manager v3.0.x already enforced `;require` as a hard filter (line 81-87 of `score.py`); v3.0.51 extends this with hierarchy matching (`region=eu` satisfied by `eu-west`/`eu-central`) and any-of parsing (RFC 8941 InnerList values). `;sovereign` modifier and the `served-region`/`region-honored`/`cross-border-risk` disclosure headers remain spec-only at the time of this draft and require additional ref-impl work.

## References

- LMRH 1.0/1.1 spec: `docs/draft-blagbrough-lmrh-00.md`
- LMRH 1.2 §E1 substitution disclosure: `docs/lmrh-1.2-substitution-disclosure.md`
- LMRH 1.2 §E2 cache mode dim: `docs/lmrh-1.2-cache-mode-dim.md`
- Reference implementation files: `app/routing/capability_inference.py` (region defaults per provider type), `app/routing/lmrh/score.py:81-84` (current advisory scoring), `app/routing/lmrh/types.py` (regions field on capability profile)
- RFC 8941 — Structured Fields for HTTP

## Acknowledgments

The region dim is the oldest LMRH dim that remained advisory while every other dim grew into a real routing input. Compliance teams asking "can I trust the proxy to keep my data in-region" deserve a yes/no answer the wire format can express. §E3 is that answer.
