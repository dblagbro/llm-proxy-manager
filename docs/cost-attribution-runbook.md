# Cost Attribution Runbook

How to read activity-log billing data correctly. Targeted at callers building cost
dashboards (paperless-ai-analyzer, AI Tax Analyzer, DevinGPT) and at proxy operators
investigating spending-cap behavior.

## TL;DR — query templates

```sql
-- Real per-call billing for the last hour, by API key
SELECT
  api_key_id,
  COUNT(*) AS calls,
  ROUND(SUM(CAST(json_extract(event_meta,'$.cost_usd') AS REAL)), 4) AS real_cost_usd
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-1 hour')
  AND json_extract(event_meta,'$.cost_class') = 'per_call'
GROUP BY api_key_id
ORDER BY real_cost_usd DESC;

-- Subscription-quota consumption for the last hour (informational; $0 real billing)
SELECT
  api_key_id,
  COUNT(*) AS calls,
  ROUND(SUM(CAST(json_extract(event_meta,'$.quota_usd') AS REAL)), 4) AS quota_consumed_usd
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-1 hour')
  AND json_extract(event_meta,'$.cost_class') = 'subscription'
GROUP BY api_key_id
ORDER BY quota_consumed_usd DESC;
```

## Background

As of v3.0.50, the proxy classifies every `llm_request` event as one of two cost classes
in the activity-log `event_meta` payload:

| `cost_class`     | Provider types                                    | `cost_usd` semantics                              | `quota_usd` semantics                                                |
|------------------|---------------------------------------------------|---------------------------------------------------|----------------------------------------------------------------------|
| `per_call`       | `openai`, `anthropic`, `anthropic-direct`, `vertex`, `google`, `cohere`, `xai`, ... | Real $ billed to operator (litellm rate)          | absent / `None`                                                       |
| `subscription`   | `claude-oauth`, `codex-oauth`, `anthropic-oauth`  | Always **$0** — subscription quota, no per-call billing | What this would have cost on per-call billing (litellm-rate equivalent) |

This split was driven by the 2026-05-02 paperless burn incident: cross-family-substituted
gpt-4o → codex-oauth calls were being recorded with the substituted-model litellm cost
even though codex-oauth is operator's flat-rate ChatGPT Plus subscription (real cost: $0).
The paperless rolling cost ticker was reading ~$3-5/hr inflated.

## Building a cost ticker

### Real-billing-only ticker (recommended)

For an operator-facing "what am I being billed" ticker, filter on
`cost_class = 'per_call'` and ignore subscription rows:

```sql
SELECT ROUND(SUM(CAST(json_extract(event_meta,'$.cost_usd') AS REAL)), 4)
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-1 hour')
  AND api_key_id = ?  -- your key
  AND json_extract(event_meta,'$.cost_class') = 'per_call';
```

### Quota-tracking ticker (advanced)

For a "what would this cost on per-call billing" sister-ticker (useful when comparing
subscription vs per-call paths, or projecting cost if subscription tier ever runs out):

```sql
SELECT ROUND(SUM(CAST(json_extract(event_meta,'$.quota_usd') AS REAL)), 4)
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-1 hour')
  AND api_key_id = ?
  AND json_extract(event_meta,'$.cost_class') = 'subscription';
```

### Combined view

```sql
SELECT
  json_extract(event_meta,'$.cost_class') AS cc,
  COUNT(*) AS calls,
  ROUND(SUM(CAST(json_extract(event_meta,'$.cost_usd')  AS REAL)), 4) AS real_billed_usd,
  ROUND(SUM(CAST(json_extract(event_meta,'$.quota_usd') AS REAL)), 4) AS quota_used_usd
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-1 hour')
  AND api_key_id = ?
GROUP BY cc;
```

## Common pitfalls

**Don't sum `cost_usd` across both classes**: subscription rows are $0, so summing them
together is correct mathematically — but if you separately track quota burn for capacity
planning, mixing them obscures whether the $X/hr is real billing or quota that hasn't
hit its monthly cap yet.

**Don't filter on `chosen-because=cross-family-fallback` to exclude substituted calls
from cost**: a substituted call lands on the cheapest matching provider (per v3.0.46
paid-plan-preferred logic), and the proxy already records cost correctly for whichever
provider served. The cost class is the right discriminator, not the substitution event.

**Legacy events (`cost_class IS NULL`)**: pre-v3.0.50 events lack the `cost_class` field.
Their `cost_usd` reflects the pre-fix overcounting on subscription paths. Filter them
out with `cost_class IS NOT NULL` if you want a stable ticker that excludes the
historical inflation period.

## Substitution-aware audit

Drift check — find all events where the served model differed from what the caller
requested (cross-family substitution markers per LMRH 1.2 §E1):

```sql
SELECT
  created_at,
  json_extract(event_meta,'$.requested_model') AS requested,
  json_extract(event_meta,'$.served_model')   AS served,
  json_extract(event_meta,'$.cost_class')     AS cc,
  json_extract(event_meta,'$.in_tok')         AS in_tok
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-24 hours')
  AND api_key_id = ?
  AND json_extract(event_meta,'$.requested_model') IS NOT NULL
  AND json_extract(event_meta,'$.requested_model') != json_extract(event_meta,'$.served_model')
ORDER BY created_at DESC
LIMIT 100;
```

## Cache-savings audit

Per-key cache-read savings rollup (cache_read_input_tokens × $cached_rate):

```sql
-- Anthropic cache_read pricing is roughly 10% of input rate.
-- For Haiku 4.5 at $0.80/M input → cache reads ~$0.08/M.
SELECT
  COUNT(*) AS subscription_events,
  -- Approximation: extract cache_read_input_tokens via regex on response_body
  -- (adjust for your model's input rate)
  COUNT(*) AS placeholder
FROM activity_log
WHERE event_type='llm_request'
  AND severity='info'
  AND created_at >= datetime('now','-24 hours')
  AND api_key_id = ?
  AND json_extract(event_meta,'$.cost_class') = 'subscription';
```

Cache token counts are returned in `usage.cache_read_input_tokens` and
`usage.cache_creation_input_tokens` in the upstream Anthropic response and captured
in the activity log's `response_body` (when body capture is enabled).

## See also

- `docs/lmrh-1.2-substitution-disclosure.md` — wire-format spec for substitution events
- `docs/lmrh-1.2-cache-mode-dim.md` — caller-side cache-mode hint dim
- `app/monitoring/helpers.py:record_outcome` — where `cost_class`, `cost_usd`, `quota_usd`
  are written
