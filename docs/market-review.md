# Market, licensing and viability review — llm-proxy-v2

**Review date:** 2026-09-26 · **Reviewer:** automated review (agent), commissioned by the operator
**Project version at review:** v5.22.22 · **Next scheduled review:** 2027-01-15 (see triggers)
**First review** — no prior `market-review.md` existed.

> Scope limits observed: no product code changed, no repository archived, no licensing
> changed, no third party contacted during this review.

---

## Recommendation: **NARROW**

Keep the gateway. **Stop building the consumer-subscription OAuth capability**, which is
4 of 7 live providers and ~4,400 lines (6%) of `app/`, because the two largest vendors
prohibit it in writing and at least one enforces it at the API layer. Re-base the project
on metered API keys plus the compliance/audit layer, which is where its remaining
defensible value sits.

This is not a recommendation to stop. The gateway works, has real internal consumers, and
its dependency hygiene is better than the market leader's. It is a recommendation to cut a
capability whose foundation has been removed by the vendors it depends on.

---

## 1. The finding that drives this review

The project's most distinctive capability is routing traffic through **consumer
subscription sessions** rather than metered API keys — `claude-oauth`,
`ChatGPT-oauth-plan`, `grok-web`, and `cursor-oauth` provider types, with
`oauth_refresh_token`, `anthropic_session_cookies` and `codex_session_cookies` persisted in
the providers table, plus five documented capture runbooks in `docs/`.

Anthropic's Claude Code legal and compliance page states directly
([code.claude.com/docs/en/legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance),
retrieved 2026-09-26):

> "**OAuth authentication** is intended exclusively for purchasers of Claude Free, Pro,
> Max, Team, and Enterprise subscription plans and is designed to support ordinary use of
> Claude Code and other native Anthropic applications."

> "Anthropic does not permit third-party developers to offer Claude.ai login into their own
> applications, **or to route requests through Free, Pro, or Max plan credentials on behalf
> of their users**. Moreover, **developers may not collect, store, or intermediate
> Claude.ai credentials or session tokens** — sign-in to a Claude account must complete
> through Anthropic's own flow."

> "Anthropic reserves the right to take measures to enforce these restrictions and may do
> so without prior notice."

The project does the three specific things that sentence prohibits: it collects, stores,
and intermediates subscription session credentials. There is a carve-out in the same
document, but it is scoped to **API keys** ("how customers provision and manage their own
API keys … provided the resulting usage is billed to the key owner … and is not resold or
intermediated"). It does not extend to subscription OAuth, and the prohibition on
collecting or storing session tokens carries no carve-out at all.

Reporting indicates enforcement began in **January 2026**, with consumer OAuth tokens
blocked for third-party tools including OpenClaw, OpenCode, Roo Code and Goose, and
subscription tokens rejected at the API layer when used outside Claude Code
([autonomee.ai](https://autonomee.ai/blog/claude-code-terms-of-service-explained/),
[openclawlaunch.com](https://openclawlaunch.com/guides/openclaw-claude-subscription)).

OpenAI maintains the same boundary structurally rather than in equally explicit prose:
API usage is billed separately from ChatGPT subscriptions, and "API access does not include
a ChatGPT subscription and vice versa"
([openai.com/api/pricing](https://openai.com/api/pricing/)); sharing account credentials is
prohibited under the Terms of Use
([help.openai.com](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus)).

### This is already visible in our own production telemetry

The review did not have to speculate about whether enforcement affects us. Evidence
gathered from the live fleet in September 2026:

| Observation | Consistent with |
|---|---|
| `Devin-Codex-Gmail` (`ChatGPT-oauth-plan`): 4,457 requests, **1 success**, over 24 days | plan token has no API scopes |
| Direct call with that provider's credentials → `401 "insufficient permissions … Missing scopes: model.request"` | subscription ≠ API entitlement, by design |
| Operator reauth on 2026-09-06 changed nothing | not a credential problem |
| Both `claude-oauth` providers: repeated auth failures, expired access tokens with *valid* refresh tokens | refresh path rejected upstream |

**The most important conclusion in this review is interpretive:** a substantial amount of
recent engineering (v5.22.19 auth-error classification, v5.22.20 skip persistence, v5.22.21
cross-family dispatch, v5.22.22 failover model compatibility) was spent making the system
degrade gracefully around these failures. Those were all real bugs and the fixes stand on
their own merits. But the underlying provider failures they were compensating for are
**not** bugs we can fix — they are vendors enforcing a boundary. Continuing to invest here
is building on ground that is being deliberately removed.

---

## 2. Competitive landscape

### Strongest open-source alternative: LiteLLM (also our dependency)

| Metric | Value (2026-09-26, GitHub API) |
|---|---|
| Stars / forks | 59,672 / 11,801 |
| Open issues | 5,316 |
| Last push | 2026-09-26 (same day) |
| Recent releases | `v1.104.0-dev.2`, `v1.100.3`, `v1.99.4` — all 2026-09-25 |
| License | **Split**: `enterprise/` under a separate license, remainder MIT |

Very high velocity; multiple concurrent release lines in a single day. The 5,316 open
issues alongside that cadence suggests throughput is prioritised over backlog closure.

**Licensing note:** GitHub reports `NOASSERTION` because `LICENSE` is a split file —
`enterprise/` is proprietary, everything else MIT. We consume litellm as a **library**
(the MIT portion), so there is no current licensing conflict. The open-core boundary is a
watch item, not a present problem: if routing primitives we rely on move behind
`enterprise/`, the pin strategy becomes a fork decision.

**Security history — and a point in our favour.** LiteLLM had a materially bad 2026:
CVE-2026-35029 (proxy RCE via unauthenticated `/config/update`, fixed 1.83.0);
CVE-2026-42208 (**unauthenticated** SQL injection reachable from `POST /chat/completions`,
1.81.16–1.83.6, fixed 1.83.7); CVE-2026-42203 (SSTI in `/prompts/test`); and a **PyPI
supply-chain compromise** — `litellm==1.82.7` and `1.82.8` were live for ~40 minutes on
2026-03-24 before quarantine
([docs.litellm.ai](https://docs.litellm.ai/blog/security-update-march-2026),
[docs.litellm.ai](https://docs.litellm.ai/blog/cve-2026-42208-litellm-proxy-sql-injection),
[sentinelone.com](https://www.sentinelone.com/vulnerability-database/cve-2026-35029/)).

**Our exposure is low and it is documented, dated triage rather than luck.**
`requirements.txt` pins `litellm>=1.85.2,<1.87.0` with inline reasoning: the CVEs are
LiteLLM **Proxy-server** issues, we use the library surface only, the floor was raised in
v4.4.30 specifically to clear three GHSAs, and the pin sits above both the compromised
1.82.x builds and the ≤1.83.6 SQLi range. That practice is genuinely better than most
one-maintainer projects and better than several commercial gateways' public posture.

### Portkey Gateway (OSS) — maintenance risk

| Metric | Value (2026-09-26) |
|---|---|
| Stars | 13,087 · License MIT |
| Last push | 2026-05-25 — **124 days ago** |
| Last release | `v1.15.2`, 2026-01-12 — **257 days ago** |

The open-source gateway looks de-prioritised in favour of the hosted control plane;
on-premise is an enterprise option as of April 2026
([portkey.ai](https://portkey.ai/docs/product/guardrails)). Anyone choosing Portkey OSS for
self-hosting today is choosing a component its vendor is not actively shipping.

### Others

- **Kong AI Gateway** — LLM plugins on a mature API-management platform; strongest where
  Kong is already deployed ([konghq.com](https://konghq.com/blog/engineering/ai-gateway-benchmark-kong-ai-gateway-portkey-litellm)).
- **Commercial / hosted** — OpenRouter, Vercel AI Gateway, TrueFoundry, Requesty, Bifrost.
  All are hosted-first; none satisfy a self-hosted, inspectable requirement without an
  enterprise contract ([truefoundry.com](https://www.truefoundry.com/blog/llm-gateway-on-premise-infrastructure)).

### Feature overlap — honest assessment

| Capability | Ours | Market |
|---|---|---|
| Multi-provider routing, failover, retries | ✅ | **Commodity.** Every alternative. |
| OpenAI/Anthropic wire translation | ✅ | Commodity (litellm does it) |
| Budgets, virtual keys, rate limits | ✅ | Commodity — in LiteLLM's **MIT** core |
| Observability dashboards | partial | Alternatives are ahead |
| Compliance enforcement + audit chain | ✅ | **Contested.** LiteLLM Enterprise, Portkey, Kong, TrueFoundry all target it |
| Subscription-OAuth session reuse | ✅ | **Nobody credible does this — because it is prohibited** |
| Small-cluster HMAC config sync (LWW) | ✅ | Unusual; niche value |
| "Readable in an afternoon" | ✅ | **Genuinely rare** and increasingly valuable post-CVE |

**Missing differentiation is the core problem.** Strip the subscription-OAuth capability and
what remains is a competent gateway whose every remaining feature is available in an
MIT-licensed project with 59,672 stars and same-day releases. The compliance layer is only
1,617 lines (2% of `app/`) and competes with funded commercial roadmaps — with EU AI Act
high-risk obligations enforceable from **August 2026** raising the tide for everyone
([braintrust.dev](https://www.braintrust.dev/articles/best-ai-governance-platforms-llm-applications-2026)).

---

## 3. Project viability

**Contributor concentration: 1.** Every commit in the past year is from a single author
(`git log --since='1 year ago' --pretty='%an' | sort -u`). Bus factor 1. 53 commits in the
last 90 days (Jul 5 / Aug 36 / Sep 12), 357 tags — real, sustained activity, entirely
dependent on one person.

**Demand evidence is real but small and internal.** Named consumers: DevinGPT
(`LLM_PROXY_URL` set, no direct provider keys — genuinely dependent), coordinator-hub
(369,563 requests, $164.56 lifetime — the only substantial consumer), flowise, camreview,
translation-service, tax-ai-analyzer. No evidence of external adoption of the public Docker
image was found in this review. Total spend across all keys is ~$20/month — this is a
small-scale deployment, which matters for cost/benefit.

**Maintenance cost is visible and non-trivial.** 57 pre-existing unit-test failures gate CI
from covering the full suite; `_messages_streaming.py` is 741 lines against its own 700-line
guard (already a `known_failures` entry); September alone required five releases
(v5.22.18–v5.22.22) to chase provider failure modes, four of which trace back to
subscription-OAuth providers.

**Reliability has genuinely improved.** The `_next_route` wedge fix has held ~6 weeks; the
cluster-sync body-read race went from 185 failures/day to 0; `/v1/messages` went from 32%
errors to 0. The engineering is sound. That is why the recommendation is NARROW and not
ARCHIVE.

---

## 4. Why not the alternatives

| Option | Why not |
|---|---|
| **CONTINUE** | Would keep investing in a capability two vendors prohibit and one enforces against. Four of seven providers rest on it. |
| **INTEGRATE** (adopt LiteLLM Proxy) | Tempting on features, but it means inheriting the proxy attack surface we currently avoid by design — the RCE, SQLi and SSTI CVEs are all *proxy-server* issues. Would trade our best property for commodity features. Reconsider if the compliance layer is retired. |
| **FORK** | Nothing to fork from; we already consume litellm as a library on good terms. |
| **PIVOT** | Too destructive. The core gateway works and has dependent consumers. |
| **ARCHIVE** | Real internal dependents (DevinGPT has no fallback path), a working compliance layer, and better dependency hygiene than the leader. Archiving would strand consumers to solve a problem that cutting one capability solves. |

---

## 5. Recommended actions

### Stop building
1. **All consumer-subscription OAuth providers** — `claude-oauth`, `ChatGPT-oauth-plan`,
   `grok-web`. No new capture flows, no new refresh machinery, no further failure-mode
   engineering. (`cursor-oauth` needs a separate read of Cursor's terms — not assessed here.)
2. **Session-credential persistence** — `anthropic_session_cookies`,
   `codex_session_cookies`, and subscription `oauth_refresh_token` storage. Anthropic's
   prohibition on collecting or storing session tokens is explicit and unqualified.
3. **Observability dashboards** — alternatives are far ahead; not a winnable axis.

### Keep building
1. **Compliance enforcement + audit chain** — the strongest remaining differentiator.
   Narrow it to what an auditor signs off on: the hash-chained audit trail, per-key and
   system-wide company/model policy, disclosure headers. EU AI Act timing favours this.
2. **Legibility as a product property** — "readable in an afternoon" is a real answer to
   the question LiteLLM's 2026 CVE record raises. Worth saying out loud in the README.
3. **Wire-format translation correctness** — well-tested, load-bearing for consumers.
4. **Dependency-triage discipline** — the dated CVE reasoning in `requirements.txt` is a
   genuine asset. Formalise it as a release gate.

### Immediate follow-ups (not code changes — decisions for the operator)
- **Get a ruling on the public Docker image.** `dblagbro/llm-proxy-manager` ships
  subscription-OAuth capture tooling publicly. Distributing it is materially different from
  personal use and is the highest-risk item found. Anthropic's page directs licensing
  questions to their sales contact.
- **Re-attribute the four September releases.** They are good fixes, but the bug-log should
  record that the *provider* failures were vendor enforcement, not defects, so future
  readers do not re-chase them.
- **Plan DevinGPT's migration** to metered API keys before, not after, the remaining
  subscription providers stop working.

### Roadmap changes
- **M1** — unchanged; finish the 72 h soak.
- **M2** — **raise to top priority.** 57 failures → 0 and full-suite CI gating is now the
  highest-value work: it is the foundation for everything kept and it is entirely under our
  control, unlike provider behaviour.
- **M3** — **re-scope.** Remove subscription-OAuth items. Replace with: compliance-layer
  hardening, `_messages_streaming.py` split back under its 700-line guard, and API-key
  migration for dependent consumers.
- **M4** — unchanged; the security review of compliance/auth/cluster surfaces is well aimed.
- **New M5 — provider-portfolio correction.** Move all live traffic to metered API keys or
  a provider-sanctioned path. Success: zero providers depending on subscription session
  credentials.

---

## 6. Risks

### Risks of continuing as-is
- **Account action without notice.** Anthropic reserves the right to enforce "without prior
  notice". The exposed accounts are the operator's own Max subscriptions.
- **Distribution risk** — the public image packages the prohibited pattern.
- **Wasted engineering** — September's pattern (five releases chasing provider failures)
  repeats indefinitely, because the root cause is not ours to fix.
- **Silent cost migration** — as subscription providers fail, traffic falls back to metered
  keys. Already observed: a flat-rate plan's traffic moved onto a metered OpenAI key, and
  the proxy's own accounting under-reported it by ~37% until v5.22.20.
- **Bus factor 1** compounds all of the above.

### Risks of narrowing
- **Capability loss is real.** Subscription reuse was the genuine cost advantage. Removing
  it converts a flat subscription cost into metered spend. At current volume (~$20/month)
  that is affordable; at 10× it needs a budget decision.
- **Reduced differentiation** — what remains competes directly with LiteLLM. The honest
  answer is that a small, legible, self-hosted gateway with a real audit chain is a
  legitimate niche, but it is a *niche*, not a moat.
- **Consumer disruption** — DevinGPT and coordinator-hub need migration windows.
- **Sunk-cost discomfort** — ~4,400 lines and five runbooks are retired. They were good
  engineering against a foundation that has since been withdrawn.

---

## 7. Next review

**Scheduled: 2027-01-15.** Bring forward on any of:
- Anthropic, OpenAI or xAI changes subscription-credential terms in either direction
- LiteLLM moves routing primitives we depend on behind `enterprise/`
- A second maintainer joins (changes the bus-factor calculus materially)
- External adoption of the public image appears
- Monthly metered spend exceeds $200 (changes the cost/benefit of subscription reuse)
- EU AI Act enforcement produces concrete audit requirements the compliance layer must meet

---

## Sources

All retrieved 2026-09-26.

- [Anthropic — Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) — authoritative OAuth restriction
- [LiteLLM — Suspected supply chain incident (Mar 2026)](https://docs.litellm.ai/blog/security-update-march-2026)
- [LiteLLM — CVE-2026-42208 SQL injection](https://docs.litellm.ai/blog/cve-2026-42208-litellm-proxy-sql-injection)
- [LiteLLM — Security hardening, April 2026](https://docs.litellm.ai/blog/security-hardening-april-2026)
- [SentinelOne — CVE-2026-35029 (proxy RCE)](https://www.sentinelone.com/vulnerability-database/cve-2026-35029/)
- [LiteLLM LICENSE (split MIT / enterprise)](https://raw.githubusercontent.com/BerriAI/litellm/main/LICENSE)
- [Portkey Gateway — guardrails / on-prem](https://portkey.ai/docs/product/guardrails)
- [Kong — AI Gateway benchmark](https://konghq.com/blog/engineering/ai-gateway-benchmark-kong-ai-gateway-portkey-litellm)
- [TrueFoundry — on-premise LLM gateway](https://www.truefoundry.com/blog/llm-gateway-on-premise-infrastructure)
- [Braintrust — AI governance platforms 2026 (EU AI Act timing)](https://www.braintrust.dev/articles/best-ai-governance-platforms-llm-applications-2026)
- [OpenAI — API pricing (API separate from subscriptions)](https://openai.com/api/pricing/)
- [OpenAI — What is ChatGPT Plus (credential sharing)](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus)
- [Claude Code ToS explained (Jan 2026 enforcement)](https://autonomee.ai/blog/claude-code-terms-of-service-explained/)
- [OpenClaw — why subscription auth is not offered](https://openclawlaunch.com/guides/openclaw-claude-subscription)
- [OpenZiti — open-source LLM gateways compared](https://blog.openziti.io/comparing-open-source-llm-gateways)
- GitHub API: `BerriAI/litellm`, `Portkey-AI/gateway` (metrics table above)
