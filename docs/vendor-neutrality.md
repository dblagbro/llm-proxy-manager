# Vendor-neutrality policy guide

llm-proxy-v2 is a vendor-neutral LLM routing gateway. Anthropic/Claude, OpenAI, Google, Mistral, xAI, Cohere, Meta, AWS, Ollama, vLLM, llama.cpp, OpenRouter, LiteLLM-bridged providers, and any other adapter the operator configures are **optional**, **configurable**, **replaceable**, and **blockable** by deployment policy.

No single vendor is required for the gateway to start, route, or serve traffic. Anthropic in particular can be allowed, preferred, deprioritized, or blocked using the same mechanism that controls every other vendor.

This document is the operator runbook for that policy mechanism.

## Quick reference

| Want to… | Set |
|---|---|
| Allow only OpenAI + Google | per-key `allowed_companies = ["openai", "google"]` (or system `compliance_system_allowed_companies`) |
| Block Anthropic entirely | per-key `blocked_companies = ["anthropic"]` (or system equivalent) |
| Allow Claude family but ban Opus 4 | `blocked_models = ["claude-opus-4-*"]` |
| Allow only `gpt-4*` | `allowed_models = ["gpt-4*"]` |
| Prefer Anthropic with OpenAI fallback | `allowed_companies = ["anthropic", "openai"]` + provider priority field |
| Halt ALL LLM traffic fleet-wide | toggle `compliance.llm_emergency_stop` via WebUI or `POST /api/admin/llm-emergency-stop/toggle` |
| Block all clients identifying as `claude-code/*` | `compliance_ua_block_enabled = true` (default) + `anthropic` in any blocklist |

All knobs are cluster-replicated within ~60 seconds and audited to `compliance_policy_changes` and `compliance_events`.

## Policy model

The audit-grade policy bundle has four dimensions, each per-key AND system-wide. They union; deny wins.

```
Policy:
  blocked_companies:  Set[company_id]            # e.g. {"anthropic"}
  allowed_companies:  Set[company_id]            # non-empty → allowlist mode
  blocked_models:     List[exact-or-glob]        # e.g. ["claude-opus-4-*"]
  allowed_models:     List[exact-or-glob]        # non-empty → allowlist mode
```

**Evaluation order** (`app/compliance/policy.py::evaluate_policy`):

1. Company `owner_company` is in `blocked_companies` → drop, `reason_code = blocked-company`.
2. Resolved model-family company set (e.g. `anthropic.claude-3-haiku` → `{anthropic, aws}`) intersects `blocked_companies` → drop, `blocked-model-family`. Catches Bedrock-served Anthropic.
3. Provider's `default_model` OR caller's `requested_model` matches any `blocked_models` glob/exact → drop, `blocked-model`.
4. `allowed_companies` non-empty AND neither `owner_company` nor model-family is in it → drop, `company-not-in-allowlist`.
5. `allowed_models` non-empty AND neither `default_model` nor `requested_model` matches → drop, `model-not-in-allowlist`.
6. Otherwise → allow.

A model in BOTH `blocked_models` and `allowed_models` is **blocked** (deny wins). Same for companies.

**Pattern matching** is `fnmatch`-style and case-insensitive. `claude-*` matches every Claude family; `gpt-4-*-turbo` matches all turbo variants. Exact names without wildcards are exact matches.

**Cluster sync.** Per-key edits replicate via the existing `api_keys` push (~60s) with field coverage; pre-v5.2.0 peers don't clobber v5.2 fields (membership-test apply, mirrors the v4.4.18 fix). System-wide edits replicate via the generic `system_settings` push. Cache invalidation triggers on receipt.

**Fail safe.** If no allowed provider remains after filtering, the request returns HTTP 503 with `X-Compliance-Refused: no-compliant-provider-available` and an audit row. The system never silently routes to a banned vendor.

## Per-key vs system-wide

Each dimension exists on both `ApiKey` (per-key) and `SystemSetting` (deployment-wide). They union, with deny-wins semantics applied to the merged bundle.

| Dimension | Per-key column | System setting |
|---|---|---|
| Blocked companies | `api_keys.blocked_companies` | `compliance_system_blocked_companies` |
| Allowed companies | `api_keys.allowed_companies` | `compliance_system_allowed_companies` |
| Blocked models | `api_keys.blocked_models` | `compliance_system_blocked_models` |
| Allowed models | `api_keys.allowed_models` | `compliance_system_allowed_models` |

System settings carry the same JSON-encoded `list[str]` shape and replicate via cluster sync just like per-key fields.

## Example deployment profiles

### Anthropic-permitted (default behavior)
No policy set → all configured providers are eligible. Routing scores by latency, cost, capability, and operator-set priority.

### Anthropic-preferred (allowlist with Anthropic first)
```json
{
  "allowed_companies": ["anthropic", "openai", "google"],
  "blocked_companies": [],
  "blocked_models": [],
  "allowed_models": []
}
```
Combined with raising the priority of Anthropic provider rows; the router prefers higher-priority providers within the allowlist.

### Anthropic-blocked (compliance-locked deployment)
```json
{
  "allowed_companies": [],
  "blocked_companies": ["anthropic"],
  "blocked_models": [],
  "allowed_models": []
}
```
Catches every Anthropic adapter, every Claude family model name, and Bedrock-served Anthropic. Add `"openai"` etc. to extend the ban.

### Open-source-only
```json
{
  "allowed_companies": ["meta"],
  "blocked_companies": [],
  "blocked_models": [],
  "allowed_models": []
}
```
Pair with provider rows for `ollama`, `vllm`, `llama.cpp` — all serve Meta-family open-weight models and resolve to company `meta` via the taxonomy.

### Granular model ban
```json
{
  "blocked_models": ["claude-opus-4-*", "gpt-4.1"],
  "allowed_companies": ["anthropic", "openai"]
}
```
Allow Anthropic and OpenAI broadly, but ban two specific model lines.

## Adding a new provider adapter without vendor lock-in

The router dispatches off the `Provider` DB row's `provider_type`. Add a new vendor by:

1. Register the `provider_type` slug in `app/routing/litellm_binding.py::PROVIDER_TYPE_TO_LITELLM` (maps your slug → the litellm prefix).
2. Add a default-model entry to `PROVIDER_DEFAULT_MODELS`.
3. Add a row to `app/compliance/company_map.py::KNOWN_COMPANIES` so blocklist/allowlist matching works symmetrically (or use `COMPLIANCE_CUSTOM_COMPANIES` env JSON for deployment-only additions).
4. If the vendor needs a request/response shape that isn't OpenAI-chat-shaped or Anthropic-messages-shaped, add an adapter file in `app/providers/` and a dispatch branch in `app/api/messages.py` next to the existing `claude-oauth` / `grok-web` branches. Provider-specific request shapes MUST stay isolated behind that adapter; do not leak them into the generic router or `acompletion_with_retry`.

No code changes are required to start ALLOWING or BLOCKING the new provider — policy operates on the slugs you registered.

## Emergency stop

See [`emergency-stop-runbook.md`](emergency-stop-runbook.md) for the operator procedure. In brief: a single fleet-wide toggle that returns HTTP 503 for every `/v1/messages`, `/v1/chat/completions`, and background `acompletion_with_retry` call until disengaged. Every blocked request writes an audit row.

## Tests proving the contract

- `tests/unit/test_v521_fine_grained_policy.py` — 27 tests, every operator-spec dimension.
- `tests/unit/test_v520_llm_emergency_stop.py` — 15 tests for the kill switch.
- `tests/unit/test_v5_router_compliance_pre_filter.py`, `test_v5_messages_ua_block.py`, `test_v5_compliance_endpoints.py`, `test_v5_bedrock_multi_company.py` — v5.0 baseline.

All tests are hermetic — no live vendor calls. Live-traffic integration tests are opt-in via `--run-real` plus an explicit env var.

## What the gateway does NOT do

- It does **not** hardcode Claude as a default model anywhere in the routing path. The chain is `request.model → provider.default_model → PROVIDER_DEFAULT_MODELS[provider_type] → "gpt-4o"` (only for genuinely unknown provider types).
- It does **not** require any vendor SDK package as a hard dependency. Vendor adapters lazy-import behind `try/except` and feature flags.
- It does **not** require any vendor API key at startup. The app boots with zero providers and an empty policy.
- It does **not** silently fall back to a banned vendor. When the allowed set is empty, the response is HTTP 503 with an audit row.

## Related docs

- `architecture.md` — full system architecture, including the compliance section.
- `5.0-compliance-design.md` — v5.0.0 design doc behind the company/model taxonomy.
- `compliance-taxonomy-v5.0.0.md` — the `KNOWN_COMPANIES` shape.
- `emergency-stop-runbook.md` — operator runbook for the v5.2.0 kill switch.
