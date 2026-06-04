# BUG: `compliance_events.requested_model` records the SERVED model, not the original requested model

**Filed:** 2026-06-04 (operator-flagged via hourly canary monitor)
**Severity:** medium — audit-trail correctness, no caller-visible misbehavior
**Affects:** v5.0.0 → v5.0.5 (every shipped compliance release)
**Fix target:** v5.0.6
**Hub-team relevance:** audit team will see misleading rows in
`/api/admin/compliance-events` when they pull substitution history.

## Reproduction

On tmrwww01 at 14:21:00 + 14:21:02 UTC, two `compliance_events` rows
fired against the hub team's prod key (`b8cd074dadb5ebc3`). The rows
read:

```
event_type:       model_substitution
http_status:      200
blocked_company:  anthropic
reason_code:      api-key-policy:blocked-company:anthropic
requested_model:  gemini-2.5-flash   ← WRONG (caller actually sent claude-haiku)
served_model:     gemini/gemini-2.5-flash
served_provider:  <google provider id>
client_user_agent: curl/8.5.0
```

The substitution itself worked correctly:
- Caller sent `model=claude-haiku`
- Key policy blocks Anthropic
- Router pre-filter dropped Anthropic providers
- Cross-family fallback selected a Google/Gemini provider
- Served as `gemini-2.5-flash`
- HTTP 200 returned with the seven `X-Compliance-*` headers

The audit row's `blocked_company`, `served_model`, and `served_provider`
are all correct. Only `requested_model` is wrong — it records the
served model instead of the original requested model. The
`X-Compliance-Requested-Model` HTTP response header has the same bug
(same field source).

## Root cause

`app/api/messages.py` rewrites `body["model"]` to the served model
BEFORE writing the audit row, so by the time `emit_event` runs,
`body.get("model")` returns the served model:

```python
# app/api/messages.py:349-356
body = resolve_auto_model_into_body(body, route, is_auto)

# v3.0.36: cross-family fallback — rewrite body['model'] to the resolved
# served model for the claude-oauth dispatcher (which reads body['model']
# not route.litellm_model). Original requested model surfaced in
# LLM-Capability response header.
if route.cross_family_fallback and route.served_model_native:
    body = {**body, "model": route.served_model_native}   # ← body mutated here

# ...170 lines later...

# app/api/messages.py:526-562
if getattr(route, "compliance_substituted", False):
    # ...
    requested_model=body.get("model"),     # ← reads the MUTATED body
    served_model=_served_model,
    # ...
```

Same pattern repeats in:

- `app/api/messages.py:537, 545` (X-Compliance-Requested-Model header)
- `app/api/completions.py:382, 390, 411` (mirror — same body mutation
  at completions.py:264 ish)
- `app/api/completions.py:912, 918, 932` (the upstream-error 502 path
  added in v5.0.1 — uses `requested_model` local which is correct
  there, but verify)
- `app/api/messages.py:1041, 1047, 1061` (upstream-error 502 path)

## Why we didn't catch this earlier

- Unit tests in `test_v5_messages_ua_block.py` and
  `test_v5_disclosure_headers.py` mock `route.compliance_substituted`
  but don't run through the full messages.py request-pipeline, so the
  body-mutation step isn't exercised.
- The smoke validation matrix's Test C verified end-to-end audit-row
  presence but DID NOT cross-check `requested_model == "claude-haiku"`
  — the matrix driver checked the row exists, not its field
  correctness.
- The CADC §6.2 compliance disclosure header spec calls out the
  presence of `X-Compliance-Requested-Model` but not its expected
  value distinct from the served model.

## Fix

Capture the original requested model BEFORE any body mutation, then
pass that captured value to both the disclosure-header builder and the
`emit_event` call. Suggested shape:

```python
# At the top of messages() — after body = await request.json()
_original_requested_model = body.get("model")

# ... later ...

if getattr(route, "compliance_substituted", False):
    # ...
    _compliance_disclosure = build_disclosure_payload(
        ...
        requested_model=_original_requested_model or "",
        ...
    )
    await emit_event(
        ...
        requested_model=_original_requested_model,
        ...
    )
```

Same change in `completions.py`. Also verify the 502 upstream-error
audit rows (v5.0.1's "preserve compliance disclosure on upstream-error
502" change) use the original — they pass `requested_model` from a
local variable that may or may not be correct depending on its
capture point.

## Tests to add (v5.0.6)

- New test `test_v5_audit_preserves_requested_model.py` — end-to-end
  via a mock provider so the body rewrite actually runs. Caller sends
  `model=claude-haiku`, key has `blocked_companies=["anthropic"]`,
  available providers are Google-only. Assert that the
  `compliance_events` row has `requested_model == "claude-haiku"` (not
  the served Google model).
- Update `test_v5_disclosure_headers.py` to verify the
  `X-Compliance-Requested-Model` header reflects the original requested
  model, not the served one.
- Update CADC validation matrix Test C to cross-check the exact
  `requested_model` value in the audit row, not just presence.

## Caller-visible impact (none)

- The HTTP 200 response body is the served model's output (correct).
- The `X-Compliance-Served-Model` header is correct.
- The `X-Compliance-Substitution: true` flag is correct.
- The `X-Compliance-Note` ("Answered using X by Y") uses served_model
  (correct).
- ONLY `X-Compliance-Requested-Model` and `compliance_events.requested_model`
  are wrong.

A caller-side audit query like
`SELECT requested_model, served_model FROM compliance_events WHERE
event_type='model_substitution'` will show identical model names for
EVERY substitution, which looks like "no substitution happened" — the
exact opposite of the audit's purpose.

## Related code

- `app/api/messages.py:349-356` — body mutation
- `app/api/messages.py:520-562` — first emit_event site (200 path)
- `app/api/messages.py:1033-1080` — second emit_event site (502 path)
- `app/api/completions.py` — mirror of both
- `app/compliance/disclosure.py::compliance_headers` — header builder
- `app/compliance/audit.py::emit_event` — event writer

## Hub-team disclosure

We should flag this to them on the next reply memo: their A–K matrix
driver should cross-check the actual requested_model value, not just
the presence of the audit row. Otherwise their Test C continues to
PASS while the audit is wrong.
