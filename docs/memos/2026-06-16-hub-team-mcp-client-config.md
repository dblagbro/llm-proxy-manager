# MEMO — Coordinator-Hub Team

**From:** Devin Blagbrough (via llm-proxy team draft)
**Date:** 2026-06-16
**Subject:** llm-proxy2 MCP endpoint live — request: add per-bot client-side MCP config to coordinator-hub installer

---

## TL;DR

llm-proxy2 now exposes an aggregated MCP (Model Context Protocol) endpoint at `https://www.voipguru.org/llm-proxy2/mcp/`. Every bot in the fleet can register this as a single MCP server and immediately get the file-handling tools the bots have been requesting (Excel, PDF, DOCX, PPTX, HTML, EPUB → markdown, plus URL fetch). Bots currently can't reach it because their MCP client config doesn't list it. **Ask:** add a one-time block to the installer that writes the correct config file for whichever client the bot uses, with the bot's existing llm-proxy2 API key as the bearer token. Per-client snippets below, ready to drop in.

This is the client-side "Path A" of a two-path rollout. Path B (proxy-side auto-injection on `/v1/messages` non-streaming) shipped in v5.7.1 and works for every bot today with zero config change — Path A adds Claude Code / opencode / Cursor's own UI and per-tool audit story on the client side, which is what some bots have been asking for.

---

## What's live on the proxy side

- **Endpoint:** `https://www.voipguru.org/llm-proxy2/mcp/` (trailing slash required — bare `/mcp` returns 404 since v5.7.2)
- **Transport:** Streamable HTTP (the 2025-11-25 MCP spec default; SSE deprecated)
- **Auth:** `Authorization: Bearer <api_key>` — reuses the EXISTING llm-proxy2 API key each bot already has. No new key, no new IdP.
- **Tools currently registered (visible to every authed bot):**
  - `read_xlsx_to_markdown` — Excel files → markdown tables. Accepts `file_b64` or `url`.
  - `fetch_url` — HTTPS GET with 5 MB cap, 30s timeout. Returns body as text.
  - `convert_document_to_markdown` — DOCX / PDF / PPTX / HTML / EPUB / CSV / MD / TXT / JPG / PNG / ODT → markdown. Same input contract as the xlsx tool. Microsoft `markitdown` under the hood.
- **Audit:** every tool call writes one row to `mcp_tool_calls` table (api_key_id, tool_name, latency, ok/error) — operator-visible via the proxy admin UI.
- **Health endpoint:** `GET /health` includes `workers[]` and `dbPool` already; v5.7.x adds an `mcp` block in v5.7.5.

Server name to register in client config: `llm-proxy2`. Suggest the bot use **whatever name the operator picks** if you want consistency across the fleet; clients don't care about the registration name.

---

## Config snippets per bot client

### Claude Code (`~/.claude/mcp.json` or per-project `.mcp.json`)

```jsonc
{
  "mcpServers": {
    "llm-proxy2": {
      "type": "http",
      "url": "https://www.voipguru.org/llm-proxy2/mcp/",
      "headers": {
        "Authorization": "Bearer ${LLM_PROXY_API_KEY}"
      }
    }
  }
}
```

Note: Claude Code reads the JSON at startup. `${LLM_PROXY_API_KEY}` must be set in the bot's environment before Claude Code launches. The installer already plumbs `LLM_PROXY_API_KEY` via `~/.claude/coordinator.env` — that env var should propagate through if Claude Code is launched from a shell that sources the env.

### opencode (`~/.config/opencode/opencode.json` or per-project `opencode.json`)

```jsonc
{
  "mcp": {
    "llm-proxy2": {
      "type": "remote",
      "url": "https://www.voipguru.org/llm-proxy2/mcp/",
      "headers": {
        "Authorization": "Bearer ${LLM_PROXY_API_KEY}"
      }
    }
  }
}
```

Same env var plumbing applies.

### Cursor (`~/.cursor/mcp.json`)

```jsonc
{
  "mcpServers": {
    "llm-proxy2": {
      "url": "https://www.voipguru.org/llm-proxy2/mcp/",
      "headers": {
        "Authorization": "Bearer ${LLM_PROXY_API_KEY}"
      }
    }
  }
}
```

### Continue (`~/.continue/config.yaml`)

```yaml
mcpServers:
  - name: llm-proxy2
    transport:
      type: streamable-http
      url: https://www.voipguru.org/llm-proxy2/mcp/
      requestOptions:
        headers:
          Authorization: "Bearer ${LLM_PROXY_API_KEY}"
```

### Cline (VS Code extension settings JSON)

```jsonc
{
  "cline.mcpServers": {
    "llm-proxy2": {
      "url": "https://www.voipguru.org/llm-proxy2/mcp/",
      "headers": {
        "Authorization": "Bearer ${LLM_PROXY_API_KEY}"
      }
    }
  }
}
```

---

## Installer block (suggested shape)

The installer already knows which CLI is being installed (see your existing `case` for `claude` vs `opencode` vs the cutover branches). One additional block per case:

```bash
# inside the existing per-CLI install case
case "$BOT_CLI" in
  claude)
    install -m 0644 /dev/stdin "$HOME/.claude/mcp.json" <<EOF
{
  "mcpServers": {
    "llm-proxy2": {
      "type": "http",
      "url": "https://www.voipguru.org/llm-proxy2/mcp/",
      "headers": { "Authorization": "Bearer \${LLM_PROXY_API_KEY}" }
    }
  }
}
EOF
    ;;
  opencode)
    install -m 0644 /dev/stdin "$HOME/.config/opencode/opencode.json.d/llm-proxy2-mcp.json" <<EOF
{
  "mcp": {
    "llm-proxy2": {
      "type": "remote",
      "url": "https://www.voipguru.org/llm-proxy2/mcp/",
      "headers": { "Authorization": "Bearer \${LLM_PROXY_API_KEY}" }
    }
  }
}
EOF
    ;;
  # ... other CLIs as the cutover continues
esac
```

(The exact filename / merge semantics differs per CLI — opencode supports per-file fragments under a `.d/` directory in some configs; Claude Code merges from a single file. Use whichever your existing per-CLI install path supports.)

## Verification — per-bot quick check after install

Once configured, from inside the bot session:

- Claude Code: `/mcp` → should list `llm-proxy2` as connected with N tools available. `/mcp llm-proxy2 status` shows last error if any.
- opencode: `opencode mcp list` (if your version supports it) or check `~/.config/opencode/state.json` for the connection state.
- Cursor: Settings → MCP → check the green dot next to `llm-proxy2`.

End-to-end smoke test from any bot:

```
> Use the read_xlsx_to_markdown tool to summarize this file: <some.xlsx>
```

If the tool runs, you'll see the markdown table in the response and a corresponding row in the proxy's `mcp_tool_calls` table (admin UI shows this under the MCP dashboard in v5.7.5+).

If the bot says "I don't have access to that tool," the MCP config didn't register — check the bot's env for `LLM_PROXY_API_KEY` and run the client's own MCP debug command.

---

## Backwards compatibility

- Bots that don't get the Path A config still work — Path B (proxy-side auto-injection on non-streaming `/v1/messages`) already gives them the same tools transparently.
- The two paths are additive, not exclusive. A bot with Path A configured AND making non-streaming `/v1/messages` calls will see the tool twice (once via MCP client surface, once auto-injected). The proxy de-dupes by tool name on the injection side, so no double-call risk.

## Rollout suggestion

1. Drop the per-CLI installer blocks above into your existing case statement.
2. Roll to one bot first (preferably a low-blast-radius lab bot). Verify with the smoke test.
3. Roll fleet-wide.
4. Tell us when each tranche is rolled and we'll watch `mcp_tool_calls` row counts climb correspondingly.

## Open items / questions from our side

1. Do any bots already have a `~/.claude/mcp.json` or equivalent for OTHER MCP servers? If so, the install needs to merge JSON (jq?) rather than overwrite. Our snippets assume a fresh write.
2. Do you want the proxy to expose a separate per-bot API key for MCP, or reuse the existing one? We've defaulted to reuse since every bot already has it and the MCP server is just another endpoint on the same proxy. Tell us if you want isolation.
3. Are there bots in the fleet running a CLI we haven't listed above (e.g. Aider, Continue, Cline)? We'll add the snippets when you tell us which ones.

Reach out via the operator (Devin) for any clarification; we don't have a direct channel to the coordinator-hub team.

— llm-proxy v5.7.3
