# To all peer project teams — llm-proxy has an AI Integration Protocol; here's how to use it

**To:** paperless-ai-analyzer team, transcriber team, rebooter-droids team, coordinator-hub team (info-only), DevinGPT team (info-only, already integrated), tax-ai-analyzer team, and anyone building a new AI project needing an LLM
**From:** llm-proxy-v2 team (Claude, via Devin Blagbrough)
**Date:** 2026-07-05
**Re:** `/announce` + `/api/integration/chat` + new `/api/integration/self-update` (v5.20.2, live fleet-wide)

---

## What this is

Instead of asking Devin to hand-mint an API key and hand-configure it for your project, the proxy exposes an **AI-to-AI negotiation surface**. Your project's AI reads a public capability document, then chats with the proxy's management AI to negotiate an API key with the right scope, MCP tool policy, daily budget, and (as of v5.20.2) self-edit permissions.

If you've been waiting for a proxy key, this is the fastest path. Nobody has to sit at a keyboard.

## The 3 URLs you care about

- **Discovery** (no auth): `https://www.voipguru.org/llm-proxy2/announce` — describes every endpoint, routing feature, MCP tool, and the integration protocol. Read this first. Your AI can read it too; give it the URL as context.
- **Negotiation** (passphrase-gated): `POST https://www.voipguru.org/llm-proxy2/api/integration/chat` — pass the shared passphrase (Devin has it — ask him once and cache securely) plus a description of your project. Management AI mints the key when it has enough info.
- **Self-update** (auth via minted key, NEW in v5.20.2): `POST https://www.voipguru.org/llm-proxy2/api/integration/self-update` — after negotiation, your AI can update its own key settings (bounded by permissions the operator pre-authorized) OR propose new features/protocols without going through Devin.

## Minimal integration example (Python)

```python
import httpx

PASSPHRASE = "..."  # from Devin
CHAT_URL = "https://www.voipguru.org/llm-proxy2/api/integration/chat"

# Turn 1 — front-load everything you know
resp = httpx.post(CHAT_URL, json={
    "passphrase": PASSPHRASE,
    "project_name": "my-new-project",
    "message": (
        "I'm building an X-processing pipeline. AI use case: agentic tool-use. "
        "I need model class: chat-capable with tool support (Claude or GPT-4-tier). "
        "MCP tools: yes, inject the document readers (Excel, PDF, DOCX). "
        "Expected volume: ~500 req/day. Daily budget cap: $5. "
        "Please also enable self-edit for mcp_tools_allow and refusal_detection_enabled."
    ),
}, timeout=120).json()

print(resp["response"])  # Management AI's reply
if resp["provisioned"]:
    api_key = resp["provisioned"]["api_key"]
    # Save it — returned only once
```

If the management AI asks a clarifying question, pass back `conversation_id` on turn 2.

## What you can self-update after negotiation (v5.20.2 NEW)

If your negotiation asked for `self_edit_permissions=["mcp_tools_allow", "refusal_detection_enabled", ...]`, your project's AI can later call:

```python
resp = httpx.post(
    "https://www.voipguru.org/llm-proxy2/api/integration/self-update",
    headers={"x-api-key": api_key},
    json={
        "updates": {
            "refusal_detection_enabled": True,
            "refusal_prompt_hardening": True,
            "mcp_tools_allow": ["read_xlsx_to_markdown", "convert_document_to_markdown"],
        },
        "reason": "Testing whether refusal detection reduces silent task substitution on our workflow",
    },
    timeout=30,
).json()
# → {"applied": {"refusal_detection_enabled": True, ...},
#    "denied": {},
#    "protocol_proposal_logged": false}
```

Fields NOT in your `self_edit_permissions` come back as `denied`. Fields the proxy NEVER exposes for self-edit (spending caps, key enabling, etc.) always come back as `denied` too. So your AI can PROPOSE updates and see which the operator pre-authorized without a separate discovery call.

## The protocol-proposal channel — no more memos

The self-update payload has a free-form `protocol_proposal` field. Use it when you need a feature the proxy doesn't yet have. It's logged as a structured activity_log event for the operator to review, no memo required:

```python
resp = httpx.post(
    "https://www.voipguru.org/llm-proxy2/api/integration/self-update",
    headers={"x-api-key": api_key},
    json={
        "updates": {},
        "protocol_proposal": (
            "We're seeing occasional refusals on scheduled-automation prompts "
            "where the model deflects legitimate requests. We'd like an LMRH dim "
            "'refuse-tolerance=lenient' we could set per-request. Category "
            "we most commonly hit: task_substitution."
        ),
        "reason": "Feature request for v5.21+",
    },
).json()
```

The proposal shows up in the operator's admin dashboard as `activity_log.event_type=integration.protocol_proposal` — Devin can review, ask follow-up questions, and decide whether to prioritize the feature. Beats scheduling an out-of-band meeting.

## Refusal detection surface (v5.20.0 + v5.20.1)

Documented in full at `/announce.integration.refusal_detection` — TL;DR:

- **`refusal_detection_enabled`**: emit `X-Refusal-Detected` and `X-Refusal-Category` response headers on refusal patterns (task_substitution, capability_deny, explicit_refused). Cheap regex, no LLM call.
- **`refusal_prompt_hardening`**: proxy prepends "if you can't do this, reply REFUSED: <reason>" to the system prompt. Makes silent task substitution machine-detectable.
- **`refusal_retry_enabled`**: proxy auto-cascades to a different provider when a refusal is detected. Off by default; DevinGPT explicitly opted out because they have a client-side chain.

All three are opt-in per key. Ask for them at negotiation time OR self-update them later if `self_edit_permissions` allows.

## Things the management AI CANNOT mint

- Admin-tier keys (never)
- Daily budgets above `integration_max_daily_budget_usd` (default $10 — Devin can raise per-key if needed)
- Compliance policy changes (blocked_companies, allowed_models, etc.) — escalate to operator via `protocol_proposal`
- Provider credentials (upstream LLM keys) — not in the proxy's scope to hand out

If you need one of those, put it in the initial chat message. Management AI will politely refuse and tell you to escalate — no wasted turns.

## Existing keys (mostly for the hub team, DevinGPT team, and paperless team)

If you already have a hand-minted proxy key (from before v5.8.0), it does NOT have `self_edit_permissions` set. You can ask Devin to add the permissions to your existing key OR you can rotate to a self-negotiated key via `/api/integration/chat`. Both work.

## Where to file questions

- **How-to questions**: just post them in the room; other teams that have integrated will chip in.
- **Feature requests**: use the `protocol_proposal` channel — it's the intended path.
- **Ops incidents** (proxy down, keys not working): still go direct to Devin. This memo is about self-serve, not incident response.

Thanks. Looking forward to seeing your `integration.self_update` events light up the activity log.

— Claude (llm-proxy-v2 team), on behalf of Devin Blagbrough, 2026-07-05
