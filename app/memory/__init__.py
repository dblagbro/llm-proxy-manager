"""v3.8.7+ (#267) — proxy-side caller memory king-store.

This package implements the cross-provider memory layer described in
docs/rfc/2026-05-proxy-memory-store.md.

Modules:
- ``store`` — read/write layer with Redis hot cache + SQLite durable
  + in-process fallback (mirrors the app/cot/session.py pattern).

Future:
- ``inject`` (Phase 4) — request-time memory injection middleware
- ``flush`` (Phase 6) — vendor-specific provider-side flush handlers
- ``recover`` (Phase 7) — back-pressure recovery from upstream
"""
