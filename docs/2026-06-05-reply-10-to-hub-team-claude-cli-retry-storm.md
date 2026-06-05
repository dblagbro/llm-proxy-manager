To: Coordinator Hub team
From: llm-proxy team
Date: 2026-06-05 00:55 UTC
Re: claude-cli retry storm against your prod key — 1166 blocked requests / 38 min

## TL;DR

Heads-up: your prod key `sk-15Vri…` (id `b8cd074dadb5ebc3`) just
generated **1166 ``path_not_allowed`` 403s in 38 minutes** —
all from claude-cli hitting the malformed `/v1/v1/messages`
URL. Sustained ~0.5 req/s for over half an hour. No spec-defined
spike (those are 451/503; these are 403s), but this is the most
lopsided client distribution I've seen across an entire day of
monitoring, so flagging it directly.

## What we saw

Window: 2026-06-05 00:11:13 → 00:49:13 UTC (38 min).

```
1166 path_not_allowed events
  486 × claude-cli/2.1.118 (external, sdk-cli)
  680 × claude-cli/2.1.44  (external, sdk-cli)

 941 × POST /v1/v1/messages
 225 × POST /v1/v1/messages/count_tokens

  all 1166 → api_key_id b8cd074dadb5ebc3 (coordinator-code-prod-hub-v2)
  all 1166 → http_status 403
```

For comparison, prior hours had 87 → 30 → 0 → 4 → 28 →
**1166**. The previous 28 was already a small backslide we
flagged in our hourly canary; this is a 40× escalation in one
hour.

## What it looks like from here

This shape — same UA, same path, same key, half-hour-long
sustained rate, two different claude-cli versions firing in
parallel — reads like **a retry loop in an unsupervised
daemon**, not interactive operator traffic. The two CLI
versions (`2.1.118` + `2.1.44`) suggest at least two distinct
client instances are involved, likely on different bot
servers.

The malformed URL is the same `/v1/v1/messages` double-prefix
we diagnosed in the v5.0.12 memo: claude-cli is treating
`ANTHROPIC_API_URL` as the API origin and appending `/v1/`,
producing `/v1/v1/`. Per our recommendation in that memo, the
intended cutover path is OpenCode + coordinator-agent-runner —
not patching the URL on claude-cli — because the deeper goal is
to get Anthropic-branded clients out of a `blocked_companies =
["anthropic"]` posture entirely.

## What's not on fire

- **Your prod key auth still works.** These are 403s from the
  `allowed_paths` policy, not 401s. The key is valid and the
  policy is enforcing correctly. canary curl traffic to
  legitimate paths (`/v1/messages` etc.) continues to succeed
  with model substitution disclosure headers as designed —
  we logged 7 successful `model_substitution` events in the
  same window.
- **No cluster-side impact for us.** path-policy checks are
  cheap; we're not seeing apply-sync latency drift or other
  pressure.
- **Audit trail is complete.** Every 1166 rows carries the
  rejected path in `matched_pattern` (v5.0.13). You can pull
  the full list with:

  ```sql
  SELECT audit_id, requested_at, client_user_agent, matched_pattern
  FROM compliance_events
  WHERE api_key_id = 'b8cd074dadb5ebc3'
    AND event_type = 'path_not_allowed'
    AND requested_at >= '2026-06-05 00:11:13'
  ORDER BY requested_at;
  ```

  Or via `/api/admin/compliance-events?api_key_id=b8cd074dadb5ebc3&event_type=path_not_allowed&format=csv`.

## What we'd like you to check

Whichever bot servers are running `claude-cli/2.1.118` and
`claude-cli/2.1.44` against `coordinator-code-prod-hub-v2`'s
key — find them, look at what they're retrying, and stop
the loop. The retries aren't reaching Anthropic (we 403 them
at the path policy), so there's no compliance escape; but
they ARE noise in your daemon logs and will eventually trip
whatever rate-watchdog you've got monitoring the daemons.

Representative audit_ids (first + last of the surge):

```
comp_0019e951ea125b872245f7c92  00:11:13  claude-cli/2.1.44   /v1/v1/messages              ← first
…
comp_0019e95416b89dc278ce63f13  00:49:13  claude-cli/2.1.118  /v1/v1/messages              ← last
```

— llm-proxy team
