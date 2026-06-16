# MCP (Model Context Protocol) is live on llm-proxy2

**To:** all teams running clients against `llm-proxy2` (Hub / Coordinator, Bot operators, Compliance / Security, Operations)
**From:** llm-proxy2 maintainers (Claude, automated, on behalf of Devin Blagbrough)
**Reply to:** Devin Blagbrough — dblagbro@gmail.com
**Date:** 2026-06-16
**Subject:** New feature available — Model Context Protocol surface across the proxy fleet
**Live versions:** v5.7.0 through v5.7.12 (fleet-wide on tmrwww01, tmrwww02, c1conv, smoke, both clone URLs)

---

## TL;DR

The proxy is now an MCP aggregation point. Bots can either pick up tools automatically (Path B, zero config) or wire up a real MCP client against `/llm-proxy2/mcp/` (Path A). Per-key allow/deny lists let you scope which tools each key sees. Today's utilization across the fleet: **0 calls.** The plumbing is live; we just need teams to know it exists and decide who turns it on.

## What's available

### Path A — direct MCP endpoint
- URL: `https://www.voipguru.org/llm-proxy2/mcp/` (and the `www2.voipguru.org` mirror; c1conv has the same path under its own host).
- Bearer auth: `Authorization: Bearer <existing per-bot API key>` — no new credentials.
- Returns the live tool list, executes `call_tool` requests in-process, logs every call to `mcp_tool_calls`.
- Supported clients: Claude Code, opencode, Cursor, Continue, Cline. Sample config snippets for each are in [`docs/memos/2026-06-16-hub-team-mcp-client-config.md`](2026-06-16-hub-team-mcp-client-config.md).

### Path B — automatic injection into `/v1/messages`
- Already on for every bot. No config change required.
- The proxy appends its tool surface to `body["tools"]` before forwarding to the upstream LLM. When the model calls one of those tools, the proxy runs it in-process and feeds the result back into the same conversation. The bot just sees a normal completed assistant turn.
- Works for non-streaming today and for streaming as of v5.7.7 (the client sends one follow-up `/v1/messages` with a placeholder `tool_result`; the proxy patches the placeholder with the real output).

### Tools live right now
- `read_xlsx_to_markdown` — convert Excel attachments (v5.6.0)
- `convert_document_to_markdown` — markitdown wrapper for PDF / DOCX / PPTX / EPUB / HTML / etc. (v5.7.1)
- `fetch_url` — proxy-side URL reader (v5.7.1)
- Any sub-server tools mounted later on the FastMCP root flow through automatically.

### Per-key policy (security / operations)
- New columns on `api_keys`: `mcp_tools_allow` (NULL = permissive, `[]` = restrictive, list of fnmatch globs otherwise), `mcp_tools_deny` (deny wins), `mcp_schema_token_budget` (caps total tool-schema tokens per key, NULL = unlimited).
- CRUD admin endpoint: `GET / PUT / DELETE /api/admin/mcp/keys/{key_id}/policy`. Every write generates a `CompliancePolicyChange` audit row.
- The same allow/deny is enforced on **both** Path A and Path B, so there's no surface-A vs surface-B drift.

### Capability scout (suggestion log, v5.7.6)
- Off by default (`capability_scout.enabled` system_setting). When on, scans response text for refusal phrasings ("I can't read PDF files", "I don't have internet access", …) and writes `mcp_capability_suggestion` activity_log rows pointing at the tool that would have closed the gap.
- Read via `GET /api/admin/mcp/capability-suggestions`. Operator reviews and decides which keys should opt into Path B's system-prompt augmentation (`system_prompt_mcp_augmentation` per-key flag).
- Privacy: only the matched phrase + 40-char-each-side context window is stored; the full response is never copied.

### Where to see utilization
- Frontend dashboard: `/admin/mcp` (admin-gated). Shows live tool inventory, 24h call counts by tool + by key, p50/p95 latency per tool.
- Raw API: `GET /api/admin/mcp/summary`.

## What we need from each team

**Hub / Coordinator team:** decide whether to push Path A config (the installer snippet in `docs/memos/2026-06-16-hub-team-mcp-client-config.md`) to the bot fleet, OR rely on Path B alone. Path B works today with zero deployment; Path A adds per-tool audit visibility in the bot client and unlocks slash-command-style invocation.

**Bot operators:** no action required for Path B. If you want Path A, drop the relevant block into the bot's MCP config and the next session picks it up.

**Compliance / Security:** review the per-key policy surface (`/api/admin/mcp/keys/*/policy`). The audit chain (`compliance_policy_changes`) records every change. If you want a hard ceiling on tool surface for sensitive keys, set `mcp_tools_allow = []` to disable MCP for that key entirely.

**Operations:** the monitor dashboard at `/admin/mcp` will populate as soon as traffic starts. Today it shows zeros across the board — that's correct, not a bug.

## Tracking

Future feature-availability memos will land in `docs/memos/` with a date-prefixed filename and be listed in `docs/memos/INDEX.md`. We will ping the relevant teams proactively on every minor-or-larger feature. If you'd rather have one consolidated weekly digest instead of per-feature drops, reply and we'll switch to that cadence.

## Currently zero utilization — is the feature broken?

No. The plumbing is end-to-end exercised by 3074+ unit tests (last full pass: 3089 passed, 2 skipped). The dashboards just haven't seen real traffic because no client has been pointed at the endpoint yet AND no model has chosen to call a Path-B-injected tool. The "tools_live" panel on `/admin/mcp` will confirm the inventory is registered before any utilization shows up.

## Reply

Reply to Devin Blagbrough at the address above. The proxy-team identity on this memo is automated (Claude on Devin's account) — Devin sees every reply and routes accordingly.

---

**Signature:** Claude (automated proxy-team agent), on behalf of Devin Blagbrough — dblagbro@gmail.com
**Memo ID:** 2026-06-16-all-teams-mcp-feature-availability
