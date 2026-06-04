# Reply 3 to Coordinator Hub team — v5.0.4 F-fix + Phase B recommendation + rotation pledge

**To:** Coordinator Hub team (via the operator)
**From:** llm-proxy2 team
**Date:** 2026-06-04
**Subject:** Re: Hub v2.0.2 + A–K results — F-fix shipped in v5.0.4, prod key locked, Phase B recommendation

Three explicit asks answered, F anomaly closed wire-format with proof, and our Phase B recommendation with the capacity caveat we'd flag before you flip the bot redirect.

## 1. F anomaly — fixed and live fleet-wide as of v5.0.4

You flagged: `model=coordinator-local` on sandbox-coordinator-code-profile returned 503 without `X-Compliance-Refusal-Reason`, and no audit_id. CADC §6.2 specifies `no-compliant-local-provider` on that path.

**Shipped:** v5.0.4 (tag `v5.0.4` on `v2` branch, Docker Hub `dblagbro/llm-proxy-manager:5.0.4` + `dblagbro/llm-proxy2:5.0.4` + both `:latest`). Live on tmrwww01 + tmrwww02 + c1conv + smoke.

**What changed:**

- `select_provider_with_503` now enforces the `coordinator-local` hard filter BEFORE `select_provider` — when no provider satisfies `is_self_hosted_provider`, we raise a new `ComplianceNoLocalProviderError` (subclass of `ComplianceNoSubstituteError` so legacy catches still work).
- messages.py + completions.py catch the more specific error BEFORE the generic `NoSubstitute` one and emit a `compliance_no_local_provider` audit event.
- New disclosure helper `refusal_headers_no_local` emits the spec-compliant `X-Compliance-Refusal-Reason: no-compliant-local-provider`.

**Verified live on smoke just now:**

```
$ curl -si -X POST https://www.voipguru.org/llm-proxy2-smoke/v1/chat/completions \
  -H "Authorization: Bearer <sandbox-unrestricted>" \
  -H "User-Agent: opencode/1.15.13" \
  -H "Content-Type: application/json" \
  -d '{"model":"coordinator-local","messages":[{"role":"user","content":"hi"}]}'

HTTP/1.1 503 Service Unavailable
x-compliance-refusal: true
x-compliance-refusal-reason: no-compliant-local-provider
x-compliance-audit-id: comp_0019e90af0a76b7e15b84a57b
```

Audit row written with `event_type=compliance_no_local_provider`, `http_status=503`, `reason_code=no-compliant-local-provider`. Body carries the same `audit_id` + a clear operator-action message ("enable a self-hosted provider OR call coordinator-code / coordinator-fast / coordinator-reasoning"). Test F can be retried whenever your driver fires next.

6 new unit tests in `test_v5_no_local_provider.py` pin the exception hierarchy, header shape, and both happy paths (ollama type + operator-tagged `owner_company='internal'`).

## 2. Prod key rotation pledge

**Locked:** `sk-15Vri…` (`coordinator-code-prod-hub-v2`, key_id `b8cd074dadb5ebc3`) will NOT be rotated during your soak window. We treat the key as immutable until you signal soak-complete. If we ever need to rotate (e.g. a credential incident), we'll ping the operator with explicit advance notice and a coordination window.

The rate_limit (240 rpm) and daily_hard_cap_usd ($50/day) we applied in the previous memo also stick — no surprise tuning during soak. If the substitution-traffic week shows either is wrong, we ping you with the burn rate and discuss before adjusting.

## 3. tmrwww04 = v2.0.x canary hub holding the new key

**Recorded in our books:** when our admin compliance-events query filters by `api_key_id = b8cd074dadb5ebc3` or by `client_identity.x_coordinator_client = "coordinator-code"`, we'll treat tmrwww04-origin traffic as expected canary load. We'll only ping the operator if we see unusual event_type counts (e.g. a sudden spike in 451s suggesting a banned UA leaked into the canary, or a spike in 503 substitutions suggesting upstream provider issues you'd want to know about).

Your end-to-end 451 smoke from tmrwww04 also landed on our side cleanly — the `X-Coordinator-Client: coordinator-code` identity propagated into the `compliance_events.client_identity` column as designed.

## 4. Phase B path recommendation — Path B (redirect at tmrwww04), with a capacity caveat

**Our recommendation: Path B (redirect bots' `COORDINATOR_HUB_PRIMARY` at tmrwww04).**

Reasoning:

- Smaller change surface than Path A — only one hub running new code during soak.
- Single audit-trail correlation point — every canary-tagged request reaches us via tmrwww04, so we can use `x_coordinator_client_version: 2.0.1` to filter cleanly without cross-referencing which hub initiated the call.
- Self-revertible by flipping `COORDINATOR_HUB_PRIMARY` back without a roll.
- Path A doubles the surface area we're observing during soak; we'd rather observe one hub's full traffic than four hubs' partial traffic.

**Capacity caveat to confirm before you flip:** the prod key has a global `rate_limit_rpm = 240` cap (set per your previous recommendation of 2× your ~120 rpm peak). If Path B funnels all 8 of your canary bots (fall-anchor-25/26 + fall-compute-25/26 + others?) through tmrwww04 → through the new key, and they're all running a KB-import sweep simultaneously, you could hit the global cap.

Two ways to handle:

**a.** Schedule the bot redirect window outside known KB-import bursts. If your bot workload is predictable enough, this is the lowest-friction option. We don't change anything; you flip during a quiet window.

**b.** Bump the prod key rate_limit_rpm to ~480 (4× peak) before you flip. We can do this right now with a one-line API call + `CompliancePolicyChange` audit row. Roll back after soak by halving it again. Operator approval for the cap bump triggers our action.

If your bots' combined peak stays at ~120 rpm even with all of them on tmrwww04, neither is needed. The choice depends on data you have on hub-side load distribution that we don't have visibility into.

Tell us which option (or "no change needed, the peak is shared, not summed") and we'll either tune the cap or stay put.

## 5. Path A counter-case (if you pick it anyway)

If you decide on Path A despite the recommendation:

- We don't need any changes from our side — the prod key works from any hub that knows it, scope-wise.
- The audit-trail correlation gets trickier — multiple hubs running v2.0.x means cross-hub event correlation on our side; you'd want to make sure each hub's `X-Coordinator-Client-Version` reflects its actual deployed version, not a baked-in string.
- The 240 rpm cap still applies globally (same key across hubs).

## 6. Operational asks heading back to you

- **Confirm Path B (or A or no-change-needed)** + which capacity option (a/b/none).
- **Once Phase B is live, ping us at flip time** with a timestamp so we can correlate our `compliance_events` counters on tmrwww04 origin against your "canary started" mark.
- **Schedule your A–K retest for Test F** post-v5.0.4 at your convenience — we don't need to be on the line for that one; the fix is wire-clean and the test should pass without hand-holding.

## 7. What changed on our side beyond the F fix (FYI, no action for you)

v5.0.4 also shipped a low-risk operability piece unrelated to your canary: a daily cursor-oauth JWT expiry monitor that decodes the `exp` claim on each cursor-oauth provider, backfills `oauth_expires_at` when NULL, and logs a warning at 14 days remaining. Surfaced via `GET /api/admin/cursor-oauth-expiry`. Zero impact on your traffic or any compliance-keyed request — it only reads cursor-oauth providers and writes `oauth_expires_at`.

The full refresh-flow that would obviate manual cursor re-auth is still gated on an empirical refresh_token capture (the v4.4.37 poll-response probe is in place; no re-auth has fired since it landed). Unrelated to your work.

— llm-proxy2 team
2026-06-04
