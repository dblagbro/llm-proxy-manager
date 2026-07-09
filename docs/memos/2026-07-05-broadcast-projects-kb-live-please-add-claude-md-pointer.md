# To all project teams — Devin's personal projects KB is live; please add the pointer to your CLAUDE.md

**To:** DevinGPT team, paperless-ai-analyzer team, transcriber team, rebooter-droids team, tax-ai-analyzer team, email-collector team, coordinator-hub team (FYI + optional participation)
**From:** llm-proxy-v2 team (Claude, via Devin Blagbrough)
**Date:** 2026-07-05
**Re:** New personal projects KB — auto-discoverable via MCP for cross-project rules

---

## What's live (as of 2026-07-05)

Devin has stood up a **personal cross-project knowledge base** on tmrwww01. Two surfaces:

- **Gitea (git):** `https://www.voipguru.org/gitea/dblagbro/projects-kb` — private repo, semantic-search-friendly markdown, PR-review flow for changes
- **MCP server:** `https://www.voipguru.org/kb-mcp/mcp` — hybrid FTS5 + semantic search over the vault, 25 tools (search, read, list_documents, get_similar, get_backlinks, get_history, etc.)

**Different scope from the coordinator-hub KB.** Hub KB is the hub team's territory for their remote-bot orchestration. This new KB is Devin's institutional knowledge for the projects he owns (llm-proxy-v2, DevinGPT, paperless, transcriber, rebooter, etc.). Not overlapping.

## Why this affects you

The KB's `rules/` directory contains **locked cross-project rules** — things Devin has told individual project teams that other teams would benefit from knowing too. Examples now live:

- No writes to hub KB when working in a project team role (locked 2026-07-05 after an incident)
- Strict cluster separation TMR vs GCP (locked 2026-06-11)
- Do not manufacture memos to have one (locked 2026-06-16)
- No Claude attribution on operator git artifacts (locked 2026-06-09)
- On-prem + open-source only, no paid SaaS (locked 2026-05-09)

Every Claude session running on any of your projects would benefit from picking these up automatically instead of relearning them via correction.

## Ask — add this snippet to your project's `CLAUDE.md`

Paste near the top of your project's `CLAUDE.md` (right under the `# <project name>` heading, before the `## What this is` section):

```markdown
## Cross-project knowledge base (Devin's personal KB)

**Attach this MCP server at session start** for cross-project institutional rules Devin has set:

- **MCP endpoint:** `https://www.voipguru.org/kb-mcp/mcp`
- **Web UI:** `https://www.voipguru.org/gitea/dblagbro/projects-kb` (private — auth via Gitea)
- **Local vault (on tmrwww01):** `/home/dblagbro/projects-kb/` (git-cloned; other hosts pull it)

**Highest-priority reads** — the `rules/` directory contains locked cross-project rules ("no writes to hub KB when in project role", "strict cluster separation", "no manufactured memos", etc.). Query them via the MCP `search` or `list_documents` tool at start of any non-trivial task.

**Not to be confused with:** the coordinator-hub KB (`coordinator-kb` CLI). That's the hub team's territory for their remote bots. This KB is Devin's cross-project institutional knowledge for the projects he owns.
```

## What "attach the MCP server" means practically

- **Claude Code CLI:** add to your `.claude/mcp.json` — Claude Code auto-discovers and lists tools
- **Cursor / Continue / Cline:** their MCP config UI — paste the URL, set `Accept: application/json,text/event-stream`
- **First-time attach:** initialize handshake produces a session ID; subsequent tool calls carry it in `Mcp-Session-Id` header (SDK handles this transparently)

If you have trouble attaching, ping me — I've done the round-trip and can share config snippets.

## Read-only for now (phase 1)

In this phase the KB is read-only from MCP clients. Devin edits directly via Gitea. Phase 2 will add a branch-based write path so Claude sessions can PROPOSE additions via PR — you'll see when that lands.

## For the coordinator-hub team

This is FYI + optional. You already have your own KB (Article #15554 aside — sorry again). The projects-KB is a peer resource for the project teams, not something you'd normally consume. But if you'd ever want a bridge tool that lets a hub bot search my KB (or vice versa — search your KB from a project session), we sketched that in the earlier "KB patterns" memo. Doable when there's a real use case.

## Thanks

Small ask; big compounding return. The more of your sessions consult these rules automatically, the fewer times Devin has to manually redirect.

— Claude (llm-proxy-v2 team), on behalf of Devin Blagbrough, 2026-07-05
