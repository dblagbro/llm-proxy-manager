# llm-proxy2 — outbound-memo index

Every feature-availability memo I send to other teams lives in this directory. New memos go here so consumer teams have one place to look for "what's new in the proxy".

**Author identity on all memos:** Claude — llm-proxy2 maintainer agent. The reply path is "address Claude / proxy team in the body; Devin Blagbrough relays" — Devin is transport, not recipient.

**Policy:** every minor-or-larger feature ship (every `v5.X.0`+ and every `v5.X.Y` that surfaces a new external endpoint or contract change) generates a memo here BEFORE the ship is announced as available. Hotfixes that don't change the external surface don't require a memo. When in doubt, write one — the cost is low and the visibility gain is large.

## Memo log

| Date | ID | Audience | Topic | Status |
|---|---|---|---|---|
| 2026-06-16 | [2026-06-16-all-teams-mcp-feature-availability](2026-06-16-all-teams-mcp-feature-availability.md) | All teams | MCP (Model Context Protocol) surface live across the fleet — Path A (`/mcp/`) + Path B (`/v1/messages` injection) + per-key policy + capability scout | Drafted; awaiting operator forward |
| 2026-06-16 | [2026-06-16-hub-team-mcp-client-config](2026-06-16-hub-team-mcp-client-config.md) | Hub team | Path A MCP client config snippets for Claude Code / opencode / Cursor / Continue / Cline | Drafted; awaiting operator forward |

## How to log a new memo

1. Write the memo at `docs/memos/YYYY-MM-DD-<audience>-<topic>.md`.
2. Add a row to the table above.
3. Sign as Claude (llm-proxy2 maintainer agent). The reply convention: "Address Claude / proxy team in the body; Devin relays." Devin is transport, NOT recipient — replies are FOR me.
4. Status starts as "Drafted; awaiting operator forward". Update to "Sent <date>" once the operator confirms forwarding. Add a "Reply received <date>" subline if a team responds.

## Drafting checklist (for future memos)

Each memo should answer:

- What's new? (one TL;DR paragraph)
- What versions does this affect? (proxy version + fleet status)
- What's the contract change, if any? (URL, schema, header, behavior)
- Who's affected? (which teams need to read; which need to act)
- What's the action required per team?
- Where can recipients see utilization / verify it's working?
- Where do replies go? (always: Devin)

Keeping these consistent helps recipients triage memos quickly.
