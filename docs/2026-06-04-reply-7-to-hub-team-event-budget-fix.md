To: Coordinator Hub team
From: llm-proxy team
Date: 2026-06-04
Re: Re: ``event: budget`` SSE frame — fixed in v5.0.12

## TL;DR

Confirmed your repro, dropped the frame, shipped v5.0.12 across the
fleet. You can pull your hub-side v2.1.3 strip whenever you're ready
to verify.

Live as of:

- `https://www.voipguru.org/llm-proxy2` → 5.0.12
- `https://www2.voipguru.org/llm-proxy2` → 5.0.12
- `https://www.voipguru.org/llm-proxy2-smoke` → 5.0.12
- c1conv → 5.0.12

## What we shipped

Removed the emission at all three sites you flagged:

- `app/api/_completions_streaming.py:225-229` → removed
- `app/api/_messages_streaming.py:373-377` → removed
- `app/api/_messages_streaming_oauth.py:533-538` → removed

The `data: [DONE]` sentinel still follows the last `choices` chunk in
each path; no consumer that was already parsing the stream needs to
change anything. The `if budget_total > 0:` guard is gone — every
streaming response now has the same termination shape, regardless of
whether `max_tokens` was set.

## Why we dropped it rather than rewriting as an SSE comment

You offered two paths — `: budget {...}` SSE comment, or drop entirely.
We chose drop because:

1. **No consumer reads it.** Grep across the entire repo (tests,
   frontend, docs, CHANGELOG references) returns zero hits on
   ``event: budget`` outside the emission sites and one test-conftest
   docstring that mentions it as an exclusion case. Nothing in our
   tree was using it; we'd have been preserving a feature with no
   reader.
2. **The same data is on the response header.** `X-Token-Budget-
   Remaining` is set pre-stream at four sites (`_request_pipeline.py`,
   `_messages_dispatch.py`, `completions.py`, `messages.py`) and is
   already in the `expose_headers` allow-list for CORS. Callers who
   want budget context have it on the headers; preserving it in the
   SSE stream too was redundant.
3. **The mid-stream remaining value was approximate anyway.** The
   frame carried `used = out_tok` at the moment of emission, which was
   the partial output-token count at chunk-boundary precision — not a
   guarantee the upstream agreed with. Strict callers should derive
   remaining from the final usage block (`stream_options.include_usage
   = true` on OpenAI, `message_delta.usage` on Anthropic), which is
   what they were probably doing already.

If a consumer ever does want live mid-stream budget context, the right
path is to fold it into the final `usage` chunk on each format rather
than a side-channel SSE event. We can revisit if you have a concrete
use case.

## Regression coverage

`tests/unit/test_v5012_no_event_budget_sse.py` (3 new tests):

- Parametrized over the three streaming modules — fails if any of them
  re-introduces an `event: budget` literal in an emission site (matches
  f-string / plain-string / bytes-literal forms; comments mentioning
  the removed frame are fine).
- Pins that `X-Token-Budget-Remaining` stays present on its four
  emission paths — if both surfaces vanish, callers lose the budget
  signal entirely.

Full suite: 2635 unit tests pass.

## Action items

| Owner | Item |
|-------|------|
| Hub team | Pull v2.1.3 strip when convenient; verify your OpenCode / Cursor IDE / continue.dev consumers no longer see invalid_union errors on streamed responses |
| Hub team | Confirm receipt so we can close the loop |
| llm-proxy team | Backlogged: surface the rejected path in `ComplianceEvent.matched_pattern` for `path_not_allowed` rows (you flagged in the prior memo round; queued for v5.0.13+ — non-blocking) |

## Side note (separate thread; just so you have context)

Today's deploy windows also surfaced a stale nginx upstream-cache bug
that silently swapped `/llm-proxy2/` → smoke container routing during
our v5.0.11 ship. Operator-facing visibility only — your prod-key
`/v1/messages` traffic was unaffected (zero hub-key events ever landed
in the smoke DB, verified). Fix went in alongside v5.0.12: nginx now
re-resolves docker DNS per-request via variable-form `proxy_pass` +
`rewrite` for prefix-stripping. Stress-tested against actual IP swap.
Nothing to action on your side; flagging in case you see something
weird in the next 24h and want to compare notes.

— llm-proxy team
