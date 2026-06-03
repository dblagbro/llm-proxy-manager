# Response — Corrections accepted; v5.0.0 implementation lock + final asks confirmed

**To:** Coordinator Hub team
**From:** llm-proxy2 team
**Date:** 2026-06-03 (late evening)
**Re:** Acceptance of your corrections; confirmations to all 12 final asks; v5.0.0 implementation lock; sandbox readiness
**Status:** Architecture closed. Implementation begins on confirmation of this memo.

Thanks for the detailed corrections. All approved and folded into the spec. **One naming update first**, then point-by-point answers to your 12 final asks, then the test matrix update.

---

## 0. Version naming — actually v5.0.0, not v4.4.42

Heads-up: the magnitude of this work (33 architecture decisions; new sub-package; new tables; new middleware; cross-cutting changes to dispatch, cache, memory, cluster sync) warranted a major version bump. **The proxy ship is v5.0.0** (not v4.4.42). The Coordinator Hub team's matching version is **2.x**. Adjust your runbooks / changelogs accordingly. Everything else in your reply maps cleanly — only the version number on the wire changes.

---

## 1. Confirmations to your 12 final asks

| # | Your ask | Answer |
|---|---|---|
| 1 | SSE body prelude is opt-in, not default-on. | ✅ Confirmed. Default = headers-only. SSE prelude requires explicit `Accept-Compliance-Events: true`. |
| 2 | `Accept-Compliance-Events: true` is the ONLY way to request the stream prelude. | ✅ Confirmed. No other path emits the prelude. |
| 3 | `coordinator-local` means self-hosted/private, not Ollama only. | ✅ Confirmed. Eligibility: provider_type in `{ollama, vllm, llamacpp, lmstudio, localai}` OR `compatible` with `extra_config.self_hosted=true` OR `owner_company` in `{internal, local, self-hosted}`. NO fallback to hosted if no eligible self-hosted provider exists → HTTP 503 `no-compliant-local-provider`. |
| 4 | `allowed_paths` matching is exact against normalized path. | ✅ Confirmed. Exact-string match against the normalized path (nginx/base-prefix stripped, trailing slash normalized, double-slash collapsed). No substring; no accidental prefix. Globs deferred to v5.1 with explicit `*` syntax. |
| 5 | `debug_echo_enabled` behavior relative to `allowed_paths`. | ✅ Locked: **debug bypasses allowed_paths**. When `debug_echo_enabled=True`, requests to `/api/debug/echo-client` skip the `allowed_paths` check. Reasoning: dual-gate (debug_echo_enabled flag + sandbox-only access) provides defense in depth without forcing every sandbox key to include the debug endpoint in its allowed_paths list. Production keys won't have `debug_echo_enabled=True` so this is sandbox-only. |
| 6 | `X-Coordinator-*` values are sanitized/capped before audit storage. | ✅ Confirmed. Sanitization: header names lower-cased; values capped at 512 bytes; CR/LF/control chars stripped; never auth-trusted; never bypass UA detection; no duplicate-value logging. |
| 7 | Anthropic SDK UA patterns 451 for banned keys. | ✅ Confirmed. The narrow patterns (`anthropic-sdk-python/`, `anthropic-sdk-typescript/`, etc.) DO fire 451 for banned keys. We acknowledge this expands your migration scope — any Hub component calling the proxy via the Anthropic SDK must migrate to a neutral HTTP client (or the coordinator-agent-runner per your §1 plan). The taxonomy doc will list all narrow patterns explicitly so your QA can verify. |
| 8 | `/v1/models` for CLI-scoped keys returns only `coordinator-*` aliases. | ✅ Locked. **Decision Y, locked 2026-06-03:** when a key has `allowed_paths is not None AND debug_echo_enabled is False`, `/v1/models` returns ONLY the 4 logical aliases (coordinator-code, coordinator-fast, coordinator-reasoning, coordinator-local). Other keys (admin, debug, unrestricted) see the full model list post-block-filter. This enforces the alias abstraction at the wire — the CLI can't accidentally request a vendor-specific model. |
| 9 | Smoke refreshed to v5.0.0 before candidate CLI testing. | ✅ Confirmed. Build sequence: deploy to 3-node dev cluster → refresh smoke from same image → provision sandbox keys/providers/aliases → notify your team. Smoke is the first env you can validate against post-deploy. |
| 10 | Taxonomy doc available before production cutover. | ✅ Confirmed. Will ship as `docs/compliance-taxonomy-v5.0.0.md` in the v5.0.0 release. Full UA pattern list for all 10 built-in companies (Anthropic, OpenAI, Google, xAI, Cohere, Meta, Mistral, AWS, Microsoft, Amazon). You can request narrowing of any specific pattern before production cutover. |
| 11 | `cluster_sync_status: quorum-reached-1-pending` is a successful policy activation result. | ✅ Confirmed. Response includes `policy_change_id`, `audit_id`, `applied_to_peers`, `pending_peers` (with `peer`, `reason`, `will_sync_by`), `effective_at`. Runbook polls `/api/admin/cluster/compliance-ready` for convergence. If not converged by `will_sync_by`, your alarm path fires. |
| 12 | A, B, C, D scope additions, with corrections. | ✅ All four folded in with your corrections. SSE default is now headers-only (not prelude-on); `coordinator-local` semantics widened; `allowed_paths` is exact-match; debug bypasses allowed_paths; X-Coordinator-* sanitization in place. |

---

## 2. Coordinator Code identity locked

Per your §5:

- `X-Coordinator-Client: coordinator-code` (stable internal name; survives a CLI swap from OpenCode to whatever else later)
- `X-Coordinator-Profile: opencode-proxy-locked` (current implementation profile)
- `X-Coordinator-Client-Version: 2026.06.03` (date-versioned)
- `X-Coordinator-Upstream-CLI: opencode/<observed-version>` (optional; populated once you've selected the final CLI)

We'll surface all four in the admin UI per-request identity panel.

---

## 3. Production CLI allowed_paths locked

For the production Coordinator Code key on the hub team's deployment:

```json
"allowed_paths": [
  "/v1/chat/completions",
  "/v1/models",
  "/health"
]
```

No `/v1/embeddings` until you confirm OpenCode requires it. No debug endpoint on production keys.

For sandbox testing on smoke, you'll have separate keys (per §6 below) with intentionally varied `allowed_paths` so you can exercise Test J's enforcement and the bypass-on-debug-echo-enabled path.

---

## 4. Test matrix update

Adding your H, I, J, K to the validation matrix:

| Test | Path | Expected v5.0.0 behavior on smoke |
|---|---|---|
| A — exact UA | `POST /api/debug/echo-client` | Returns UA, matched_client_product (null for OpenCode if its UA is clean), would_451=false |
| B — normal non-streaming | `POST /v1/chat/completions {model: "coordinator-code", stream: false}` | 200; alias-resolved provider; no substitution headers; usable output |
| C — banned model non-streaming | `POST /v1/chat/completions {model: "claude-haiku", stream: false}` | 200; X-Compliance-Substitution=true; X-Compliance-Served-Model=<non-Anthropic>; compliance_events row event_type=model_substitution |
| D — banned model streaming | `POST /v1/chat/completions {model: "claude-haiku", stream: true}` (with `Accept-Compliance-Events: true`) | 200; SSE first frame is compliance_substitution; X-Compliance-* headers; usable stream |
| E — banned UA | Any request with `User-Agent: claude-cli/2.1.88` | HTTP 451; full error body; compliance_events row event_type=client_product_refusal; no upstream call attempted |
| F — self-hosted route | `POST /v1/chat/completions {model: "coordinator-local"}` | 200 if eligible self-hosted provider configured on smoke; HARD self-hosted filter; 503 `no-compliant-local-provider` otherwise; no fallback to hosted |
| G — external egress lockdown | CLI under network monitoring | Only the proxy URL is contacted; `allowed_paths` enforces 403 if the CLI hits anything other than the configured list |
| **H** | Streaming request, NO `Accept-Compliance-Events` header | 200; no `compliance_substitution` body prelude; first body frame is protocol-standard; X-Compliance-* headers present; compliance_events row present (default behavior — opt-out) |
| **I** | Streaming request, `Accept-Compliance-Events: true` | 200; `compliance_substitution` prelude emitted; X-Compliance-* headers present; compliance_events row present (explicit opt-in) |
| **J** | Allowed-paths-restricted key calls `/v1/test-egress-blocked` (decoy endpoint) | HTTP 403; `X-Compliance-Reason: path-not-in-allowed_paths`; compliance_events row event_type=path_not_allowed; no provider call |
| **K** | `User-Agent: anthropic-sdk-python/0.40.0` from banned key | HTTP 451; matched_company=anthropic; matched_pattern=`prefix:anthropic-sdk-python/`; compliance_events row event_type=client_product_refusal; no provider call (validates SDK-path migration scope) |

For Test J: we'll provision a decoy endpoint `/v1/test-egress-blocked` on smoke that the CLI-scoped key cannot reach (returns 403 with the compliance reason header) so you can verify the enforcement is firing without leaving the proxy URL space.

---

## 5. Anthropic SDK path migration — acknowledged scope expansion

Your §6 is a real expansion of Hub migration scope. The narrow Anthropic UA patterns mean:

- Any Hub-side code calling the proxy via `import anthropic` (the Anthropic Python SDK) WILL emit `anthropic-sdk-python/...` as part of the UA → 451 on banned keys.
- Same for `@anthropic-ai/sdk` (TypeScript), `anthropic-go`, `anthropic-java`, `anthropic-ruby`, `anthropic-rust`.
- Direct `requests`-based code is fine (neutral client identity).

Your preferred state matches our enforcement model exactly:

> **Bot runtime:** coordinator-agent-runner with direct HTTP / neutral internal client. No Claude CLI, no Anthropic SDK UA.
> **Human CLI:** Coordinator Code profile (OpenCode + locked config + proxy), OpenAI-compatible /v1/chat/completions, no direct vendor SDK identity.

We'll keep the SDK patterns narrow and visible in `docs/compliance-taxonomy-v5.0.0.md`. If a specific SDK version's UA shape changes and the existing pattern would false-positive (or miss the new shape), we can refine the pattern before cutover.

---

## 6. Sandbox preparation on smoke

Once v5.0.0 ships to the 3-node dev cluster (end of 2026-06-04), we refresh smoke and provision:

**Smoke URL:** `https://www.voipguru.org/llm-proxy2-smoke/`

**Sandbox API keys (delivered out-of-band):**

| Key purpose | blocked_companies | allowed_paths | debug_echo_enabled |
|---|---|---|---|
| `sandbox-banned-anthropic` | `["anthropic"]` | `null` (unrestricted) | `true` |
| `sandbox-coordinator-code-profile` | `["anthropic"]` | `["/v1/chat/completions", "/v1/models", "/health"]` | `false` |
| `sandbox-debug-with-paths` | `["anthropic"]` | `["/v1/chat/completions", "/v1/models", "/health"]` | `true` (validates the bypass on debug-echo) |
| `sandbox-unrestricted` | `[]` | `null` | `false` (control comparison) |

**Pre-configured providers:** synthetic test providers for each candidate model class — Anthropic (control-only), Gemini, OpenAI, Llama via ollama (matches coordinator-local eligibility), OpenRouter (relay-path testing).

**Decoy endpoints:** `/v1/test-egress-blocked` returning 403 for Test J.

**Aliases pre-published:** the 4 logical aliases per §10 of the spec.

**Debug logging:** verbose. Activity log retention extended to 90 days on smoke.

We'll ping when smoke is live + tokens are delivered.

---

## 7. Phasing reaffirmed

| Phase | Trigger | What's enabled |
|---|---|---|
| Phase 1 (now → cutover-N days) | Manual | Smoke validation; CLI testing (A-K matrix); Hub runner migration; Anthropic SDK removal from any Hub component calling the proxy |
| Phase 2 (cutover-N → cutover-1) | Hub team ready | Production canary with banned key on your prod proxy; audit review |
| Phase 3 (cutover) | All systems migrated | Full enforcement: model/provider substitution + cache/memory filtering + UA 451 (including Anthropic SDK paths) |
| Phase 4 (post-cutover) | Stable | compliance_events monitoring; taxonomy tuning; v5.1.0 scanner |

No post-deadline grace path. No emergency Claude CLI fallback. Confirmed.

---

## 8. Spec + impact map artifacts

Two docs ship with v5.0.0:

- **`docs/5.0-compliance-design.md`** — full architecture spec; lockable contract
- **`docs/5.0-impact-map.md`** — per-file change list with line-number anchors

Both available for your review once the implementation lands. Happy to share drafts now if useful.

---

## 9. Timeline reaffirmation

- **v5.0.0 ship to 3-node dev cluster:** end of 2026-06-04 (24-48h from architecture close, which is essentially now)
- **Smoke refresh to v5.0.0 + sandbox keys/providers/aliases/decoy:** within ~4h of dev-cluster deploy
- **Your validation window:** smoke-live through cutover-2-days
- **Production cutover (your prod proxy cluster):** at your discretion within the 10-day window
- **v5.1.0 (single-LLM scanner + full configurable aliasing):** week of 2026-06-09
- **v5.2.0 (3-judge consensus mechanism for scanner):** week of 2026-06-16

We're around through end-of-day 2026-06-04 to keep v5.0.0 moving. Notify us with sandbox-ready signal and we'll deliver tokens out-of-band.

— llm-proxy2 team (Claude opus 4.7 + the operator)
