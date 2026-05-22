# Research: routing efficiency + cost (2026-05-22)

**Trigger:** operator asked for a research session into improvement
opportunities after the v4.4.x fix backlog was exhausted (16
releases, 0 open bugs).

**Method:** measured 24h of `activity_log` + `provider_metrics` +
`/metrics` on the live fleet (v4.4.16). No code changed during the
research. Findings ranked by actionability.

---

## TL;DR

The proxy's routing is **doing its job well** — cost-minimizing
(87% of traffic to the free subscription provider, 13% paid
fallback) and latency is acceptable (89% < 15s). The improvement
opportunities are almost all **consumer-side** (coordinator-hub
configuration), so the output is recommendations to forward, not
proxy code changes — except one ~10-LOC proxy robustness item.

The single most wasteful pattern: **911 `"hi"` health-probe
requests/day hitting the PAID gemini-2.5-pro provider.**

---

## What's healthy (no action)

### Routing is cost-optimal

24h traffic (9,365 requests, 99% from the `coordinator-hub` key —
the AI-Librarian / KB-classification workload):

| Provider | Reqs | Cost/24h | Marginal cost | Role |
|---|---|---|---|---|
| Devin-Anthropic-Max (VG+Gmail) | ~8,180 | $0.00 | subscription | primary (free) |
| C1 Vertex AI / Google AI | ~1,180 | ~$2/day | per-call | fallback when Anthropic-Max rate-limited / CB-open |
| Grok-Web | 4 | $0.00 | subscription | tertiary |

The router correctly sends the bulk to the **free** subscription
provider and only spills to **paid** Vertex when Anthropic-Max is
unavailable. That's the cascade working as designed.

### Latency is acceptable

24h latency distribution (all providers):

| Bucket | Share |
|---|---|
| < 5s | 23.2% |
| 5–15s | 65.9% |
| 15–30s | 9.7% |
| 30–60s | 1.2% (108 reqs) |
| > 60s | 0.0% (2 reqs) |

89% complete under 15s, 99% under 30s. Anthropic-Max p50 is 7.9s
(subscription, slower); Vertex/Gemini p50 is 3.0s (paid, faster).
The 110 requests > 30s pin a DB connection for that duration
(ARCH-A relevance) but at 1.2% it's not a pool-pressure risk.

---

## Findings (ranked by actionability)

### F1 — 911 `"hi"` health probes/day hit the PAID gemini-2.5-pro 🔴 wasteful

The single clearest waste. Of the hub's traffic, **911 requests/24h
(~1 every 95s) are literally the prompt `"hi"`** (2 input tokens,
17 output tokens) routed to **gemini-2.5-pro — a per-call paid
provider**. This is almost certainly a hub-side health/liveness
probe that's resolving to the paid Gemini provider instead of a
free one or the proxy's own `/health`.

- **Cost:** small in absolute terms (~$0.15/day, ~$55/yr) but it's
  100% waste — paying to say "hi".
- **Noise:** inflates the hub's apparent request volume by ~10%
  (911 of 9,278) and clutters the activity log.
- **Fix (hub-side):** point the probe at the proxy's `/health`
  (no auth, no model call, no cost), OR if it must exercise the
  model path, target a free subscription provider
  (`claude-haiku-4-5` → Anthropic-Max) instead of `gemini-2.5-pro`.
- **Boundary:** coordinator-hub is a different team's config. →
  recommendation memo for the operator to forward.

### F2 — Anthropic prompt-caching NOT active on the templated hub traffic 🟡 latency + quota

The hub's 8,329 claude-haiku classification requests share a
**fixed prompt prefix** — only **5 distinct 150-char prefixes
across 3,000 sampled requests**, the top one repeating 2,477×,
median input 3,828 tokens. This is textbook templated traffic
(stable system + instruction template, varying only the paragraph
being classified).

But **prompt caching is not active** for it: no `cache_*` fields
in `event_meta`, no `cache_tokens` Prometheus samples. The proxy's
`_inject_claude_code_system` adds a `cache_control` marker to the
*system marker* (small), but the hub's ~3,800-token repeated
content lives in the messages/instruction, which only gets cached
if the **caller** sets a `cache_control` breakpoint there.

- **Why it matters even though Anthropic-Max is $0 marginal:**
  prompt caching would (a) cut TTFT latency materially on the
  7.9s-p50 path, and (b) reduce the weekly-quota token burn on the
  Anthropic-Max subscription — directly relevant to the
  cap-rotation logic (`reference_anthropic_max_cap_empirical`).
- **Fix (hub-side):** add a `cache_control: {"type":"ephemeral"}`
  breakpoint to the stable prefix of the classification prompt
  template.
- **Boundary:** hub-side prompt construction. → recommendation memo.

### F3 — Semantic cache built but 0% adopted 🟡 latency + cost

`semantic_cache_enabled` is set on **0 of the active API keys** —
the feature (similarity-based response caching) is fully built but
unused. With 100% prefix repetition in the hub workload, if any
*full* prompts repeat (e.g. re-classifying the same paragraph after
a re-import), semantic cache would return cached results instantly
at $0.

- **Caveat:** classification of *distinct* paragraphs won't hit the
  cache (different content). The win depends on how much
  re-classification of identical content the hub does (re-imports,
  retries). Worth a trial on the hub key + measuring hit rate.
- **Fix (hub-side / operator):** flip `semantic_cache_enabled` on
  the coordinator-hub key, watch `llm_proxy_cache_lookups_total`
  hit rate for a day, keep if hit rate justifies it.
- **Boundary:** operator decision (it's their key). → recommendation.

### F4 — Cluster heartbeat `resp.json()` lacks status/content-type guard 🟢 proxy-side, ~10 LOC

Surfaced by the v4.4.16 log fix: `app/cluster/manager.py` heartbeat
does `data = resp.json()` without first checking `resp.status_code`
or content-type. When a peer returns a non-JSON body (e.g. nginx
502 during that peer's deploy), the heartbeat logs the peer as
"unreachable" with a `JSONDecodeError` rather than distinguishing
"peer returned non-JSON, probably restarting" from "peer truly
down (connection refused / timeout)."

- **Impact:** cosmetic today (deploy-window-only), but the
  distinction matters for alerting accuracy — a peer mid-deploy
  shouldn't read identically to a peer that's actually down.
- **Fix (proxy-side):** check `resp.status_code == 200` + guard the
  `.json()` in a try/except that classifies non-JSON as
  "degraded/restarting" vs the connection-level exceptions as
  "unreachable". ~10 LOC + a test.
- **This is the only finding I can action without crossing the
  hub-team boundary.**

### F5 — gemini-2.5-pro vs flash cost attribution discrepancy 🔵 observation

`provider_metrics`-level cost ($1.92/day for C1 Vertex) doesn't
cleanly reconcile with per-model `event_meta.cost_usd` sums
($0.16 pro + $0.22 flash = $0.38). Likely a cost-attribution path
difference (per-provider rollup vs per-event field) rather than a
real overcharge — the absolute numbers are small (~$2/day max).
Flagged for a future cost-accounting audit if the spend ever grows;
not worth chasing at $2/day.

---

## Recommended actions

| # | Action | Owner | Effort | Value |
|---|---|---|---|---|
| F1 | Repoint the `"hi"` health probe off paid gemini-2.5-pro → `/health` or a free provider | hub team (memo) | trivial | stops 911 wasted paid reqs/day |
| F2 | Add `cache_control` breakpoint to the hub's classification prompt template | hub team (memo) | small | TTFT + subscription-quota headroom |
| F3 | Trial `semantic_cache_enabled` on the coordinator-hub key | operator | 1 flag + 1 day watch | latency/cost if re-classification rate is non-trivial |
| F4 | Harden the cluster heartbeat's `resp.json()` (distinguish non-JSON from unreachable) | **proxy (me)** | ~10 LOC + test | alerting accuracy |
| F5 | (defer) cost-attribution reconciliation audit | proxy | ~1h | only if Vertex spend grows |

**Proxy-side, I can ship F4 now.** F1–F3 are consumer/operator-side
— drafting a memo for the operator to forward to the hub team
(per the cross-team boundary rule).

---

## Data appendix (24h, fleet v4.4.16)

- Total requests: 9,365 (coordinator-hub: 9,278 / 99%)
- Model mix (hub): claude-haiku-4-5 ×8,329, gemini-2.5-pro ×910
  (911 of which are `"hi"` probes), gemini-2.5-flash ×34
- Prompt-prefix distinctness: 5 prefixes / 3,000 sampled (100%
  repeat)
- Latency p50 by provider: Anthropic-Max 7.5–7.9s, Vertex 3.0s
- Semantic-cache-enabled keys: 0
- Anthropic prompt-cache active on hub traffic: no
