To: All consumer-project teams (DevinGPT, paperless-ai-analyzer, tax-ai-analyzer, rebooter-droids, transcriber, coordinator-hub, anomaly-detector, email-collector, ai-form-filler, AIRI, others)
From: llm-proxy team
Date: 2026-06-05
Re: AI provider catalog SPLIT — `/llm-proxy2/` vs `/llm-proxy/` — pick your posture, update your config

## TL;DR

As of 2026-06-05 the proxy serves **two independent fleets** with
different provider catalogs. You need to decide which one your
project routes through, and update your base URL accordingly.

- **`/llm-proxy2/`** — compliance-locked. **No Anthropic providers
  available.** Three serving providers (Google Vertex, Cursor-oAuth,
  Google Generative). Use if your project has compliance / Anthropic-
  block requirements (e.g. the coordinator-hub gov-compliance flow).

- **`/llm-proxy/`** — full options. **All 11 providers** still
  available: Anthropic-Max (Gmail + VG), Cohere, Devin-Codex,
  Devin-Personal-OpenAI, Google × 2, Grok-Web, OpenRouter,
  Cursor-oAuth. Use if your project just needs working AI and has no
  Anthropic restriction.

If you don't actively need the compliance posture, **point at
`/llm-proxy/`** — it has the broader catalog and the routing layer
will pick what's healthiest.

## What changed and why

- Backstory: v5.0.x compliance enforcement (2026-06-03/04) shipped
  per-key `blocked_companies` policy + model substitution + audit
  chain on the existing `/llm-proxy2/` cluster. That was the
  belt — software enforcement that substitutes a compliant provider
  when a banned company is requested.
- 2026-06-05 morning: operator added the **suspenders** — physically
  removed Anthropic and other restricted providers from
  `/llm-proxy2/`'s catalog, so there's nothing for the policy to
  even substitute *toward* Anthropic. Four providers remain.
- Same day: snapshot-and-forked a separate **`/llm-proxy/`** cluster
  on tmrwww01 + tmrwww02 (NOT on c1conv) with the full 11-provider
  catalog, for projects that don't need the compliance posture.
- Both clusters run the same code (v5.0.19 as of this memo), same
  uptime guarantees, same UI. They're independent: changes you make
  on one don't propagate to the other.

## Per-project action items

### DevinGPT (most critical — operator-flagged)

DevinGPT's AI calls go to whichever proxy URL is in
`DEVINGPT_LLM_PROXY_URL` (or equivalent) in its compose env. If it's
currently pointing at `https://www.voipguru.org/llm-proxy2`, the
behavior change you'll see is:

- Requests for Claude / Anthropic models will be **substituted to a
  Google-family provider** (Gemini 2.5 Flash via Vertex) with
  `X-Compliance-*` disclosure headers. NOT refused, just rerouted.
  If your code reads the `model` field of the response and expects
  it to match the requested model, that no longer holds.
- The Devin-Anthropic-Max-Gmail / Devin-Anthropic-Max-VG providers
  are GONE from the routing pool. Any code that hardcoded provider
  IDs to either of those needs updating.

**Recommended for DevinGPT:** flip the proxy base URL to
`https://www.voipguru.org/llm-proxy` (no `2`). That preserves the
full provider catalog including Anthropic, and the existing model
selection and conversation-memory logic continues to work unchanged.
DevinGPT's persona / conversation features were designed against the
Anthropic+Codex catalog; the compliance posture would degrade them
unnecessarily.

If you absolutely need DevinGPT on the compliance cluster (rare —
you'd only want this for a regulated demo), set up the per-key
`blocked_companies` policy on the DevinGPT API key first so you
know which calls get substituted.

### Other consumer projects

For each project, identify the env var or config field that holds
the proxy base URL (typically named `LLM_PROXY_URL`,
`LLMPROXY_BASE`, or similar; some pass it as `OPENAI_API_BASE` or
`ANTHROPIC_API_URL`). Decide:

| Project | Current likely posture | Suggested URL |
|---|---|---|
| **paperless-ai-analyzer** | Document classification — needs Anthropic for nuanced parsing | `/llm-proxy/` (full) |
| **tax-ai-analyzer** | Same Anthropic-heavy workflow | `/llm-proxy/` (full) |
| **rebooter-droids** | ESP8266 firmware decisions — cheap models | `/llm-proxy2/` ok (Gemini-flash is fine) |
| **transcriber** | LLM passes for diarization naming | `/llm-proxy/` (richer catalog) |
| **coordinator-hub** | Already on the compliance flow (`coordinator-code-prod-hub-v2` key has `blocked_companies=["anthropic"]`) | **stay on `/llm-proxy2/`** |
| **anomaly-detector** | Background sweeps — cost-sensitive | either; `/llm-proxy/` if you want Devin-Cohere |
| **email-collector** | Operator-side email retry triage | either |
| **ai-form-filler** | Form auto-fill via LLM | `/llm-proxy/` (needs reasoning) |
| **AIRI** | Voice + reasoning over the proxy | `/llm-proxy/` (full) |

If a project doesn't appear above and you're unsure: default to
`/llm-proxy/` and switch only if compliance asks.

## Mechanics of the switch

### URL pattern

- Current `/llm-proxy2/` → `https://www.voipguru.org/llm-proxy2`
  (or `www2.` / `c1conv` peer URLs)
- New `/llm-proxy/` → `https://www.voipguru.org/llm-proxy` (or
  `www2.` peer URL — clone is NOT on c1conv, so don't peer there)

Both serve the same OpenAI- and Anthropic-compatible API surfaces
under `/v1/messages` + `/v1/chat/completions` + `/v1/responses`.
No protocol change.

### API keys

API keys are **NOT cluster-synced between the two fleets** (different
DBs by design). If you have an API key that works against
`/llm-proxy2/`, it does NOT exist on `/llm-proxy/` until someone
provisions it there. Process:

1. Visit `/llm-proxy/keys` (the UI on the clone), log in (separate
   admin session — the cookie is `llmproxy_clone_session`, not
   `llmproxy_session`).
2. Either: create a new key for your project, or have the operator
   restore the old prefix/value from the original cluster.
3. Update your project's config with the new key.

The compliance fields (`blocked_companies`, `allowed_paths`,
`debug_echo_enabled`) are also per-key and per-cluster — you decide
the posture independently on each cluster.

### Smoke test after the switch

After updating your base URL + API key, run one chat completion
against each:

```bash
curl -X POST https://www.voipguru.org/llm-proxy/v1/chat/completions \
  -H "Authorization: Bearer <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"ping"}]}'
```

If you get `{"choices":[...]}` you're routed. If you get an
`X-Compliance-Substitution: true` response header, you're on the
compliance cluster and the model got substituted (look at
`X-Compliance-Served-Model` for what actually ran). If you get 401
your key isn't provisioned on that fleet yet.

### Timeline

- **Today (2026-06-05)**: both URLs live and stable. No deadline to
  switch — your current `/llm-proxy2/` traffic keeps working with
  the compliance substitutions firing.
- **DevinGPT specifically**: operator wants this flipped to
  `/llm-proxy/` ASAP since the compliance substitution behavior
  degrades the conversation-memory / persona logic.
- **Anyone else who hasn't decided in ~7 days**: assumed to be ok
  on `/llm-proxy2/` with compliance substitutions. We'll re-ping
  if your project starts triggering more than 20 substitution
  events per hour (signal that you should probably move).

## Open questions / how to ask

- Not sure which posture fits your project? Tell the operator your
  use case (especially whether you have model-specific code paths)
  and we'll recommend.
- Need provider-specific behavior (e.g. only Anthropic, only
  Gemini)? Set per-key `blocked_companies` on whichever cluster you
  end up on, rather than picking a cluster purely for that reason.

— llm-proxy team
