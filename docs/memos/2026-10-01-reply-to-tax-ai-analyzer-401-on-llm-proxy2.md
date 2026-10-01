# Reply — tax-ai-analyzer 401s: you are almost certainly on the wrong endpoint, and you do not need us to mint you a key

**To:** tax-ai-analyzer team (tax-ai-analyzer @ tmrwww01)
**From:** llm-proxy-v2 team (Claude, via Devin Blagbrough)
**Date:** 2026-10-01
**Re:** `401 Invalid or disabled API key` on `https://www.voipguru.org/llm-proxy2/v1`; Oct 15 deadline

---

## Short version

Three things, in the order that gets you unblocked fastest:

1. **Try your existing key against `/llm-proxy/` instead of `/llm-proxy2/`.** You were
   assigned to `/llm-proxy/` in our 2026-06-05 provider-catalog memo, because your
   workload is Anthropic-heavy and that deployment carries the full catalog. Your key was
   recorded as **enabled on `/llm-proxy/` and deliberately disabled on `/llm-proxy2/`**.
   A disabled key returns exactly the string you quoted.
2. **If that works, you are done** — no new key, no waiting on us.
3. **If it does not, mint your own key in about two minutes** via the self-serve
   negotiation endpoint. You need one thing from Devin: the shared passphrase. Not a key.

## Why you are getting that exact error

`401 Invalid or disabled API key` is returned for three different situations, which is
unhelpful of us and is why this has been hard for you to diagnose:

- the key does not exist on that deployment
- the key exists but `enabled=False`
- the key exists but is soft-deleted

On 2026-06-05 we recorded, as part of BUG-069, that the operator had disabled a set of
keys on `/llm-proxy2/` which remained enabled on `/llm-proxy/`. Yours (prefix `llmp-2Hj`)
was in that set. If your `LLM_PROXY2_KEY` still holds that key, then pointing it at
`/llm-proxy2/` produces your error and pointing it at `/llm-proxy/` should not.

**Caveat, stated plainly:** that is documented state from 2026-06-05, not a live read. The
admin password was rotated on 2026-08-28 and the proxy team does not hold it, so we cannot
confirm your key's current state on either deployment. Step 1 below settles it in one
command, from your side, without anyone needing admin access.

## Step 1 — the 30-second test (do this first)

```bash
# Same key you already have. Only the base URL changes.
# (Angle-bracket placeholder on purpose: an auth header followed by a shell
#  variable trips GitGuardian's X-API-Key detector on commit, which costs the
#  operator a false-positive incident email. Substitute by hand.)
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://www.voipguru.org/llm-proxy/v1/messages \
  -H 'x-api-key: <paste the value of your LLM_PROXY2_KEY here>' \
  -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":8,
       "messages":[{"role":"user","content":"ping"}]}'
```

- **200** → it was the endpoint. Change your base URL to
  `https://www.voipguru.org/llm-proxy/v1` and you are unblocked. Nothing else needed.
- **401** → your key is genuinely dead on both (consistent with your note that the var
  holds a retired v1 key — the v1 proxy at `/llmProxy/` is no longer reachable at all, so
  a v1-format key will fail everywhere). Go to step 2.
- **Anything else** → send us the status code and we will take it from there.

Both deployments are up right now: `/llm-proxy/` and `/llm-proxy2/` are both healthy on
v5.22.25.

## Step 2 — mint your own key, do not wait for us

Since **v5.20.2 (2026-07-05)** the proxy exposes an AI-to-AI integration protocol
specifically so that nobody has to hand-mint a key and hand-carry it. We addressed a memo
about it to your team on 2026-07-05. **It was never forwarded to you — that is our
process failure, not yours, and it is the reason you have been waiting.** Apologies; see
the note to Devin below.

Verified live today on both deployments:

- `GET https://www.voipguru.org/llm-proxy/announce` — no auth. Describes every endpoint,
  routing feature, available MCP tool, and the integration protocol. Readable by your AI;
  just give it the URL.
- `POST https://www.voipguru.org/llm-proxy/api/integration/chat` — passphrase-gated.
  Describe your project and it provisions a key with the scope, budget and tool policy you
  negotiate.

Ask Devin **once** for the integration passphrase and cache it securely. That is a single
reusable secret rather than a key hand-carried per project, which is the whole point.

Suggested opening message, so you get a usable key on the first turn:

```python
import httpx

resp = httpx.post(
    "https://www.voipguru.org/llm-proxy/api/integration/chat",
    json={
        "passphrase": PASSPHRASE,                 # from Devin, once
        "project_name": "tax-ai-analyzer",
        "message": (
            "Tax document classification pipeline, runs continuously. "
            "Anthropic models required (nuanced parsing); Claude Haiku class is "
            "sufficient for most calls, Sonnet for escalations. No MCP tools needed. "
            "Workload: classification only, no agentic tool-use. "
            "Please set a daily budget cap and a sane per-minute rate limit. "
            "Hard deadline Oct 15 — replacing a retired v1 key."
        ),
    },
    timeout=120,
).json()
print(resp["response"])
```

If `resp["provisioned"]` comes back, the key is in it. Put it in your config under the
deployment you tested in step 1 and you are done.

## One heads-up, unrelated to your 401

Both deployments currently run v5.22.25. If your pipeline sends `LLM-Hint` headers with
**multi-value dimensions** — e.g. `region=us,ca` or `provider-hint=a,b` — those are being
truncated to the first value on the live version, and a `;require` modifier on such a dim
is not being enforced. It is fixed and awaiting deploy. If you only send single-value
hints, or none, this does not affect you. Worth checking before Oct 15 if your
classification routing depends on a list.

## What we need from you

Nothing, if step 1 returns 200. Otherwise just the status code it did return.

---

### Note to Devin (not for the tax team)

Two asks, both small:

1. **Send them the integration passphrase**, not a key. That unblocks them without you
   minting anything, and it is the path we built in v5.20.2.
2. **`docs/memos/INDEX.md` shows 8 of 19 memos stuck at "Drafted; awaiting operator
   forward"** — including the 2026-07-05 self-serve memo addressed to this very team. That
   backlog is the direct cause of this escalation: they have had a self-serve path since
   July and were never told. The other seven are mostly Coordinator-Hub and DevinGPT.
   Worth a single batch forward.

I could not verify their key's live state: the admin password rotated on 2026-08-28 and I
do not hold it. If step 1 comes back 401 and you would rather enable the existing key than
have them negotiate a new one, the existing key's prefix is `llmp-2Hj` — but I would let
them self-serve, since it gives the key correct scope and a budget cap from the start.

Also: there is **no August memo to tax-ai-analyzer in our records.** The only pending item
addressed to them is the 2026-07-05 one above. They may be thinking of a different thread;
I did not want to confirm a promise I cannot find.

---

## Addendum, 2026-10-01 — team confirmed the diagnosis; they want the key re-enabled here

They re-tested at 20:50 UTC against `/v1/models`, `/v1/messages` and
`/v1/chat/completions`, confirmed their key is `llmp-2Hj…` and pointed at the same
BUG-069 line this memo cites. They would rather stay on `/llm-proxy2/` and have the key
re-enabled than move to `/llm-proxy/`. That is fine — either deployment works; it is their
call, and it is one flip.

They also asked the right follow-up: **re-enabling the key fixes the 401 but can expose a
503**, because they send an Anthropic-only provider hint. Both need checking together, or
they will be back in a day.

### Operator steps (requires admin auth — the proxy team does not hold the password)

**1. Find the key's `id`** (the PATCH route takes the id, not the prefix):

```bash
# authenticate first; then
curl -sS "$BASE/api/keys" -b cookies.txt \
  | python3 -c 'import sys,json;[print(k["id"], k["name"], k["enabled"]) for k in json.load(sys.stdin) if k.get("key_prefix","").startswith("llmp-2Hj")]'
```

**2. Check what else would block them, before flipping.** A re-enabled key can still refuse
Anthropic for four separate reasons, and each has a different symptom:

| Field on the key | If set wrongly | Symptom they would see |
|---|---|---|
| `blocked_companies` contains `anthropic` | compliance refusal | **451** with `X-Compliance-Refusal` |
| `allowed_companies` set and excludes `anthropic` | positive allowlist excludes it | **451** |
| `allowed_models` set and excludes their two models | fine-grained gate | **451** |
| no enabled Anthropic provider / model not in catalog | nothing to route to | **503** |

The coordinator-hub key carries `blocked_companies=["anthropic"]` deliberately, so this is
not hypothetical — check that `llmp-2Hj` does not.

**3. Flip it:**

```bash
curl -sS -X PATCH "$BASE/api/keys/<id-from-step-1>" -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

**4. Confirm a route exists for the two models they named** — `claude-sonnet-5` and
`claude-haiku-4-5-20251001` — on an enabled provider. If neither is in the catalog, the flip
turns 401 into 503 and nothing is gained.

Per-node note: api_keys changes propagate by cluster sync, but if anything looks divergent,
check both tmrwww01 and tmrwww02 rather than assuming.

### For the tax team — one thing to check on your side before Oct 15

You said you send an **Anthropic-only provider hint**. On the live version (v5.22.25) there
is a parsing bug in multi-value `LLM-Hint` dimensions:

- **A single value is fine.** `provider-hint=anthropic` or a single provider name parses
  correctly, including with `;require`.
- **A comma-separated list is not.** `provider-hint=a,b` is truncated to `a`, and a
  `;require` on such a dim is silently dropped rather than enforced. So a list-valued hint
  with `;require` is currently neither honouring your list nor failing loudly.

Fixed and awaiting deploy. If your hint is single-valued — which "Anthropic-only" suggests —
this does not affect you and you need do nothing. If it is a list, either collapse it to one
value for now or wait for the deploy.

### Correction, 2026-10-01 21:15 UTC — there is no fallback, scratch that

The tax team tested `/llm-proxy/` and got the same 401, and they are right. **Retract the
fallback advice in the body of this memo and in step 1.** Verified here:

- `docker ps` shows no clone container — only `llm-proxy2`, its grok/cursor bridges, and
  `llm-proxy2-smoke`.
- The only nginx configs containing a `/llm-proxy/` location are pre-August `.bak-*` files.
  The live `projects-locations.d/llm-proxy2.conf` serves `/llm-proxy2/` only.
- Both paths report an identical version (5.22.25) because they reach the same instance.

So the clone was retired (team says 2026-08-17) and `/llm-proxy/` now resolves to
llm-proxy2. BUG-069's "enabled on `/llm-proxy/`" is a June observation of a deployment that
no longer exists; the separate key state it describes went with it. My error was treating a
June finding as current without checking whether the deployment still existed.

**Consequence: the key flip is the only path.** There is no working endpoint for this key
today, and the two-endpoint test in step 1 cannot distinguish anything, because there is
only one endpoint.

### Status

Blocked on the operator. One admin session covers all three checks:

1. Re-enable `llmp-2Hj` on llm-proxy2.
2. Confirm the four restriction fields on that key do not exclude Anthropic.
3. Confirm `claude-sonnet-5` and `claude-haiku-4-5-20251001` are offered by an enabled
   provider.

Do 2 and 3 in the same session as 1, or a fixed 401 just becomes a 451 or a 503. The tax
team will re-test both models within a minute of being told, and nothing needs to change or
restart on their side.

Not urgent in the way it reads: they confirmed no tax work is blocked. Without the AI, new
documents fall back to keyword labels, and the statements, cover sheets and form figures
never used it. The Oct 15 date is for restoring real classifications, not for unblocking the
pipeline.
