# GEMINI.md — Gemini CLI adapter

The canonical project contract is `AGENTS.md`. Read it first.

@AGENTS.md

## Gemini-specific notes
- Treat `AGENTS.md` as authoritative for commands, boundaries, definition of done, and the
  prohibited/approval-required list. Nothing here overrides it.
- This repo's reusable procedures live as Agent Skills in `.agents/skills/` (portable, not
  Claude-only). Consult the relevant `SKILL.md` before starting a matching task.
- Specialized agent roles and the model-routing policy are documented in `docs/agent-system.md`.
- Same guardrails apply regardless of tool: no push/tag/deploy without approval; never stop the
  full Docker stack or destroy volumes; no secrets in code or logs.
