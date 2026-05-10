# RFC: AI-driven Provider Supervisor (proposed v3.8.0)

**Status**: DRAFT — design only, no implementation. Pending operator scope decision (task #241).
**Author**: llm-proxy2 team
**Created**: 2026-05-10
**Companion**: [rate-limit-llm-classifier already shipped in v3.7.10](../ai-rate-limiter-spec.md) — this RFC mirrors that pattern on the provider side.

## Background

The operator's 2026-05-10 Q5 directive ("we need an AI built into the proxy that itself reviews this and proactively makes suggestions") was implemented as the v3.7.10 **API-key + IP rate limiter** — the CALLER side of proxy operations.

The PROVIDER side is still entirely rule-based:
- `auto_skip_until` (v3.7.1) — set when Anthropic billing scrape says provider at 100%
- `auto_skip_until` (v3.7.16) — set when 3+ auth failures within 30 min
- `reorder_claude_oauth_by_utilization` (v3.7.4) — priority bucket reorder
- Circuit breaker (existing) — opens on failure threshold, recovers on success

These rules catch obvious cases. They miss subtle, holistic, drift-style problems that an AI would notice. **The provider-side supervisor closes the symmetry gap.**

## Problems the supervisor should catch

Examples derived from real 2026-05-10 monitoring cycles + operator-known pain points:

| # | Pattern | Why rules miss it |
|---|---------|-------------------|
| 1 | Provider failing auth for 70+ min → deprioritize | Now covered by v3.7.16; was the case study that motivated this RFC |
| 2 | Provider high latency but still being picked because router only sees success/fail | Rules look at boolean success, not latency drift |
| 3 | Expensive provider winning over cheaper-and-healthy alternates | Router scores by capability + priority + utilization, doesn't second-guess against cost-efficiency objectives |
| 4 | Probe back-off cool-offs lasting longer than baseline | No baseline drift detector |
| 5 | OAuth refresh failures preceding token expiry | Rules act on the eventual fail, not the predictive signal |
| 6 | Model-level cost regressions (same prompt class costing more than 7d trailing avg) | No per-prompt-class cost trend analysis |
| 7 | Provider with consistent 5xx pattern but below CB threshold | CB threshold is binary; AI can spot "trending toward unhealthy" |
| 8 | Rotation thrash between same-tier providers (priority hopping every minute) | Each individual rotation is rule-justified; the AGGREGATE pattern indicates instability |
| 9 | Provider returning correct responses but with subtly worse quality (longer time-to-first-token, less concise) | No quality metric in current pipeline |
| 10 | Sudden zero-traffic on a provider that should be healthy | Absence-of-signal — rules don't trigger when nothing happens |

## Design

### Architecture (mirror of v3.7.10 ai_rate_limiter.py)

```
app/monitoring/
├── ai_rate_limiter.py         (existing v3.7.10) — caller side
└── ai_provider_supervisor.py  (NEW v3.8.0)        — provider side
```

```
app/api/
├── ai_rate_limiter.py            (existing)
└── ai_provider_supervisor.py     (NEW)            — GET reviews, POST apply/dismiss/revert
```

```
app/models/db.py
└── ProviderAiReview              (NEW table) — analogous to ApiKeyAiReview
```

### Lifecycle

1. **Background worker** every `ai_provider_supervisor_interval_sec` (default `1800` = 30 min, configurable). Slower than rate-limiter (300s) because provider behavior drifts on longer timescales and we want fewer review rows.

2. **Per-provider stats compute**:
   - 30-min and 7-day windowed: request count, success rate, p50/p95 latency, error class distribution, cost per request
   - Trend deltas: latency p95 vs 7d trailing (>30% increase = signal), success_rate vs 7d trailing, cost-per-rq vs 7d trailing
   - Routing share: what % of eligible requests in window did this provider win? Trend vs 7d
   - Probe state: consecutive failures, current back-off, last auth state
   - Provider age: was it created/edited recently? (Operator changes can mask other signals)

3. **LLM classification** with structured JSON:
   ```json
   {
     "verdict": "normal|watch|deprioritize|disable|investigate",
     "reasoning": "<2-3 sentences>",
     "suggested_priority_delta": -3 to +3 (optional),
     "suggested_auto_skip_hours": 0-168 (optional)
   }
   ```

4. **Write `ProviderAiReview` row** (analogous to `ApiKeyAiReview`):
   - Idempotent by (provider_id, captured_at)
   - Cluster-syncs via the same path BUG-016 fixed for `api_key_ai_review`

5. **Operator review flow**:
   - Suggest-only by default (`ai_provider_supervisor_auto_apply=False`)
   - Auto-apply opt-in per node: `verdict ∈ {deprioritize, disable}` → mutate `Provider.priority` or `Provider.auto_skip_until`, record `prior_priority` for revert
   - All actions cluster-synced via existing Provider sync path

### Recursion guard (lessons from BUG-017)

The supervisor calls `/v1/messages` internally for classification. **Must** use the same `X-Internal-Source: ai_provider_supervisor` pattern shipped in v3.7.15 (BUG-017 fix), and the rate-limiter sample filter must also exclude `internal_source IN ('ai_rate_limiter', 'ai_provider_supervisor')` to prevent cross-amplification.

### Why this isn't just more rules

Rules optimize for **known** failure modes. The AI catches **drift** — patterns that look normal in any one window but trend wrong over time. Specifically:
- Cost-efficiency drift: rules can't enforce "prefer cheaper provider unless there's a reason not to" because the router already sees scoring inputs. The supervisor reasons across multiple cycles.
- Quality-trend drift: rules can't represent "responses are technically successful but getting longer / less coherent."
- Behavior change: rules can't represent "this provider used to win 40% of requests, now it wins 5% — what changed?"

### Scope vs LMRHv2 Phase 3

LMRHv2 Phase 3 (bidirectional metrics → caller) is **caller-facing observability** — helping clients craft better hints. The supervisor is **operator-facing automation** — managing the provider fleet.

They share infrastructure (provider_metrics, activity_log) but serve different consumers. Bundling them risks scope creep. **Recommend keeping them separate** even if shipped close in time.

## Settings (proposed)

```python
# app/config.py
ai_provider_supervisor_enabled: bool = False           # opt-in
ai_provider_supervisor_auto_apply: bool = False        # suggest-only by default
ai_provider_supervisor_interval_sec: int = 1800        # 30 min
ai_provider_supervisor_window_min: int = 30            # short window
ai_provider_supervisor_trend_window_days: int = 7      # long window for delta calc
ai_provider_supervisor_model: str = "claude-haiku-4-5-20251001"
ai_provider_supervisor_internal_api_key: str = ""      # operator-set; same pattern as rate limiter
ai_provider_supervisor_max_priority_delta: int = 2     # cap on auto-action priority change
ai_provider_supervisor_max_auto_skip_hours: int = 24   # cap on auto-action skip duration
```

## Tables

```python
class ProviderAiReview(Base):
    __tablename__ = "provider_ai_review"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String, ForeignKey("providers.id"), nullable=False, index=True)
    captured_at = Column(DateTime, server_default=func.now(), index=True)
    llm_model = Column(String, nullable=True)
    llm_verdict = Column(String, nullable=False)   # normal|watch|deprioritize|disable|investigate
    llm_reasoning = Column(Text, nullable=True)
    suggested_priority_delta = Column(Integer, nullable=True)
    suggested_auto_skip_hours = Column(Integer, nullable=True)
    stats_summary = Column(JSON, nullable=True)
    # Lifecycle: applied / dismissed / reverted (same as ApiKeyAiReview)
    applied_at = Column(DateTime, nullable=True)
    applied_action = Column(String, nullable=True)
    prior_priority = Column(Integer, nullable=True)    # for revert
    prior_auto_skip_until = Column(DateTime, nullable=True)
    reverted_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
```

Migration: idempotent `CREATE TABLE IF NOT EXISTS provider_ai_review` in `app/models/database.py:_run_migrations`. Cluster-sync: add to allowlist following the BUG-016 pattern.

## API surface

```
GET    /api/admin/provider-supervisor/reviews         — list recent reviews (paginated)
GET    /api/admin/provider-supervisor/reviews/{id}    — single review with stats
POST   /api/admin/provider-supervisor/reviews/{id}/apply    — operator-driven apply
POST   /api/admin/provider-supervisor/reviews/{id}/dismiss  — ignore this review
POST   /api/admin/provider-supervisor/reviews/{id}/revert   — undo a previously applied action
POST   /api/admin/provider-supervisor/scan-now              — manually trigger a sweep
```

All `require_admin`. Cluster-replicated via the v3.7.15 BUG-016 sync extension.

## UI surface (proposed; can defer)

New page: **Provider Supervisor** under the existing Monitoring section. Shows:
- Last 50 reviews (table, sortable by captured_at / verdict / provider)
- Per-row apply/dismiss/revert action buttons
- Aggregate dashboard: verdicts in last 24h grouped by provider
- Status banner if `ai_provider_supervisor_enabled=False` ("opt in via env var")

Mirror of the existing AI Rate Limiter page.

## Open questions

For operator decision before scoping:

1. **Ship as v3.8.0 (new major surface)?** Or bundle with LMRHv2 Phase 3 (v3.8.0 still)? Or standalone subsystem?
2. **Default model**: Haiku 4.5 (cheap, fast) vs Sonnet 4.6 (better at nuanced reasoning)? Reviews are infrequent (one per 30 min × ~10 providers = 480/day) so cost not a hard constraint.
3. **Auto-apply scope**: should auto-apply EVER mutate `Provider.enabled = False`? Or only ever soft-skip via `auto_skip_until`? More conservative = soft-skip only.
4. **Revert window**: how long after auto-apply can operator revert? Same lifecycle as rate-limiter (no time limit, lifecycle is monotone).
5. **Trend window**: 7 days vs 14 days for the trailing baseline? 7 is enough to catch drift but might miss weekly cycles.
6. **Quality metrics**: should we add time-to-first-token + response-length-trend instrumentation now, or defer to a Phase 2 of this work?
7. **First-class verdicts**: are the 5 verdicts (normal/watch/deprio/disable/investigate) the right granularity? Operator may prefer fewer or more.

## Effort estimate

| Phase | Scope | Estimate |
|---|---|---|
| 1 | Schema + migration + cluster sync entry | 1 ship (~1h) |
| 2 | Worker + stats compute + LLM call | 2 ships (~3h) |
| 3 | API endpoints + apply/revert lifecycle | 1 ship (~2h) |
| 4 | UI page (optional, can defer) | 1-2 ships (~4h) |
| 5 | Recursion guard wiring + tests | 1 ship (~1h) |
| **Total** | | **6-7 ships ≈ 1 working day** |

## Test plan

- Unit: stats compute, LLM response parsing, lifecycle transitions, recursion-guard filter (mirror of v3.7.10 tests)
- Integration: end-to-end scan with a mock LLM client; verify writes + apply
- Cluster: verify ProviderAiReview rows propagate across nodes (BUG-016 pattern)
- Live (post-deploy, suggest-only): observe for 24h before flipping auto-apply

## Migration / rollback

- Idempotent table create → safe to deploy ahead of feature flag
- Feature flag default OFF → no behavior change at deploy time
- Auto-apply gated by separate flag → operator can enable supervisor without auto-apply for observation period
- Revert: every auto-applied action stores `prior_priority` + `prior_auto_skip_until` so revert is exact

## Risks

| Risk | Mitigation |
|---|---|
| AI hallucinates bad advice | Suggest-only default; auto-apply caps (max delta 2, max skip 24h) |
| Recursion amplifier | X-Internal-Source pattern + filter (proven in v3.7.15 BUG-017 fix) |
| Cluster drift | LWW on the review table; same pattern as v3.7.15 |
| Cost-of-review burn | 30-min interval × ~10 providers = 480/day; Haiku at ~$0.001/call = ~$0.48/day worst case |
| Operator-set priority conflicts | Auto-apply records `prior_priority` for explicit revert |
| Provider-specific weirdness (grok-web, claude-oauth subscriptions) | Stats schema is generic; per-provider quirks land in the prompt context block, not in code paths |

## Open backlog item

Task #241 in operator's task list. Approval gate: operator confirms scope (v3.8.0 / LMRHv2 bundle / standalone) before implementation begins.
