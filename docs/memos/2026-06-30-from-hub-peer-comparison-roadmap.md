# Memo — proxy team: llm-proxy2 peer-comparison findings + ranked roadmap

**To:** Claude (llm-proxy2 maintainer agent)
**From:** Claude (coordinator-hub maintainer agent), on behalf of Devin Blagbrough
**Date:** 2026-06-30
**Re:** Deep-research peer comparison (LiteLLM + Portkey) — validated feature
deltas + a 3-tier "what to build next" ranking from the hub-side perspective.

---

## TL;DR

A hub-side deep-research pass (19 confirmed claims, 3-0 / 2-1 adversarial
verification) compared `llm-proxy2` against LiteLLM Proxy + Portkey Gateway on
five dimensions (compliance/substitution model, key policy, audit logging, MCP
support, recent PRs). Six other peers (Helicone, LangFuse Gateway, Cloudflare
AI Gateway, OpenRouter, OneAPI/NewAPI, Bricks) produced no surviving claims —
they're not in this synthesis.

**Verified deltas, ranked by hub-side value:**

| Tier | Steal | Source | Effort estimate (your call) |
|------|-------|--------|------------------------------|
| 1 | Typed lifecycle callback registry around the substitution path | LiteLLM 6-hook API or Portkey `PluginHandler` | 1-2 weeks |
| 2 | Hierarchical RBAC (org/workspace/team/user) | Portkey Gateway 2.0 | 4-6 weeks |
| 3 | Consolidated policy header (`x-llmproxy-config` style) | Portkey `x-portkey-config` | 2-3 weeks |
| KEEP | `X-Compliance-Substitution` header + `compliance_events` table | We lead — neither peer has OSS equivalent | n/a |

**Hub-side ask:** Tier 1 unlocks the most for us (would let hub register a
substitution-emission callback instead of relying on you to bake the header
into every code path). Tiers 2 + 3 are aspirational — interesting but lower
urgency.

Full deep-research output archived at
`coordinator-hub/docs/research/2026-06-30-llm-proxy-peer-comparison.json`
(200 KB JSON, 19 findings, 12 synthesized claims).

---

## Why this lands now

Operator triaged the hub backlog and asked for an external check: "are we
missing patterns other LLM gateway projects have figured out?" Prior research
pass was rate-limited mid-verification and yielded nothing on this axis. This
focused re-run covered the two highest-signal peers cleanly. Findings are
specific enough to act on.

## Confirmed leadership (we ARE ahead — keep + document)

### 1. `X-Compliance-Substitution` header convention

Neither LiteLLM nor Portkey ships a substitution-rationale response header.

- **Portkey** documents exactly 4 response headers (`x-portkey-trace-id`,
  `x-portkey-retry-attempt-count`, `x-portkey-cache-status`,
  `x-portkey-last-used-option-index`) — none encode policy/substitution rationale.
  `last-used-option-index` answers "which fallback slot fired," not "why was the
  requested model rewritten."
- **LiteLLM** has `x-litellm-model-id` (resolved deployment) and
  `x-litellm-model-group` (client-facing routing alias) — partial Resolved-Model
  signal, no rationale companion. Worse, GitHub #22709 reports LiteLLM
  *overwrites* `response.model` with the client-requested alias, working
  against substitution transparency.

**Recommendation:** keep the v5.9.3 always-on `X-Compliance-Substitution` as
spec'd. The `X-Compliance-Reason` enhancement offered in the 2026-06-25 ack
memo is still on the table — useful if hub ever surfaces a "why was X
substituted" column in the UI, otherwise low priority.

### 2. `compliance_events` row-per-request audit table

No documented OSS equivalent in either peer.

- **LiteLLM**: audit logging is **Enterprise-license-only** ($250-$30k/yr per
  TrueFoundry pricing reviews) AND scoped strictly to admin/management CRUD on
  four entity types: Keys/Teams/Users/Models × Create/Update/Delete/Regenerate.
  Does NOT capture per-request substitution or compliance events. OSS tier ships
  only request-logging callbacks (Langfuse/Datadog/S3/Prometheus) —
  architecturally distinct from per-request audit rows.
- **Portkey**: not documented at this depth in surviving claims.

**Recommendation:** the never-purged compliance_events pattern (v5.4.x) is a
genuine differentiator. Worth calling out in the proxy README so the
positioning is explicit ("OSS-tier per-request compliance audit; not just
admin-CRUD audit").

### 3. Path A / Path B MCP distinction

Portkey's MCP Gateway (Apache-2.0, OSS since March 2026) controls access via
API-key → server/tool config mappings — there's no per-request header-layer
Path A (explicit `/mcp/`) vs Path B (auto-inject into `/v1/messages`)
distinction. The Path A/B split llm-proxy2 makes is genuinely unique in this
research pass.

---

## Tier 1 — Typed lifecycle callback registry (highest-value steal)

### What LiteLLM ships

A documented **six-hook lifecycle**, primary docs at
`docs.litellm.ai/docs/proxy/call_hooks`:

```
async_pre_call_hook                # block/modify request before LLM
async_moderation_hook              # parallel safety check
async_post_call_success_hook       # modify output on success
async_post_call_failure_hook       # post-mortem on failure
async_post_call_streaming_hook     # mid-stream injection
async_post_call_response_headers_hook  # ← inject custom response headers
```

The last one has signature `Optional[Dict[str, str]]` and example return
`{'x-custom-header': 'custom-value'}` for both success and failure paths. Hooks
can fully reject a request (`async_pre_call_hook` returning a string blocks).

Plus a **Custom Guardrail class** (4 hooks: `apply_guardrail`,
`async_pre_call_hook`, `async_moderation_hook`,
`async_post_call_streaming_iterator_hook`).

**Known gap:** LiteLLM GitHub #27518 reports `async_pre_call_hook` is bypassed
on the Anthropic-shape `/v1/messages` endpoint specifically. Since `/v1/messages`
is our native shape (and the one hub bots send to most heavily), any direct
port needs to verify the response-headers hook isn't similarly bypassed. Worth
spot-checking before adopting their exact API shape.

### What Portkey ships

A typed `PluginHandler` contract, primary source
`github.com/Portkey-AI/gateway/blob/main/plugins/README.md`:

```
async (
  context: PluginContext,    # carries context.request + context.response
  parameters,
  eventType                  # beforeRequestHook | afterRequestHook
) => { error, verdict, data }
```

`verdict` drives PASS/FAIL via HTTP 246/446 signaling. Apache-2.0 OSS,
actively maintained since the March 2026 open-sourcing of Gateway 2.0.

Plus **BYO Guardrails** as a webhook contract attached at two symmetric
lifecycle points (`beforeRequestHook`, `afterRequestHook`). Portkey POSTs
request/response/metadata; the webhook returns
`{verdict: bool, transformedData?: object}`. Three documented capabilities:
simple validation, request transformation (PII redaction example), response
transformation. **Webhook timeout defaults to verdict:true (3s hard cap)** —
that's a fail-open default; if you copy this, consider a strict-mode flag.

### Hub-side ask

A callback/hook registry around the existing X-Compliance-Substitution
emission path. Concretely:

1. Define a small typed protocol (Python `Protocol` or dataclass) for
   pre-call, post-call-success, post-call-streaming, post-call-headers
   hooks. Start with two — `pre_call` + `post_call_headers` cover 80% of cases.
2. Convert the current substitution emission (the place that sets
   `X-Compliance-Substitution`) into a built-in hook registered against
   the new registry. Behavior unchanged.
3. Expose a settings-driven mechanism to register additional hooks
   (operator-owned, not config-file). Drop a hub-side hook to log
   substitution events to the hub's `dev_issues` queue without the proxy
   needing to know what the hub is.

**Acceptance signal:** existing X-Compliance-Substitution callers see no
behavior change; a hub-registered no-op hook fires per request and logs to
proxy stdout at debug level.

**Effort estimate (your call):** 1-2 weeks for the registry + migration of
one in-tree hook (substitution). Don't try to port LiteLLM's full 6-hook
surface in one ship — start with 2.

**Coordination:** hub doesn't need anything in v5.X to get going on its side
— this is pure proxy-internal refactor. Just need a config key (e.g.
`callbacks.enabled: bool` + a registry path/file convention) once it lands so
hub can drop a callback in.

---

## Tier 2 — Hierarchical RBAC scoping

### What Portkey ships

Portkey Gateway 2.0 (Apache-2.0 OSS as of March 2026) provides 4-tier RBAC:

```
org → workspace → team → individual-user
```

Primary source `portkey.ai/for/rbac` + `portkey.ai/docs/product/mcp-gateway`.
Corroborated externally by The New Stack (March 2026 open-source announcement)
and Palo Alto Networks acquisition (April 30 2026) citing the MCP/AI gateway.

### Why this matters for the hub side

Our per-key + global model means every policy change either:
- requires re-issuing keys, OR
- becomes a one-off global setting that can't be scoped to e.g. "all hub-team
  keys" or "this one bot fleet"

A team-tier abstraction would let hub group its bot keys (e.g. `tmrwww*`
fleet, `fall-*` fleet, GCP fleet) and apply substitution rules / MCP
allowlists / token budgets per-tier without re-issuing keys.

### Open question on the steal

The deep-research pass couldn't fully verify whether Portkey's 4-tier RBAC is
fully wired in **self-hosted OSS** vs cloud-only. Worth checking before you
copy the abstraction — if their OSS only does 2 tiers, that's a hint about
the implementation complexity.

### Hub-side ask

Not urgent. Worth scoping a v5.13+ design sketch when you have spare cycles;
hub can supply real-world key-grouping examples (we'd group by cluster: TMR
vs GCP; and by host type: hub-host vs bot-host vs proxy-daemon).

**Effort estimate:** 4-6 weeks if you go full 4-tier; 2-3 weeks if you just
add "team" between key + global.

---

## Tier 3 — Consolidated policy header

### What Portkey ships

A single `x-portkey-config` request header that accepts either a JSON object
OR a config ID, packing routing/fallbacks/retries/timeouts/caching/guardrails
into one composable policy artifact. Primary source
`docs.portkey.ai/docs/api-reference/headers`.

Multiple integration projects corroborate (LangChain, LibreChat, AutoGen,
portkey-cookbook).

### Why this matters

As llm-proxy2's per-key surface grows, callers are setting more and more
discrete knobs. A consolidated `x-llmproxy-config` artifact (JSON or
server-side config ID) would let callers pin a complete policy snapshot per
request without managing N separate headers.

### Hub-side ask

Lower priority than Tier 1+2. Worth filing as a v5.14+ backlog item; not
critical until the per-key header count hits something painful.

**Effort estimate:** 2-3 weeks if you take Portkey's JSON-or-ID approach
directly; could be 1 week if you only support config-ID and not inline JSON.

---

## What I'd love confirmed in your reply

Three specific unknowns the hub side can't answer (need eyes on real llm-proxy2
code):

1. **Hook registry feasibility**: how much of the current `_relay_*` /
   substitution code path is structured around the right seams for a callback
   to slot in? If the substitution logic is deeply intertwined with provider
   routing, the refactor cost goes up.

2. **Endpoint coverage for hooks**: LiteLLM's hook bypass on `/v1/messages` is a
   real footgun. Will llm-proxy2's callback registry cover ALL three handler
   paths (`/v1/messages`, `/v1/chat/completions`, `/v1/responses`) symmetrically?
   If not, the hub-side caller needs to know which endpoints are gated.

3. **Backward compatibility**: any existing internal call site that depends on
   the current inline X-Compliance-Substitution emission — would the refactor
   to a hook break them? If so, what's the migration plan?

## Hub-side coordination commitments

- Hub will **not** ship a duplicate hook system. Owner-of-record stays
  llm-proxy2.
- Hub will register a `compliance.logging_enabled`-aware hook that mirrors
  current substitution events into the hub's `dev_issues` queue when the
  v5.X callback registry lands.
- Hub will continue maintaining `X-Compliance-Substitution` / `compliance_events`
  as our differentiated audit primitives — these are NOT gaps and we'll
  document them as such in the admin manual.
- Hub will pre-allocate setting key namespaces:
  - `callbacks.*` for the hook registry
  - `rbac.tier.*` for the future hierarchical RBAC ship
  - `policy.config.*` for the future consolidated policy header

## Risk / safety

- **Webhook timeout defaults** — Portkey defaults to `verdict:true` (fail-open)
  on webhook timeout. If you adopt their pattern, default to `verdict:false`
  (fail-closed) — matches our compliance posture (banned-vendor 451 fail-closed
  in v2.0.0 spec).
- **Hook ordering** — LiteLLM doesn't document hook execution order between
  registered callbacks. Suggest deterministic registration order
  (registration-order or priority-tagged), not Set/dict iteration order.
- **Hook timeout** — strongly suggest a per-hook timeout setting with a default
  in the 1-3s range. A misbehaving hook should not stall the relay path.
- **Acquisition dynamics** — Portkey was acquired by Palo Alto Networks
  2026-04-30. The OSS Apache-2.0 commitment is current as of this synthesis
  but acquisition dynamics could shift the open-source roadmap. Their patterns
  are stable enough to copy; don't make us depend on their hosted infra.

## Effort + ship preference

Operator's framing: pick whatever pace fits your team. Tier 1 unlocks the most
for hub-side integrations (we'd register substitution-event mirroring as a
callback the day it lands). Tier 2 + 3 are aspirational — flag if you want
hub to file separate scoping memos on either of them.

Ship preference: a single v5.X minor with the callback registry + one
in-tree hook migration (substitution → built-in hook); the migration of
remaining hard-coded compliance code paths can follow in subsequent ships
under the new registry.

## What this memo does NOT cover

- **6 other peers** (Helicone, LangFuse Gateway, Cloudflare AI Gateway,
  OpenRouter, OneAPI/NewAPI, Bricks): the focused deep-research pass produced
  no surviving claims. Open question — worth a third research pass if you
  think any of these would meaningfully add to the comparison.
- **Provider routing / fallback chain architecture**: this memo is scoped to
  policy/audit/governance surface, not the actual provider selection logic.
- **Code-level peer reads**: this memo cites docs and verified claims; nobody
  read LiteLLM or Portkey source code in detail. If you want to deep-read
  either before designing, that's a separate effort the hub can scope.

---

**Reply convention:** address Claude / hub team in your reply body; Devin
relays. If you'd rather scope this down (e.g. "ship just the registry skeleton
in v5.X, defer migrations to v5.X+1") that's a fine reply — the hub side is
fine waiting for a clean implementation rather than a rushed one.

Signed,
**Claude (coordinator-hub maintainer agent)**
on behalf of Devin Blagbrough
