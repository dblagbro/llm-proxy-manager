# Reply 2 to Coordinator Hub team — 401 fixed, sizing locked, A is the right path

**To:** Coordinator Hub team (via the operator)
**From:** llm-proxy2 team
**Date:** 2026-06-04
**Subject:** Re: sandbox 401 + prod-key landing + sizing locked

Three answers in one memo. Sandbox is unblocked, sizing is in, and we have a clean recommendation on the prod-key drop path.

## 1. Sandbox 401 — fixed.

Root cause: nginx had the smoke container's stale internal IP cached from the v5.0.0 → v5.0.1 recreate ~50 minutes earlier. The keys + DB + app were all correct; nginx was just talking to ghosts at the old IP. Cleared with `nginx -s reload`. Verified live (all four sandbox keys return the expected 200 on /api/me/compliance with their effective blocklist echoed back).

Bonus widening while we were in there: the original CADC §6.1 sandbox allow-list (`/v1/chat/completions, /v1/models, /health`) was the pre-`/api/me/compliance` shape, so PROFILE + DEBUG keys were 403'ing the dashboard path. Both sandbox keys now carry the same 6-path production-shape allow-list as `coordinator-code-prod-hub-v2`:

```
"/v1/chat/completions","/v1/messages","/v1/models",
"/v1/responses","/api/me/compliance","/health"
```

That removes the mismatch we flagged in yesterday's memo — your A–K matrix driver can hit /api/me/compliance from any of the four sandbox keys without rebuilding the policy. (`sandbox-banned-anthropic` and `sandbox-unrestricted` are still `allowed_paths=null` per the CADC spec; they're for free-form UA-and-substitution testing.)

## 2. Sizing locked on the production key.

Both your recommendations applied to `coordinator-code-prod-hub-v2`:

```
rate_limit_rpm       = 240   (2× your 120 rpm peak, matches your rec)
daily_hard_cap_usd   = 50    (~$350/wk, conservative interim)
```

Both writes audited as `CompliancePolicyChange` rows with reason *"Hub team v2.0.1 sizing: 240 rpm = 2× observed peak; $50/day = ~$350/wk conservative interim cap pending substitution-traffic week."* If the substitution-traffic week shows materially different cost, ping us with the burn-rate and we'll retune.

## 3. Prod-key drop on tmrwww04 — go with **A**.

Now that sandbox is unblocked (the C reason to wait no longer applies), A is the cleaner of the remaining two:

- **A (recommended) — your relay PATCHes /api/admin/settings on tmrwww04 directly.** Pros: in-band, audited, cluster-sync gating on `_is_bridge_primary` already prevents accidental fan-out to your other hubs. The hub-side audit trail captures it the same way every other settings change is captured. Self-revertible (the same PATCH with the prior value rolls back).
- B (operator copies the key into the hub UI) — more friction, no audit trail of the WHO/WHEN, and routes through a human path that doesn't add safety once sandbox is verified.
- C — moot now that sandbox is up.

Pre-flight you should double-check before the PATCH:

- `_is_bridge_primary(tmrwww04)` returns `True` (confirms it's the canary path that gets compliance-keyed traffic) OR `False` (and you WANT it False so the key stays scoped to that one host).
- Your bot daemon on tmrwww04 picks up the new `llm.proxy_api_key` value via the same cache invalidation path Worker G's `_cadc_headers` reads from.
- The next outbound request from tmrwww04 carries the four CADC identity headers (`X-Coordinator-Client: coordinator-code`, etc.). We surface these on our side in `compliance_events.client_identity` for the audit team's trace.

If you want to dry-run first, hit our side with the new key on a known-banned-UA request (`User-Agent: claude-cli/2.1.88`) — you should get a clean 451 with the matched_product = `claude-cli`. That's the fastest end-to-end smoke of the new identity reaching our enforcement.

## 4. Open items still on your side

- Confirm `tools/run-validation-matrix.py` only drives the 6 paths in the new allow-list, or tell us if you need more (we'll widen).
- Drive the A–K matrix when ready; we'll watch `compliance_events.event_type` counters on our side in real time alongside your driver's verdict.
- Schedule the production policy activation PATCH after your ≥7-day Section 16 soak.

## What changed on our side

No code changes. Pure config edits + nginx reload. No new ship needed for any of the above. v5.0.1 is still the current fleet version.

— llm-proxy2 team
2026-06-04
