# LLM Model Routing Hint (LMRH) Protocol
## Knowledge Transfer Document — Session: 2026-04-07

This document captures the full design rationale, RFC draft, and implementation intent
for the LMRH protocol developed across a 3-hour design session. A new Claude session
can load this file to continue the work without loss of context.

---

## 1. Origin and Motivation

**Project:** `llm-proxy` — a Node.js reverse proxy (Express + SQLite) that sits in front
of multiple LLM providers (Anthropic, OpenAI, Google Gemini, Grok, Ollama, etc.) and
provides multi-provider failover, key management, and a web UI.

**Problem:** The proxy routes requests to providers based on availability and priority
order only. It has no mechanism for a caller to express *what kind of task* the request
is, and no mechanism to select models based on task requirements. Examples of routing
intelligence that was missing:

- Claude Code CLI always calls the proxy — it should get a reasoning-capable model
- A quick chatbot query should get a fast/cheap "turbo" model, not a slow reasoning model
- A safety-critical query should be blocked from providers with low content safety ratings
- A latency-sensitive request should prefer providers in a specific geographic region

**Design constraint the user stated:** This needs to be future-proof. IPv4 was once
"enough" — we should not design an 8-bit integer flag field that runs out like IPv4 did.

---

## 2. Design Decisions Made

### 2.1 Why Named Parameters, Not Integer Flags

Early sketch used a `0–7` integer scale. User rejected this:
> "If 0 through 7 is enough, is that future proof? Can we better design this so that we
> can add whatever may be needed in the future? Thinking back to how IPv4 was great but
> IPv6 came anyway."

**Adopted approach:** RFC 8941 Structured Field Values — key=value parameter pairs in
an HTTP header. Extensible without versioning. Unknown keys are ignored (soft preference)
unless marked `;require`.

### 2.2 Safety as an Affinity

User asked: "The Pentagon is saying some providers can't be used because of their refusal
to do certain things — can we include flags for 'don't send to providers with X safety
levels'?"

**Adopted approach:** Safety is modeled as a bidirectional range: `safety-min` and
`safety-max`. This lets the caller express both:
- "This task requires a safe provider" (safety-min=3)
- "This task requires a provider that won't refuse edgy content" (safety-max=1)

Scale: 0 (uncensored) to 5 (maximum refusal rate). Provider capability declaration
uses `safety=<value>` in `LLM-Capability` response header.

### 2.3 Six Named Affinity Dimensions (with examples)

| Affinity | Type | Example |
|---|---|---|
| `task` | enum | `task=reasoning`, `task=coding`, `task=summarize`, `task=chat` |
| `latency` | enum | `latency=low`, `latency=medium`, `latency=high` |
| `cost` | enum | `cost=economy`, `cost=standard`, `cost=premium` |
| `safety-min` | 0–5 | `safety-min=3` (require safety ≥ 3) |
| `safety-max` | 0–5 | `safety-max=1` (require safety ≤ 1, i.e. permissive) |
| `region` | string | `region=us`, `region=eu` (data residency) |
| `context-length` | integer | `context-length=128000` (minimum tokens) |
| `modality` | enum | `modality=vision`, `modality=audio`, `modality=text` |

### 2.4 Hard Constraints via `;require`

Any parameter can be marked hard (routing failure if unmet) vs soft (best-effort):

```
LLM-Hint: task=coding; latency=low; safety-min=3;require
```

Here `safety-min=3` is hard; `task` and `latency` are soft preferences.

### 2.5 Backward Compatibility (critical design point)

User asked: "This needs to be able to work with all previous API calls for LLM inference
where it would just be ignored if those LLM inference URLs received this extra data."

**Answer:** Fully backward compatible. Three reasons:
1. RFC 9110 §6.3 requires all HTTP servers to silently ignore unrecognized headers
2. Section 9.1 of the RFC draft: proxy MUST strip `LLM-Hint` before forwarding to providers
3. Clients that don't send the header get normal routing — no minimum version required

The protocol lives entirely inside the proxy. Backend providers never see it. Legacy
clients get default behavior.

---

## 3. Full RFC Draft: draft-blagbrough-lmrh-00

```
Internet-Draft                                           D. Blagbrough
Intended Status: Proposed Standard                        April 2026
Expires: October 2026

       LLM Model Routing Hint (LMRH) Protocol
       draft-blagbrough-lmrh-00

Abstract

   This document defines the LLM Model Routing Hint (LMRH) protocol,
   an HTTP header extension that enables API clients to express routing
   preferences for Large Language Model (LLM) inference requests. The
   protocol operates transparently within LLM API gateways and proxies,
   providing intelligent model selection based on task requirements,
   latency constraints, cost preferences, safety requirements, regional
   compliance, and capability needs — without modifying the LLM inference
   API contract or requiring changes to backend model providers.

Status of This Memo

   This Internet-Draft is submitted in full conformance with the
   provisions of BCP 78 and BCP 79.

   Internet-Drafts are working documents of the IETF. Note that other
   groups may also distribute working documents as Internet-Drafts.

   Internet-Drafts are draft documents valid for a maximum of six months.

1. Introduction

   The proliferation of Large Language Model providers and models has
   created a complex routing problem for applications consuming LLM
   inference APIs. Different models offer different tradeoffs across
   multiple dimensions: reasoning capability, response latency, cost per
   token, content safety policies, context window sizes, and supported
   modalities.

   Current practice requires application developers to hard-code model
   selection or implement proprietary routing logic. This approach does
   not scale as the model landscape evolves and creates tight coupling
   between application code and specific model identifiers.

   This document defines a lightweight HTTP header protocol that:
   a) Allows clients to declare routing preferences once, per-request
   b) Allows proxies to make intelligent routing decisions on behalf of clients
   c) Allows providers to advertise their capabilities in a standardized way
   d) Remains invisible to and fully compatible with existing LLM inference APIs
   e) Degrades gracefully when the protocol is not supported

1.1 Terminology

   The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT",
   "SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and
   "OPTIONAL" in this document are to be interpreted as described in
   BCP 14 [RFC2119][RFC8174].

   Client:   An application sending LLM inference API requests.
   Proxy:    An intermediary that implements LMRH and performs routing.
   Provider: A backend LLM inference API (e.g., Anthropic, OpenAI).
   Model:    A specific LLM accessible via a provider's API.
   Affinity: A named routing preference dimension (e.g., task, latency).

2. Protocol Overview

   LMRH operates as a two-header protocol:

   - LLM-Hint   (request):  Client → Proxy. Declares routing preferences.
   - LLM-Capability (response): Proxy → Client. Advertises routing capabilities.

   The proxy interprets LLM-Hint, selects an appropriate provider and model,
   forwards the request with LLM-Hint stripped, and returns LLM-Capability
   describing what was actually used.

3. Header Syntax

   Both headers use RFC 8941 Structured Field Values (Dictionaries).

3.1 LLM-Hint Header

   LLM-Hint = sf-dictionary

   Example:
     LLM-Hint: task=coding, latency=low, cost=economy

   With hard constraint:
     LLM-Hint: task=reasoning, safety-min=3;require, region=us

3.2 LLM-Capability Header

   LLM-Capability = sf-dictionary

   Example:
     LLM-Capability: provider=anthropic, model=claude-opus-4-6,
       task=reasoning, safety=4, latency=high, cost=premium,
       context-length=200000, region=us

4. Affinity Dimensions

4.1 task

   Declares the nature of the LLM task. Values (case-insensitive):

   - reasoning   Multi-step logical reasoning, chain-of-thought, math
   - coding      Code generation, debugging, refactoring
   - summarize   Document summarization, extraction
   - chat        Conversational turn-by-turn interaction
   - analysis    Data analysis, classification, structured output
   - creative    Creative writing, brainstorming, open-ended generation
   - vision      Image understanding (requires modality=vision)
   - audio       Speech/audio tasks (requires modality=audio)

   Proxies SHOULD prefer models explicitly benchmarked for the declared task.

4.2 latency

   Declares acceptable response latency. Values:

   - low      Time-to-first-token < 500ms preferred; streaming required
   - medium   Time-to-first-token < 2s; streaming preferred
   - high     Latency acceptable; throughput and quality prioritized

   Default: medium.

4.3 cost

   Declares cost tolerance tier. Values:

   - economy   Prefer lowest-cost models meeting other affinities
   - standard  Balance cost and quality (default)
   - premium   Prefer highest-quality models regardless of cost

4.4 safety-min and safety-max

   Declares content safety requirements as a range. The safety scale is:

   0 - Uncensored: No content filtering applied
   1 - Permissive: Minimal filtering; adult content may be allowed
   2 - Standard:   Default provider policy
   3 - Elevated:   Additional filtering for professional contexts
   4 - Strict:     Conservative filtering; suitable for enterprise
   5 - Maximum:    Strictest available; appropriate for minors or regulated use

   safety-min=N  Route only to providers with safety >= N
   safety-max=N  Route only to providers with safety <= N

   Both may be specified simultaneously to define a safety window.
   If both are specified, safety-max MUST be >= safety-min.

   Example (require permissive provider, but not completely uncensored):
     LLM-Hint: safety-min=1, safety-max=2

4.5 region

   Declares data residency or geographic routing preference. Values are
   ISO 3166-1 alpha-2 country codes or regional groupings:

   - us    United States
   - eu    European Union (GDPR compliance implied)
   - uk    United Kingdom
   - au    Australia
   - ca    Canada
   - jp    Japan
   - sg    Singapore
   - on-prem  Route only to on-premises or self-hosted models

   Proxies SHOULD treat region as a hard constraint when marked ;require.

4.6 context-length

   Minimum context window in tokens (integer). Proxy selects models with
   context >= this value.

   Example: context-length=100000

4.7 modality

   Declares required input modality beyond text. Values (may be a list):

   - text    Text input (always assumed)
   - vision  Image or video input required
   - audio   Audio input required
   - tool    Tool/function calling required

   Example: modality=vision

4.8 freshness

   Declares knowledge cutoff recency requirement. Values:

   - any       No requirement (default)
   - recent    Knowledge cutoff within the last 6 months
   - realtime  Requires live retrieval / web grounding capability

4.8 provider-hint

   Suggests (soft) or requires (hard) a specific provider. Values are
   provider identifiers as registered in the local proxy configuration.

   Example (soft suggestion): provider-hint=anthropic
   Example (hard constraint): provider-hint=on-prem;require

5. Hard Constraints

   Any affinity parameter MAY be marked as a hard constraint by appending
   the ;require parameter flag (RFC 8941 boolean parameter):

     LLM-Hint: task=coding, safety-min=3;require, region=eu;require

   A proxy receiving a request with one or more hard constraints MUST:

   a) Select only providers and models that satisfy ALL hard constraints
   b) If no provider satisfies all hard constraints, return HTTP 503 with
      body: {"error": "no_provider_satisfies_constraints", "failed": [...]}
   c) NOT fall back to a non-conforming provider for hard-constrained affinities

   Soft affinities (without ;require) are best-effort. If no provider
   perfectly satisfies a soft affinity, the proxy routes to the best
   available match and SHOULD note the mismatch in LLM-Capability.

6. Version Negotiation

   The LLM-Hint header SHOULD include a version parameter:

     LLM-Hint: v=1, task=coding, latency=low

   Proxies receiving an unknown version MUST:
   a) Attempt to parse known affinity keys from the header
   b) Ignore unknown keys (forward compatibility)
   c) Return the version they processed in LLM-Capability: v=<N>

   This ensures that a client sending v=2 headers to a v=1 proxy gets
   partial (but not zero) routing benefit.

7. LLM-Capability Response Header

   After routing, the proxy SHOULD return a LLM-Capability header
   describing the actual provider and model selected, plus their
   capability profile:

     LLM-Capability: v=1, provider=anthropic, model=claude-sonnet-4-6,
       task=reasoning, safety=4, latency=medium, cost=standard,
       context-length=200000, region=us

   If a soft affinity was unmet, the proxy SHOULD include:
     unmet=<affinity-name>

   Example with unmet latency preference:
     LLM-Capability: v=1, provider=anthropic, model=claude-opus-4-6,
       task=reasoning, safety=4, latency=high, cost=premium,
       context-length=200000, unmet=latency

8. Capability Registry

8.1 Provider Capability Profiles

   Each provider in the proxy configuration maintains a capability profile:

   {
     "provider": "anthropic",
     "models": {
       "claude-opus-4-6": {
         "task": ["reasoning", "coding", "analysis", "creative"],
         "latency": "high",
         "cost": "premium",
         "safety": 4,
         "context_length": 200000,
         "region": ["us", "eu"],
         "modality": ["text", "vision", "tool"]
       },
       "claude-haiku-4-5": {
         "task": ["chat", "summarize"],
         "latency": "low",
         "cost": "economy",
         "safety": 4,
         "context_length": 48000,
         "region": ["us", "eu"],
         "modality": ["text", "tool"]
       }
     }
   }

8.2 Model Capability Discovery

   Proxies MAY implement automated capability discovery via:
   a) Provider API model listing endpoints (already implemented in llm-proxy)
   b) Manual capability profile configuration (RECOMMENDED as baseline)
   c) Inference from model name patterns (fallback only)

9. Security Considerations

9.1 Header Stripping

   Proxies MUST strip LLM-Hint headers before forwarding requests to
   backend providers. Backend providers MUST NOT receive routing hints
   that could influence their behavior or expose client routing policy.

9.2 Constraint Injection

   Proxies MUST validate and sanitize LLM-Hint values. Clients MUST NOT
   be able to use LLM-Hint to access providers or models not authorized
   by their API key profile.

9.3 Safety Constraint Enforcement

   The safety-min;require constraint is a security-relevant hard constraint.
   Proxies MUST enforce it server-side and MUST NOT allow client-provided
   hints to override operator-configured safety floors.

   Operators MAY configure a global safety-min floor that applies regardless
   of client hints.

9.4 Privacy

   LLM-Hint headers may contain routing preferences that reveal information
   about an organization's compliance posture (e.g., region=eu;require).
   Proxies SHOULD log LLM-Hint values only at debug level and MUST NOT
   include them in externally visible logs or metrics.

10. Extension Points

   The following affinity names are reserved for future standardization:

   - persona        Instruction persona or system prompt template reference
   - audit          Audit trail or explainability level required
   - output-format  Required output format (json, markdown, structured)
   - compliance     Named compliance framework (hipaa, sox, pci-dss)
   - temperature    Preferred temperature/creativity level
   - language       Primary language of response required
   - cache-hint     Caching policy preference (no-cache, prefer-cache)

   Implementations MAY support these as experimental extensions using the
   x- prefix convention (e.g., x-persona=legal-analyst).

11. IANA Considerations

   This document requests registration of the following HTTP header fields:

   Header Field Name:  LLM-Hint
   Applicable Protocol: http
   Status: provisional
   Reference: this document

   Header Field Name:  LLM-Capability
   Applicable Protocol: http
   Status: provisional
   Reference: this document

12. References

   [RFC2119]  Bradner, S., "Key words for use in RFCs to Indicate
              Requirement Levels", BCP 14, RFC 2119, March 1997.

   [RFC8174]  Leiba, B., "Ambiguity of Uppercase vs Lowercase in RFC
              2119 Key Words", BCP 14, RFC 8174, May 2017.

   [RFC8941]  Nottingham, M. and P. Kamp, "Structured Field Values for
              HTTP", RFC 8941, February 2021.

   [RFC9110]  Fielding, R., et al., "HTTP Semantics", RFC 9110,
              June 2022.

Author's Address

   D. Blagbrough
   Email: (contact via llm-proxy-manager project)
```

---

## 4. Implementation Status — COMPLETE (as of 2026-04-08)

All four phases are fully implemented, tested, and deployed to production.

### Phase 1 — llm-proxy v1.8.0: LMRH Routing Engine ✅

**Shipped:** `src/server.js` — `parseLmrhHint()`, `LMRH_WEIGHTS`, `scoreModelAgainstHint()`, `rankProvidersWithHint()`, `buildLmrhCapabilityHeader()`

- `LLM-Hint` header parsed on every `/v1/messages` request (RFC 8941 key=value)
- `model_capabilities` SQLite table added (via `src/database.js`)
- Weighted scoring: task=10, safety=8, region=6, latency=4, cost=3, context=2, modality=5
- Hard constraints via `;require` → HTTP 503 on failure with `{"error":"no_provider_satisfies_constraints","failed":[...]}`
- `LLM-Capability` response header set on success
- `LLM-Hint` stripped before forwarding to backend (RFC 9110 §6.3 compliant)

### Phase 2 — llm-proxy v1.9.0: Capability Profiles UI ✅

**Shipped:** `src/database.js`, `src/server.js` (3 new API routes), `public/index.html`

- `inferCapabilitiesFromModelName()` — 15 pattern rules (claude-opus/sonnet/haiku, gpt-4o/mini, o1/o3-mini, gemini-2.5-pro/flash, grok, llama, mistral, deepseek, etc.)
- `POST /api/providers/:id/model-capabilities/infer` — bulk infer + save
- `GET /api/providers/:id/model-capabilities` — list profiles
- `PUT /api/providers/:id/model-capabilities/:modelId` — manual update
- Web UI: "Scan Models" auto-populates profiles; inline badge editor per model row
- `source=inferred` vs `source=manual` tracked in DB

### Phase 3 — coordinator-hub v1.0.47: Hub as LMRH Client ✅

**Shipped:** `app/web_ui.py`, `app/templates/settings.html`

- `_make_anthropic_client(api_key, lmrh_hint=None)` — attaches `LLM-Hint` via `default_headers` when proxy URL is configured
- `_LMRH_DEFAULT_HINTS` dict — 7 use-case defaults:
  - `bot_task`: `task=reasoning, safety-min=3, latency=medium`
  - `bot_quick`: `task=chat, latency=low, cost=economy`
  - `kb_analysis`: `task=analysis, context-length=100000`
  - `avui_interpret`: `task=chat, latency=low`
  - `avaya_task`: `task=reasoning, safety-min=4, latency=medium`
  - `kb_import`: `task=analysis, context-length=200000`
  - `dev_session`: `task=reasoning, latency=medium`
- `_get_lmrh_hint(use_case)` — checks DB override `lmrh.hint.<use_case>` first, falls back to default
- All 7 LLM call sites updated with appropriate use-case hints
- `POST /ui/api/lmrh-hints` route — saves per-use-case overrides to DB
- Settings page: "LLM Routing Hints (LMRH)" card with per-use-case text inputs and defaults shown as placeholders

### Phase 4 — llm-proxy v1.10.0: CoT Auto-Engage from task=reasoning ✅

**Shipped:** `src/server.js` — `getAugmentationMode()`, `dispatchProviderCall()`

- `getAugmentationMode(provider, clientKey, lmrhHint, modelCaps)` — new params
- When `lmrhHint.task === 'reasoning'` AND `modelCaps.native_reasoning === false` → returns `'cot-pipeline'`
- Applies to both streaming and non-streaming paths
- `cot-engaged=?1` added to `LLM-Capability` response header when CoT auto-engaged
- Log line: `LMRH CoT auto-engaged: task=reasoning on non-native-reasoning model <model> via <provider>`
- Does NOT require `claude-code` key type — any caller with a hint gets reasoning augmentation

### Bug Fixes During Implementation ✅

- **v1.9.1**: `monitor.js` probe used `gemini-2.0-flash` (discontinued) as default for Google hold-down retests → changed to `gemini-2.5-flash`
- **v1.9.1**: Google providers had `maxLatencyMs=1800ms` default; Gemini 2.5 with thinking takes 3–8s → set to `30000ms` in SQLite for all Google providers on all nodes

---

## 5. Production Deployment

| Component | Version | Nodes |
|-----------|---------|-------|
| `llm-proxy-manager` | 1.10.0 | www.voipguru.org, www2.voipguru.org |
| `coordinator-hub` | 1.0.48 | www.voipguru.org (primary) |

Docker Hub: `dblagbro/llm-proxy-manager:1.10.0`, `dblagbro/coordinator-hub:1.0.48`

---

## 6. Key Implementation Files

| File | Role |
|------|------|
| `llm-proxy/src/server.js` | LMRH parsing, scoring, routing, capability header, CoT auto-engage |
| `llm-proxy/src/database.js` | `model_capabilities` table, `inferCapabilitiesFromModelName()` |
| `llm-proxy/src/monitor.js` | Hold-down probe (uses `gemini-2.5-flash` default) |
| `coordinator-hub/app/web_ui.py` | `_make_anthropic_client()`, `_LMRH_DEFAULT_HINTS`, `_get_lmrh_hint()`, 7 call sites |
| `coordinator-hub/app/templates/settings.html` | LMRH Routing Hints card |

---

## 7. Reusable Patterns for Future Extensions

- **New affinity dimension**: add key to `LMRH_WEIGHTS` in `server.js`, add field to `MODEL_INFERENCE_RULES` in `database.js`, add scoring branch in `scoreModelAgainstHint()`
- **New hub use case**: add entry to `_LMRH_DEFAULT_HINTS`, call `_get_lmrh_hint('new_use_case')`, pass to `_make_anthropic_client()`
- **New model patterns**: add rule to `MODEL_INFERENCE_RULES` array in `database.js`
