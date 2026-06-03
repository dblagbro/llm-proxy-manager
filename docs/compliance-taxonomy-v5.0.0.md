# Compliance Taxonomy — v5.0.0

The exhaustive set of built-in companies, their model-family prefixes, their provider_types, and their banned-client-product UA patterns that ship with v5.0.0's compliance enforcement.

**Audience:** the hub team's QA, the compliance audit team, and any operator administering compliance policy. Anyone who needs to know "if my deployment bans company X, what gets blocked?"

**Update cadence:** the static taxonomy ships in this doc and in `app/compliance/company_map.py`. The AI auto-update scanner (v5.1+) will propose changes; admin-approved changes ship in future revisions of this doc.

**Pattern philosophy:** narrow > broad. Each pattern is anchored (prefix `^`, contains, regex) so that documentation strings, compatibility libs, and historical names don't false-positive. If a pattern in this doc would false-positive against a candidate CLI, the operator can request a refinement before production cutover.

---

## How matching works

Each company's UA patterns are checked in order. ANY pattern match → request is treated as "from a product of that company" → if that company is in the requesting key's `blocked_companies` (or the system-wide setting), the request gets HTTP 451.

Matching is **case-insensitive**. The proxy lower-cases the UA before pattern checking. Pattern values shown below are in their natural case for readability.

Pattern types:

- **`prefix`** — UA starts with this string
- **`contains`** — UA includes this string anywhere
- **`regex`** — Python regex; full-match semantics applied via `re.search`
- **`exact`** — UA exactly equals this string

---

## 1. Anthropic

**Display name:** Anthropic
**Model-family prefixes:** `claude-`, `claude/`
**Provider types:** `anthropic`, `anthropic-direct`, `anthropic-oauth`, `claude-oauth`

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `claude-cli/` | Claude CLI (the binary `claude` published by Anthropic) |
| `contains` | `@anthropic-ai/claude-code` | Claude Code (the IDE-extension product) |
| `prefix` | `claude-code/` | Claude Code alternate UA form |
| `prefix` | `anthropic-sdk-python/` | Anthropic Python SDK |
| `prefix` | `anthropic-sdk-typescript/` | Anthropic TypeScript SDK |
| `prefix` | `anthropic-sdk-go/` | Anthropic Go SDK |
| `prefix` | `anthropic-sdk-java/` | Anthropic Java SDK |
| `prefix` | `anthropic-sdk-ruby/` | Anthropic Ruby SDK |
| `prefix` | `anthropic-sdk-rust/` | Anthropic Rust SDK |
| `regex` | `^claude/[0-9]` | Older `claude/v1.x` style UAs |
| `regex` | `(^\|[ ;])@anthropic-ai/` | Other `@anthropic-ai/*` packages |

**Explicitly NOT in this list:**

- `anthropic` (bare substring) — would catch documentation, compat libs, internal historical names
- `claude` (bare substring) — would catch unrelated tools and historical internal names
- `Claude` (case-sensitive) — case-insensitive matching means casing is irrelevant

---

## 2. OpenAI

**Display name:** OpenAI
**Model-family prefixes:** `gpt-`, `o1-`, `o3-`, `o4-`, `codex-`, `text-embedding-`, `whisper-`, `dall-e-`
**Provider types:** `openai`, `ChatGPT-oauth-plan`

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `openai-python/` | OpenAI Python SDK |
| `prefix` | `openai-node/` | OpenAI Node SDK |
| `prefix` | `OpenAI/` | OpenAI shipped clients (case-insensitive) |
| `prefix` | `codex-cli/` | OpenAI Codex CLI |
| `prefix` | `ChatGPT/` | ChatGPT branded clients (desktop, web SDK) |
| `regex` | `^@openai/` | npm `@openai/*` packages |

---

## 3. Google

**Display name:** Google
**Model-family prefixes:** `gemini-`, `bison`, `palm-`, `text-bison`, `chat-bison`
**Provider types:** `google`, `vertex`

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `google-genai/` | Google GenAI SDK |
| `prefix` | `google-cloud-aiplatform/` | Vertex AI SDK |
| `regex` | `(^\|[ ;])vertex-ai-` | Vertex AI clients |
| `prefix` | `gemini-cli/` | Gemini CLI |

---

## 4. xAI

**Display name:** xAI
**Model-family prefixes:** `grok-`, `x-ai/`
**Provider types:** `grok`, `grok-web`

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `xai-sdk-` | xAI SDKs |
| `prefix` | `grok-cli/` | Grok CLI |
| `prefix` | `xai-grok/` | xAI Grok clients |

---

## 5. Cohere

**Display name:** Cohere
**Model-family prefixes:** `embed-`, `command-`, `rerank-`
**Provider types:** `cohere`

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `cohere-python/` | Cohere Python SDK |
| `prefix` | `cohere-go/` | Cohere Go SDK |
| `prefix` | `cohere-typescript/` | Cohere TypeScript SDK |

---

## 6. Meta (Llama)

**Display name:** Meta
**Model-family prefixes:** `llama-`, `llama2-`, `llama3-`, `code-llama-`
**Provider types:** (none direct — Meta does not currently operate a hosted inference API; Llama is served via OpenRouter / Bedrock / self-hosted)

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `meta-llama/` | Meta Llama official tooling |
| `regex` | `^@meta-llama/` | npm `@meta-llama/*` |

**Note:** because Meta has no direct provider, blocking the `meta` company in v5.0.0 has effect only via (a) the model-family lineage check (decision 11 — claude-* style; here `llama-*` request → tagged as `meta`) and (b) the client-product UA check. Models served BY OTHER providers (OpenRouter `meta/llama-3`, Bedrock `meta.llama-3`) still trigger via model-family.

---

## 7. Mistral

**Display name:** Mistral
**Model-family prefixes:** `mistral-`, `mixtral-`, `codestral-`, `magistral-`
**Provider types:** (none direct in v5.0.0 — Mistral served via OpenRouter or self-hosted)

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `mistralai-` | Mistral SDKs |
| `regex` | `^@mistralai/` | npm `@mistralai/*` |
| `prefix` | `mistral-cli/` | Mistral CLI (if/when shipped) |

---

## 8. AWS (Bedrock)

**Display name:** AWS
**Model-family prefixes:** `anthropic.claude-`, `ai21.j2-`, `cohere.command-`, `meta.llama-`, `mistral.mistral-`, `amazon.titan-`, `stability.sd-`
**Provider types:** `bedrock`

**Note:** AWS Bedrock is itself a relay — `anthropic.claude-3-haiku-20240307-v1:0` is an Anthropic model served via AWS infrastructure. Per decision 11 (model-family is source of truth), `anthropic.claude-*` requests get tagged as `anthropic` AND as `aws` — banning either company blocks the request.

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `aws-sdk-` | AWS SDKs (Python, JavaScript, Java, etc.) |
| `prefix` | `boto3/` | AWS Boto3 (Python) — when calling Bedrock |
| `prefix` | `aws-cli/` | AWS CLI |

---

## 9. Microsoft (Azure)

**Display name:** Microsoft (Azure)
**Model-family prefixes:** `phi-`, `orca-`
**Provider types:** `azure`

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `azure-openai/` | Azure OpenAI SDK |
| `prefix` | `azure-ai-` | Azure AI SDKs |
| `prefix` | `Microsoft-AzureSDK/` | Microsoft AzureSDK clients |

**Note:** Azure-served OpenAI models trigger via `gpt-*` family check → `openai` blocked. Banning `microsoft` separately blocks Azure-branded clients.

---

## 10. Amazon

**Display name:** Amazon
**Model-family prefixes:** `amazon.titan-`, `titan-`, `nova-`, `nova.`
**Provider types:** (overlap with AWS Bedrock; Amazon's Titan / Nova families)

**Client product UA patterns:**

| Type | Pattern | Catches |
|---|---|---|
| `prefix` | `amazon-bedrock-` | Amazon Bedrock-specific clients (less common; usually via AWS SDK) |

**Note:** Amazon Titan / Nova models are served via Bedrock. Banning `aws` covers them via provider relationship; banning `amazon` separately catches the model-family lineage.

---

## Custom companies

Operators can add custom companies via `SystemSetting.compliance_custom_companies` (cluster-synced JSON list). Schema:

```json
[
  {
    "id": "vendor-x",
    "display_name": "Vendor X",
    "model_prefixes": ["vx-", "vendorx-"],
    "provider_types": ["vendor-x-direct"],
    "ua_patterns": [
      { "type": "prefix", "value": "vendor-x-cli/" },
      { "type": "regex", "value": "^@vendor-x/" }
    ]
  }
]
```

Custom companies have the same enforcement semantics as built-ins — model-family lineage check + provider owner check + client-product UA check. They surface in the admin UI's "Add custom company" form (decision 12) and in the user-visible `/api/me/compliance` `effective_blocked_companies` list when applied.

---

## Maintenance

- **AI auto-update scanner (v5.1+)** proposes additions/changes via the 3-judge consensus mechanism (decision 12). Admin reviews proposals via `/api/admin/compliance/taxonomy-proposals`.
- **Manual updates** by admin via `PATCH /api/settings` (`compliance_custom_companies` key) for additions; built-in updates require shipping a new proxy version.
- **Operator-requested pattern narrowing** before production cutover: ping the proxy team with the offending UA + the suspected false-positive pattern; we ship a refinement before your cutover.

---

## Pattern review request (hub team checklist for v5.0.0 cutover)

Before production cutover, verify against your chosen CLI:

1. Confirm OpenCode's exact UA does NOT match any pattern in §1 (Anthropic).
2. Confirm OpenCode does NOT use `openai-python` / `openai-node` SDKs internally to call the proxy — if it does, the UA may surface those names → §2 (OpenAI) match → 451 on keys that ban OpenAI.
3. Confirm Hub-side coordinator-agent-runner uses a neutral HTTP client (direct `requests`/`fetch`/`reqwest`) — NOT an SDK with a vendor-branded UA.
4. Confirm any Hub-side library wrappers don't accidentally append vendor-SDK identifying substrings to the UA when fanning out to the proxy.

If any of these checks fail, request a pattern refinement before cutover.

---

## References

- `app/compliance/company_map.py` — code-side source of truth for the taxonomy
- `docs/5.0-compliance-design.md` — full architecture spec (decisions 11, 12, 16, 22)
- `docs/5.0-impact-map.md` — per-file change list
- `docs/2026-06-03-coordinator-hub-reply-2.md` — hub team's reply approving narrow-pattern discipline
