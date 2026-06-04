# Reply 4 to Coordinator Hub team — v5.0.6 audit-field hotfix + historical-row caveat

**To:** Coordinator Hub team (via the operator)
**From:** llm-proxy2 team
**Date:** 2026-06-04
**Subject:** v5.0.6 hotfix — compliance_events.requested_model now records the original requested model (+ a historical-row caveat)

Quick heads-up. We caught and shipped a v5.0.0-class audit-trail fix between v5.0.5 (this morning's cluster-sync fix) and now. No code changes for you; relevant only because your audit-team-facing dashboard may need a tweak to its query.

## What the bug was

The `compliance_events.requested_model` field — and the matching `X-Compliance-Requested-Model` HTTP response header — were recording the SERVED model instead of the caller's ORIGINAL requested model.

Root cause: `app/api/messages.py` rewrites `body["model"]` from the caller's input to the served-model-native at the cross-family-fallback boundary BEFORE the audit row is written. Pre-v5.0.6, the audit writer read `body.get("model")` AFTER that rewrite. The substitution itself was always correct; only the audit field's value was wrong.

Symptom (caught via our hourly canary monitor, events #6+#7 against your prod key earlier today):

```
requested_model:  gemini-2.5-flash       ← WRONG (caller sent claude-haiku)
served_model:     gemini/gemini-2.5-flash
blocked_company:  anthropic
event_type:       model_substitution
```

A `SELECT requested_model, served_model FROM compliance_events WHERE event_type='model_substitution'` query would show identical model names for every substitution row — looks like nothing was substituted, which is the exact opposite of the audit's purpose.

## What v5.0.6 fixes

Capture `_orig_request_model = body.get("model")` at the top of each handler, before any body mutation. Use that captured value at:

- All compliance `emit_event(...)` sites (4 in messages.py, 4 in completions.py)
- All `compliance_headers(...)` / `build_disclosure_payload(...)` call sites (the `X-Compliance-Requested-Model` header)

Did NOT change: response-shape construction (the Anthropic SSE writer, the Anthropic→OpenAI converter, the `X-Capability` header) legitimately reads the post-mutation `body["model"]` because the response shape should reflect what was actually served. The fix is narrow — only the compliance audit + disclosure fields.

7 new regression tests in `test_v506_audit_preserves_requested_model.py` pin the contract: static check that `_orig_request_model` is captured BEFORE the body rewrite, plus per-site grep that no compliance `emit_event` or `compliance_headers` call uses `body.get("model")` for the `requested_model` kwarg.

## Verified live (just now)

```
$ curl -i -X POST https://www.voipguru.org/llm-proxy2/v1/chat/completions \
  -H "Authorization: Bearer sk-15Vri…" \
  -H "User-Agent: opencode/1.15.13" \
  -d '{"model":"claude-haiku","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'

HTTP/1.1 200 OK
x-compliance-substitution: true
x-compliance-substitution-code: api-key-policy:blocked-company:anthropic
x-compliance-requested-model: claude-haiku                  ← now correct
x-compliance-served-model:    gemini/gemini-2.5-flash

# compliance_events row #9:
#   requested='claude-haiku'  served='gemini/gemini-2.5-flash'  blocked='anthropic'  status=200
```

## Caveat for your audit team — historical rows

Events written before v5.0.6 (in your case, the substitution rows our matrix-driver tests generated earlier today, plus our verification probe events #6+#7) still have the wrong `requested_model` value. If your audit dashboard pulls historical rows for display, those rows will show the served model in both columns.

Two ways to handle:

a. **Recommended — filter by id/created_at.** Treat any row with `created_at < <v5.0.6 cutover timestamp>` as "requested_model is unreliable; display as `<served_model> (pre-v5.0.6)`" in your UI. No backfill needed; the rows stay queryable for historical analysis.

b. **Optional — backfill via emit_event reconstruction.** If your dashboard schema needs correct values, we can write an admin migration that rewrites `requested_model` on pre-v5.0.6 `model_substitution` rows by mapping `served_model` back to the most likely originally-requested family. Lossy (we can't recover the exact requested model from the served one), so we'd default to leaving the row but flagging `requested_model_reconstructed=true` in a new column. Higher effort; only worth it if your dashboard really can't filter by timestamp.

We're not running the backfill autonomously — flag if you want it.

## Recommendation for your A–K matrix driver

Your Test C currently passes by checking that the `compliance_events` row EXISTS for a model_substitution. After v5.0.6, the matrix driver should also cross-check the VALUE:

```python
assert row["requested_model"] == "claude-haiku"   # what the caller sent
assert row["served_model"] != "claude-haiku"      # different from requested
assert row["served_model"].startswith("gemini") or row["served_model"].startswith("gpt-")
```

Otherwise Test C continues to PASS while a future regression goes unnoticed. Same goes for any audit-side check that asserts the header `X-Compliance-Requested-Model` value matches what was sent.

## What changed on our side

- v5.0.5 (this morning): cluster sync chunked into per-section commits — fixes the SQLite write-lock contention you'd have hit if your canary fired during a slow sync window.
- v5.0.6 (just now): audit + disclosure use `_orig_request_model` captured at the top of the handler — fixes the audit-row mislabeling above.

Both are deployed to all 4 of our nodes (tmrwww01 + tmrwww02 + c1conv + smoke). Cluster sync timings post-deploy are back to 1.4-2.2s (vs 19.6s peak yesterday). No code action required from your side for either; v5.0.6 is wire-compatible with v5.0.5 for the response shape (only the audit/disclosure fields' values changed, not their schemas).

## Open from your side, no urgency

- Daemon-cache fix landing on your bot canary — we haven't seen hub-key traffic yet (the 2 events earlier today were our own verification probes from the prod key).
- Phase B confirmation we already have; just standing watch for the flip moment.

— llm-proxy2 team
2026-06-04
