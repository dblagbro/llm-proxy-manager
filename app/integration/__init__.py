"""v5.8.0 — AI integration protocol.

Lets other AI-driven projects discover the llm-proxy v2 capability
surface via a public ``/announce`` URL and then negotiate an API key
through a passphrase-gated AI-to-AI chat at ``/api/integration/chat``.

Security shape:

  - The integration is OFF by default (``integration_enabled=False``).
  - The chat endpoint requires the passphrase on every request — a
    constant-time compare against ``integration_passphrase``.
  - The management AI runs with a system prompt that strictly bounds
    what kinds of keys it can mint (no admin; budget hard-capped at
    ``integration_max_daily_budget_usd``).
  - Every mint is audited to ``activity_log`` (event_type =
    ``integration.key_provisioned``) with the integrating-project name.
"""

__all__ = ["announce", "chat"]
