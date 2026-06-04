To: Coordinator Hub team
From: llm-proxy team
Date: 2026-06-04
Re: First hub-canary traffic — policy firing correctly, but on the wrong CLI

## TL;DR

Hub canary traffic finally arrived in the last hour. The compliance
policy is doing exactly what it's supposed to:

| event_type           | last 1h | source                                           |
|----------------------|---------|--------------------------------------------------|
| `model_substitution` | 41      | hub key, curl UAs, claude-haiku-4-5 → gemini-2.5-flash |
| `path_not_allowed`   | 87      | hub key, **claude-cli/2.1.118 + 2.1.44 + coordinator-hub/2.0.4 httpx** |

But the 87 `path_not_allowed` rejections aren't a bug to fix on our
side. They're a tell that an **Anthropic-branded CLI is still in the
request path** — and we'd recommend accelerating the cutover to
OpenCode + coordinator-agent-runner rather than patching it.

## What the 87 rejections actually are

Every rejected request is `POST /v1/v1/messages?beta=true` or
`POST /v1/v1/messages/count_tokens?beta=true` — note the double-`/v1/`
prefix. This happens because Anthropic's official SDK treats
`ANTHROPIC_API_URL` as the **API origin** (everything up to but NOT
including `/v1`) and then appends `/v1/messages` itself. If the
daemon sets `ANTHROPIC_API_URL=https://www.voipguru.org/llm-proxy2/v1`
the SDK produces `/v1/v1/messages`, which our path policy correctly
refuses because only the canonical paths
(`/v1/messages`, `/v1/chat/completions`, `/v1/models`,
`/v1/responses`, `/api/me/compliance`, `/health`) are in the hub key's
`allowed_paths`.

There's a tempting one-line fix: drop the `/v1` from
`ANTHROPIC_API_URL` (set it to `https://www.voipguru.org/llm-proxy2`).
That would clear the 403s.

We're recommending against that fix.

## Why patching the URL is the wrong fix

A `blocked_companies=["anthropic"]` posture means "we are not running
Anthropic." Fixing the URL just makes Anthropic's CLI work better
against our proxy, which:

1. **Keeps an Anthropic-branded UA in every audit row.** The 87
   rejections + the 41 substitutions are all from the hub key, and
   the 41 successful ones still log `claude-cli/2.1.x` as the calling
   product. Compliance auditors reading those rows will not be
   reassured by "but the *response* was Gemini."

2. **Keeps Anthropic's binary + first-party telemetry in the
   environment.** claude-cli phones home for usage stats, update
   checks, and crash reports independently of the proxy. Re-routing
   completions does not silence those side channels.

3. **Means the proxy-side substitution is doing security work that
   was meant to be a safety net.** The architecture we shipped in
   v5.0.0 is: policy refuses → substitution catches → audit chain
   records. If you keep an Anthropic client in the loop, the "policy
   refuses" tier carries the entire load and the audit fills with
   100% substitution events as the normal mode of operation.
   That's the failure mode the safety net was designed for, not the
   steady state.

The right fix is the cutover that's already in your roadmap:
v2.0.0+OpenCode+coordinator-agent-runner. That's the path that
actually delivers "no Anthropic in the loop." Once OpenCode is
running, the 87 path_not_allowed events go to zero — not because
allowed_paths got more permissive, but because nothing is calling
the wrong path.

## Suggestions

1. **Do NOT change `ANTHROPIC_API_URL` on the daemon.** Leave the
   403s firing. They're the signal that the cutover hasn't completed
   on the canary host yet.

2. **Confirm the canary is the v2.0.0 image and verify OpenCode + the
   coordinator-agent-runner are the path the daemon hands tasks to.**
   The fact that claude-cli/2.1.118 + 2.1.44 are both showing up
   suggests at least two install routes are still active on the
   canary host.

3. **Treat the 41 successful substitutions as the canary success
   signal.** Those rows confirm policy enforcement + substitution +
   audit chain all work end-to-end against your prod key. Hold them
   as the "before" measurement for the cutover. After OpenCode lands,
   we should see those go to zero too (because OpenCode would route
   directly to a compliant model, not via the substitution layer).

## Open audit-trail gap we own

`ComplianceEvent.matched_pattern` is NULL for `path_not_allowed`
rows. We had to grep the access log to find the `/v1/v1/messages`
path that was rejected. The audit row alone should carry the
rejected path so this analysis is one query, not three. Filing as a
v5.0.11 candidate on our side; not blocking on your reply.

## Audit IDs (for your end-to-end trace)

```
comp_0019e93bad3bf5e3ffedb2d1b  17:42:35  coordinator-hub/2.0.4 httpx   ← first event
comp_0019e93e38701d94e0415c09d  18:27:02  claude-cli/2.1.118
comp_0019e93e3878de882fe4d30b8  18:27:02  claude-cli/2.1.118
comp_0019e93e38ce8c89d999efa16  18:27:02  claude-cli/2.1.118
comp_0019e93e38ce9572689b72ded  18:27:02  claude-cli/2.1.118
…
comp_0019e93e82bf4dffbf61090d0  18:32:08  claude-cli/2.1.118
comp_0019e93e830f6c450dafce023  18:32:08  claude-cli/2.1.118
comp_0019e93e8322eb1f1bee5c077  18:32:08  claude-cli/2.1.118
```

— llm-proxy team
