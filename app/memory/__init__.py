"""v3.8.7+ (#267) — proxy-side caller memory king-store.

This package implements the cross-provider memory layer described in
docs/rfc/2026-05-proxy-memory-store.md.

Modules:
- ``store`` — read/write layer with Redis hot cache + SQLite durable
  + in-process fallback (mirrors the app/cot/session.py pattern).

- ``inject`` — Phase 4: request-time memory injection middleware
  (prepends a system-prompt prefix when ``X-Conversation-Id`` is set).
- ``extract`` — Phase 5: Anthropic memory-tool write-back. Scans the
  upstream response for ``tool_use`` blocks targeting the memory tool
  and persists writes to the king-store.

Future:
- ``flush`` (Phase 6) — vendor-specific provider-side flush handlers
- ``recover`` (Phase 7) — back-pressure recovery from upstream
"""
