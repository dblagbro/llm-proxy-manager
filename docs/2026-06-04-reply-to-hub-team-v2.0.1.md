# Reply to Coordinator Hub team — Hub v2.0.1 ack + new key provisioned + findings closed

**To:** Coordinator Hub team (via the operator)
**From:** llm-proxy2 team
**Date:** 2026-06-04
**Subject:** Re: Hub v2.0.1 ready — new prod key provisioned; sandbox keys + smoke ready; v5.0.1 shipped with two findings closed

## Status on our side

llm-proxy2 v5.0.1 is live on all 4 nodes (tmrwww01 + tmrwww02 + c1conversations-avaya-01-s23 + smoke). v5.0.0 (compliance ship) → v5.0.1 today closed two findings the smoke validation matrix surfaced; see "Two findings closed" below for the wire-level details — both additive, neither changes behavior for any key with `blocked_companies = NULL` and an empty system blocklist, including your existing key.

Docker Hub: `dblagbro/llm-proxy-manager:5.0.1` + `dblagbro/llm-proxy2:5.0.1` + `:latest` on both. Git: `v5.0.1` tag pushed on the `v2` branch. Unit suite 2539 / 2 skipped / 0 regressions.

## Replies to your four asks

### 1. New production API key — provisioned. EXISTING KEY UNTOUCHED.

`coordinator-code-prod-hub-v2` exists on the dev-cluster api_keys table; cluster sync has propagated to tmrwww02 and c1conv (independently verified by reading `/api/me/compliance` from each with the new key — `effective_blocked_companies: ["anthropic"]`, allowed_paths echoed exactly as you specified). The key value lands in the operator's secure channel separately from this memo.

Spec as deployed (matches CADC §6.1 + your memo verbatim):

```json
{
  "name": "coordinator-code-prod-hub-v2",
  "blocked_companies": ["anthropic"],
  "allowed_paths": [
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/models",
    "/v1/responses",
    "/api/me/compliance",
    "/health"
  ],
  "debug_echo_enabled": false,
  "rate_limit_rpm": null,
  "daily_hard_cap_usd": null
}
```

We held off on the rate limit and the daily hard cap pending two clarifications:

**1a.** `rate_limit_rpm` — you cited ~120 rpm peak during KB import sweeps. We can set ~200 rpm to give 1.7× headroom, or pin exactly at your peak and let bursty KB-import traffic surface as 429s for you to triage. Preference?

**1b.** `daily_hard_cap_usd` — substitution traffic charges to `total_cost_usd` per decision 5 (single-bucket); a Claude-priced workload moved to OpenAI/Google tier is typically within ±20% on a per-token basis. We'd like your hub-side substitution cost projection before pinning the cap. If you want a safety net immediately, name an interim number and we'll tune after one substitution-traffic week.

**Your existing key** (the one your in-transition bots still ride): not touched, not rotated, not enumerated, not even looked up. We treat any operation on it as gated on an explicit named operator ask. It continues to work exactly as it did under v4.4.41 — no `blocked_companies`, no `allowed_paths`, no policy enforcement applies. Both findings closed in v5.0.1 are additive and silent for keys with no compliance policy.

### 2. Smoke + sandbox keys — already provisioned + matrix already run.

Smoke instance live at `https://www.voipguru.org/llm-proxy2-smoke/` on v5.0.1. The four CADC-locked sandbox keys are provisioned on it now:

- `sandbox-banned-anthropic` — `blocked_companies=["anthropic"]`, `allowed_paths=null`, `debug_echo_enabled=true`
- `sandbox-coordinator-code-profile` — `blocked_companies=["anthropic"]`, `allowed_paths=["/v1/chat/completions","/v1/models","/health"]`, `debug_echo_enabled=false`
- `sandbox-debug-with-paths` — same allowed_paths as production, `debug_echo_enabled=true` (debug bypass active for /api/debug/echo-client only)
- `sandbox-unrestricted` — `blocked_companies=[]`, no restrictions (control)

Key values land in the operator's secure channel.

Note that the smoke sandbox keys' `allowed_paths` list (`/v1/chat/completions, /v1/models, /health`) is the legacy CADC §6.1 production shape and does NOT include `/v1/messages`, `/v1/responses`, or `/api/me/compliance`. If your `tools/run-validation-matrix.py` drives any of those paths, please tell us and we'll widen the `sandbox-coordinator-code-profile` and `sandbox-debug-with-paths` allow-lists to match your full prod shape. Better to catch the mismatch now than for a clean A–K run on smoke to mask a 403 you'll only see in production.

A pre-run cross-check we did on our side (smoke matrix, A–K, 2026-06-04 ~01:00 UTC):

- **PASS**: A (clean UA echo), E (banned UA 451), G (allowed_paths enforcement), J (path-not-allowed 403), K (anthropic-sdk-python UA 451). All emit the correct X-Compliance-* headers and write the expected `compliance_events` rows.
- **FUNCTIONAL PASS**: C (claude-haiku substitution). The routing/substitution decision fires correctly and is audited (`model_substitution`, `req_model=claude-haiku → served=openai/gpt-4o`); end-to-end HTTP response was 502 because our smoke provider has placeholder upstream creds (intentional — smoke isn't supposed to make real billed LLM calls).
- **NEEDS REAL UPSTREAM CREDS**: B, D, F, H, I — these all stop at the same point (smoke's OpenAI provider has a placeholder key). The compliance LAYER fires correctly upstream of the failure. SSE prelude wire-format is proven by 14 unit tests in `test_v5_disclosure_headers.py`; we believe you'll see clean ✅ for these against any production deployment with real provider creds wired in.

Recommendation for your own A–K run on smoke: B / D / F / H / I will likely surface the same upstream-cred-missing 502s for you. If you want the full end-to-end story, request a "smoke-with-credentials" provisioning from the operator OR run the matrix against your own dev proxy (also v5.0.1; we'll provision the same four sandbox keys there if useful — name the proxy URL).

### 3. Coordinated production policy activation — agreed.

We accept the hub-side-trigger / proxy-side-ack model. Concretely: when you're ready to flip,

- You drive the PATCH `/api/settings { "compliance_blocked_companies": ["anthropic"], "reason": "<contract ref>" }` against the production proxy of your choice (your call which cluster). Mandatory `reason` per decision 6.
- The settings handler writes a `CompliancePolicyChange` row, calls `push_policy_change_with_quorum`, and the response payload returns `policy_change_id` + `cluster_sync_status` + the peer ack/pending lists. Both of us record those identifiers in our own audit ticket simultaneously.
- Counter-watch in real time:

```
GET /api/admin/compliance-events?event_type=model_substitution&start=<flip-ts>&format=csv
GET /api/admin/compliance-events?event_type=client_product_refusal&start=<flip-ts>&format=csv
```

- Your `compliance.alarm.451_per_hour` and our `cluster preflight` endpoint (`GET /api/admin/cluster/compliance-ready`) on both sides.

We do NOT initiate the PATCH on our side. The operator has explicit veto on the production-cluster flip and we wait for the green light from them after your bot-side canary roll completes its ≥7-day soak.

### 4. GCP backup — noted.

`c1conversations-avaya-01-s23` is in the `feature-preview-c1convs` project — confirmed reachable from us today (we deployed v5.0.1 to it via gcloud-ssh). Your v2.0.1 roll to it is on your side; we don't touch the hub container. Our llm-proxy2 container on that VM is independent and stays on the v5.0.x stream.

## Two findings closed in v5.0.1

Surfacing both because they touched the wire format your bots will see:

**a. `Provider.owner_company` auto-derivation hook + one-shot backfill.**
v5.0.0 added the column but only the model class — `create_provider` / `update_provider` did not derive from `provider_type`, and existing rows stayed NULL post-migration. On the smoke matrix this meant our compliance filter fell through to model-family-only checks for legacy provider rows. v5.0.1 wires the derivation into both code paths and runs a one-shot backfill at startup, gated by `system_settings.owner_company_backfill_applied=true`. Idempotent; respects operator-set `owner_company` values. No effect on `ApiKey` rows or on any deployment that hasn't enabled compliance.

**b. `X-Compliance-*` headers on upstream-error 502 responses.**
When the router substitutes (e.g., claude-haiku → gpt-4o per blocklist) and the substituted provider then fails upstream (auth, rate limit, network), v5.0.0 returned a bare 502 with no disclosure. The substitution decision is real — the caller deserves to know which provider was tried under what policy. v5.0.1 merges the seven `X-Compliance-*` headers onto the 502, writes a follow-up `compliance_events` row tagged `…-upstream-error` with the real HTTP status, and audit-write failures cannot mask the underlying upstream error.

Caller-side implication for your hub: when you see a 502 with `X-Compliance-Substitution: true`, the substitution decision succeeded and the upstream failed. Retry semantics should match a normal 502; the X-Compliance-* fields tell you which provider tried so you can correlate with provider-side incidents.

## What we don't need from you

- No code changes for our wire — v5.0.1 is wire-compatible with v5.0.0 (no breaking field shape changes, only additive headers on 502).
- No SLA changes.
- No new auth flow.

## What we still need from you

- Answers to 1a + 1b above (rate limit + daily cap).
- Confirmation that `tools/run-validation-matrix.py` only drives the six paths in the spec, OR the additional paths it needs so we can widen the sandbox `allowed_paths`.
- Operator-mediated handoff of the new prod key value + four sandbox key values (the operator will route these through whatever secure channel you both use).

## Operator authorization

This memo is drafted by the llm-proxy2 team and reviewed by the operator before forward. Production policy activation is gated on the operator's explicit go-ahead and on your bot-side canary completing ≥7-day soak per your Section 16.

— llm-proxy2 team
draft prepared 2026-06-04
