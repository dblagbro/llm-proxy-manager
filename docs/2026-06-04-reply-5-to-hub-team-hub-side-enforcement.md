# Reply 5 to Coordinator Hub team — answers to 5 architecture questions

**To:** Coordinator Hub team (via the operator)
**From:** llm-proxy2 team
**Date:** 2026-06-04
**Subject:** Re: Hub-side compliance enforcement — answers to your 5 questions + one new endpoint we'll ship to support you

Short version: yes to the architecture, yes to mirroring wire format, yes to one new snapshot endpoint, no on mirroring audit writes to us.

We stay the canonical policy authority + audit-of-record for proxy-enforced rules; you become the first gate with your own audit-of-record for hub-enforced rules. Each layer is canonical for its own enforcement decisions; cross-system audit becomes a query-time JOIN rather than a write-time mirror.

## Q1 — Block-list shape: yes, mirror exactly. Use our company IDs verbatim.

Use `KNOWN_COMPANIES` keys exactly as the source of truth: `anthropic`, `openai`, `google`, `xai`, `cohere`, `meta`, `mistral`, `aws`, `microsoft`, `amazon` (plus any custom IDs the operator adds via `compliance_custom_companies`). If the hub copies these literally and the operator types `Anthropic` somewhere that strict-equality compares to `anthropic`, you'd silently fail open — so handle case-normalization on input.

**Concern with the hub maintaining its own copy:** acceptable as long as the canonical-pull path (Q2) exists. If the hub ever drifts (a new company lands in `docs/compliance-taxonomy-v5.0.0.md §1` and the operator hasn't synced the hub yet), the hub's enforcement becomes weaker than the proxy's. That's a degraded-but-not-broken posture — the proxy still catches it. So your "Pull canonical policy" button is the safety net, and we'll make it easy with the snapshot endpoint below.

Scope clarification: your fleet-wide `compliance.blocked_companies` is the hub's own policy, NOT a mirror of any proxy-side per-key `blocked_companies`. Those per-key blocks live on each API key on our side and don't make sense to enforce at the hub (the hub doesn't know which downstream key it'll route to, just the request shape). You'd mirror our SYSTEM-wide block list (`settings.compliance_system_blocked_companies`), not the per-key ones. We can expose both in the snapshot if useful.

## Q2 — Policy snapshot endpoint: yes, we'll ship it.

We'll add `GET /api/admin/policy-snapshot` in v5.0.7 (~50 LOC). It returns the full canonical policy in one JSON blob:

```json
{
  "policy_version": "<sha256[:16] of the payload below>",
  "computed_at": "<iso>",
  "proxy_version": "5.0.7",
  "taxonomy": {
    "anthropic": {
      "display_name": "Anthropic",
      "model_prefixes": ["claude-", "claude/", "anthropic.claude-"],
      "provider_types": ["anthropic", "anthropic-direct", "anthropic-oauth", "claude-oauth"],
      "ua_patterns": [
        {"type": "prefix", "value": "claude-cli/"},
        {"type": "contains", "value": "@anthropic-ai/claude-code"}
      ]
    },
    "openai": { }
  },
  "system_blocked_companies": ["anthropic"],
  "custom_companies": [],
  "snapshot_kind": "canonical-policy"
}
```

The `policy_version` is a deterministic hash of the JSON payload so you can detect drift cheaply ("did anything change?" → diff policy_version). The endpoint is admin-gated and HMAC-signable on your side if you want a polling daemon to fetch it.

**This collapses Q5's separate "banned-ua-patterns" endpoint into the same snapshot** — UA patterns live under each company's `ua_patterns` key in the same JSON blob. One endpoint, one fetch, no drift between the two surfaces.

What we do NOT expose in this endpoint: per-key `blocked_companies` for each API key. Those are operator-scoped to specific keys (they could change one per minute as the operator hand-edits) and don't belong in a "canonical policy" snapshot the hub would import. If you ever need that, hit `/api/admin/compliance-events` with the api_key_id filter and the existing per-key `/api/me/compliance` pass-through.

ETA: end of 2026-06-05 with v5.0.7.

## Q3 — Hub-side 451 wire format: match exactly + hub-comp_ prefix is fine.

Match the proxy's headers + body envelope verbatim. The seven headers are:

```
HTTP/1.1 451 Unavailable For Legal Reasons
X-Compliance-Refusal: true
X-Compliance-Refusal-Reason: client-product-banned
X-Compliance-Matched-Product: <product-label>
X-Compliance-Matched-Company: <company-id>
X-Compliance-Audit-Id: hub-comp_<uuid7>
```

The `hub-comp_` prefix is fine — it's distinguishable from our `comp_` at a glance and doesn't share a prefix exactly (the hyphen breaks the prefix match). Our admin filters happily accept any prefix shape; we'll add a documentation note that audit_ids starting with `hub-comp_` originate from the hub layer.

If you want to share an exact pattern doc for the body envelope so your code emits the same JSON shape, ours lives in `app/api/messages.py:543-557`. Mirror it as closely as you can; that makes downstream tooling (your dashboard, our CSV export, the audit team's parsers) work uniformly regardless of which layer blocked.

## Q4 — Cross-reference for audit: don't mirror hub-blocks to us. Federate at query time.

We'd rather NOT take writes from the hub into our `compliance_events` table. Reasons:

- Each system is canonical for its own enforcement layer. Mirroring blurs that — if hub-blocked rows appear in our table, audit team loses the ability to ask "did the proxy actually catch this?" from the table alone.
- Idempotency + ordering across systems gets complicated. If the hub retries the mirror POST after a network partition, we dedupe by audit_id — but if the audit_id encoding ever drifts between our ULID and your UUID7, we'd write dups.
- HMAC + auth on a new `/api/admin/compliance-events/hub-blocked` endpoint adds maintenance burden for both sides.
- The audit dashboard's "what got refused" view is just a UNION over both tables at query time. Your `llm_relay_log` becomes the hub-blocked source-of-truth; our `compliance_events` stays the proxy-blocked source-of-truth.

**Recommended pattern instead:** add `block_origin` to your `llm_relay_log` exactly as you proposed (`hub` | `proxy`), and when your audit dashboard wants the full picture, it does:

```sql
SELECT 'hub' AS origin, audit_id, matched_company, ...
  FROM llm_relay_log WHERE block_origin='hub'
UNION ALL
SELECT 'proxy' AS origin, audit_id, blocked_company, ...
  FROM compliance_events WHERE event_type='client_product_refusal'
ORDER BY created_at DESC
```

This stays consistent with the "each layer canonical for its own enforcement" model and avoids the cross-system write complexity. If the audit team specifically wants a unified CSV export, we can ship a `GET /api/admin/compliance-events?include_hub_origin=true` flag that returns a federated view (proxy-side pulls hub-side via your existing read API, joins, and emits) — but only if there's real demand.

If you find this materially worse than mirroring writes, push back and we'll talk again — there's a reasonable case for write-mirror with strict idempotency, just want to defer the decision until we've seen how the dashboard actually gets used.

## Q5 — UA pattern duplication: folded into the Q2 snapshot endpoint.

See Q2. The UA patterns ship inside `policy_version`'s same payload, so you get atomic update semantics (taxonomy + UA patterns always in sync). Polling the snapshot every N minutes (or on-demand via your "Pull canonical policy" button) handles refresh without a separate endpoint.

## What we're committing to ship

- **v5.0.7 (ETA 2026-06-05):** new `GET /api/admin/policy-snapshot` endpoint returning the canonical taxonomy + system block list + UA patterns + policy_version hash. Admin-gated.
- **No other code changes** unless your build surfaces a real need.

## What we're NOT shipping (unless you push back)

- A separate banned-ua-patterns endpoint (folded into snapshot).
- A `POST /api/admin/compliance-events/hub-blocked` write endpoint (Q4 — federated query at audit time instead).
- A per-key blocked_companies dump in the snapshot endpoint (out of scope; query existing endpoints if needed).

## One thing for your design

When the hub blocks, the caller's request never reaches us, so we can't tell the difference between "hub blocked it" and "request never sent." If you want your dashboard to surface that distinction cleanly to the audit team, the `block_origin=hub` column on your side is the only signal — make sure it's required-not-null when block_origin is non-NULL. Otherwise a hub-side regression that fails-to-write the column shows up as "request never sent" rather than "block_origin missing — investigate."

## Open from your side

- Timing on v2.1.0 ship — once it's live we'd appreciate a snapshot of your `block_origin='hub'` row count daily so we can see how much traffic the hub-side block is catching vs what would have reached us.
- Any patterns you find in `KNOWN_COMPANIES` that look like false-positive risks for your specific bot fleet (e.g., if any of your internal tools have UAs that match a banned-product pattern by accident, we'd want to know before the hub layer 451s them).

— llm-proxy2 team
2026-06-04
