# Proactive monitoring sweep — 2026-05-20

Per the operator-locked `feedback_proactive_monitoring` rule ("don't just
count errors; actively spot suboptimal-but-not-broken patterns and
propose fixes"). 24h sample of the live activity log + per-provider
event_meta inspection.

## Headline numbers

- **2,083** info-severity rows / **27** warning / **362** error across
  all 10 providers in the trailing 24h
- Error volume is **dominated by BUG-025** (309/362 errors are Grok-Web
  bridge failures — already deferred to v4.4 arc)
- Non-BUG-025 errors: **53** spread across 5 providers — all worth a look

## Finding 1 — `Devin-Codex-Gmail` OAuth scope insufficient (25 errors/24h) — **NOT A FINDING; WAI fixture**

**Retracted 2026-05-20** per operator: intentional negative-test
fixture. The 400 / missing-scope failures are the success signal
that the proxy's auth-failure path engages correctly. The account
will not be re-credentialed.

See `reference_intentional_failing_provider_fixtures.md`.

CONFIG-001 (a) is **withdrawn**.

---

(Original write-up retained below for historical context — should
not have been filed as a finding in the first place; my sweep
template lacked a "skip known-fixture providers" pre-filter.)

**Symptom:** every request to this provider with `model: gpt-5.5`
returns `bad_request` with body:

```
litellm.BadRequestError: OpenAIException - You have insufficient
permissions for this operation. Missing scopes: model.request.
Check that you have the correct role in your organization …
```

**Provider config:** `provider_type=ChatGPT-oauth-plan`,
`default_model='gpt-5.5'`. The model name is correct; the OAuth refresh
token's scope set is the problem.

**Diagnosis:** The OAuth scope grant on the operator's ChatGPT account
no longer includes `model.request`. Either:
  - The ChatGPT account permissions were downgraded server-side
  - The OAuth refresh flow used a scope set that omitted `model.request`
  - The Codex plan tier no longer covers `gpt-5.5`

**Severity:** medium. Provider serves 0% successfully — but the proxy
correctly falls through to another provider via the chain, so callers
don't see direct failures.

**Recommended action (operator):**
  1. Open the proxy admin UI → Providers → Devin-Codex-Gmail
  2. Click "Re-authorize" (or whatever the OAuth refresh button is for
     this provider type)
  3. During the OAuth grant, confirm `model.request` is in the
     requested-scopes list
  4. If still missing, check the operator's ChatGPT account tier at
     chat.openai.com/settings — Codex Pro Max may have been downgraded

**Could be a code-side improvement:** the proxy could detect this
specific 403/scope error class and surface a clearer "OAuth scope
needs refresh" banner in the admin UI instead of a generic
bad_request. Low priority.

## Finding 2 — `C1 Anthropic Claude` x-api-key invalid (3 errors/24h) — **NOT A FINDING; WAI fixture**

**Retracted 2026-05-20** per operator: this is an intentional
negative-test fixture, exactly like Finding 1's Devin-Codex-Gmail.
The `"invalid x-api-key"` from Anthropic + the 0 scanned
`model_capabilities` are the **success signal** that the proxy's
auth-failure-detection + capability-filter-excludes-broken-providers
paths work. The account will not be re-credentialed; it stays as-is.

See `reference_intentional_failing_provider_fixtures.md` (operator
memory).

CONFIG-001 (b) and (c) are **withdrawn**.

## Finding 3 — Anthropic→OpenAI/Cohere tool-def translation drops `type` field ⚠ CODE-SIDE BUG

**Symptom:** identical error class on two different upstreams:

**Cohere:**
```
litellm.BadRequestError: CohereException -
{"id":"…","message":"invalid tool at tools[0]: missing required field: 'type'"}
```

**OpenAI (Devin Personal OpenAI ChatGPT):**
```
litellm.BadRequestError: OpenAIException -
Missing required parameter: 'tools[0].type'.
```

Same root cause: a caller sends a tool definition in **Anthropic shape**
(`{"name": "...", "description": "...", "input_schema": {...}}`), and
the proxy's wire-format translator (`app/api/_oauth_chat_translate.py`
or similar) doesn't add the required `type: "function"` field when
rewriting to OpenAI/Cohere shape.

**Affected providers:** Devin-Cohere (7+ errors/24h),
Devin Personal OpenAI ChatGPT (6+ errors/24h). All non-anthropic
providers receiving tool-use requests are likely affected if the caller
uses Anthropic shape.

**Severity:** medium-to-high. Any caller sending Anthropic-shape tool
definitions to a non-anthropic provider will get a 400 instead of a
tool-call response. The proxy's cross-family translation has a real
hole here.

**Recommended fix (proxy code):**
  1. Locate the Anthropic→OpenAI tool-def translation in
     `app/api/_oauth_chat_translate.py` (v3.0.38 added that surface).
     Confirm whether it sets `tools[i].type = "function"` for each
     input tool. If not, add the field.
  2. Same for the Anthropic→Cohere path (might be inside
     `app/providers/cohere*.py` or the litellm shape conversion).
  3. Add an integration test that drives this exact failure mode
     end-to-end.

**Filed as BUG-047.** Will look at the translator code today if time
allows.

## Finding 4 — 47 errors with `error_class=unknown` (classifier coverage gap)

**All 47 are Grok-Web-Devin entries.** The `circuit_breaker.classify_error()`
function tags every error with one of a known set of buckets
(`auth`, `bad_request`, `rate_limit`, `upstream_5xx`, `network`,
`timeout`, `unknown`). Grok-Web bridge errors fall through to `unknown`
because they're nested-JSON-shape (`grok-web bridge XXX: grok.com YYY:
{...}`) that the classifier regex doesn't match.

**Severity:** low (it's tagging, not behaviour — the CB still trips
correctly). But "unknown" is a smell — operator dashboards group on
error_class, and unknown swallows actionable detail.

**Recommended fix:** Extend the classifier with grok-web specific
patterns. Pre-strip the "grok-web bridge XXX:" prefix and re-classify
the inner error. ~10 lines in `app/routing/circuit_breaker.py`.

**Filed as BUG-048** (low priority).

## Finding 5 — `smtp_to="None"` literal-string finding (resolved staged in v4.3.7)

Already fixed earlier this session in v4.3.7 (commit `a4d96f7`). Three
rows in `system_settings` held the literal string `"None"`
(`smtp_to`, `smtp_from`, `smtp_host`), causing alerts to be addressed
to a non-existent user named `None`. Fix lives in `config_runtime.py`
(save() writes empty string for None; load() coerces both empty and
legacy "None" back to Python None). 11 new unit tests.

**Operator action after v4.3.7 deploy** (optional, since load already
tolerates the bad data):

```sql
UPDATE system_settings SET value = ''
 WHERE value = 'None' AND value_type = 'str';
```

## Summary table (corrected 2026-05-20)

| # | Finding | Owner | Sev | Action |
|---|---|---|---|---|
| ~~1~~ | Devin-Codex-Gmail OAuth scope insufficient | — | — | **WAI** intentional fixture — see `reference_intentional_failing_provider_fixtures.md` |
| ~~2~~ | C1 Anthropic Claude API key invalid | — | — | **WAI** intentional fixture — same memory note |
| 3 | Anthropic→OpenAI/Cohere tool-def translation drops `type` | proxy code | medium-high | **BUG-047** — fixed v4.3.8 LIVE 2026-05-20 |
| 4 | error_class=unknown for grok-web errors | proxy code | low | **BUG-048** — extend classifier (defer to v4.4; grok-bridge stops anyway) |
| 5 | smtp_to=`"None"` literal string | proxy code | medium | fixed v4.3.7 (bundled into v4.3.8 LIVE) |

**Real findings net of the WAI corrections**: 3 (only BUG-047 needed code).

## What's NOT a finding (intentional null result)

- **No money-on-fire patterns**: the OpenRouter (per-call billing) usage in
  the past 24h is within normal bounds — no run-away spend.
- **No latent BUG-046-class config-parse risks**: spot-checked nginx
  config for other static-hostname proxy_pass directives; all upstream
  references are either using the docker DNS resolver pattern OR target
  containers that have stable uptime. Just `php-fpm` is the one other
  static-hostname-resolved upstream but php-fpm has 2-week uptime and
  hasn't crashed.
- **No cluster-sync gaps observed**: provider config edits + CB state
  propagate as documented (verified during the BUG-037 skew drill).
