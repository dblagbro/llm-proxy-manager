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
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://www.voipguru.org/llm-proxy/v1/messages \
  -H "x-api-key: $LLM_PROXY2_KEY" \
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
