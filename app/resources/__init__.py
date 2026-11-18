"""Local accelerator resource telemetry and admission.

This package is **resource admission / accelerator telemetry** — whether
the host can physically serve a local model right now. It is not the MCP
capability-signalling "back-pressure" in
``docs/5.10-mcp-backpressure-design.md``.

- Slice 1: read-only ``probe.py`` plus ``GET /api/local/accelerators``.
- Slice 4 skeleton: ``admission.py`` / ``errors.py`` 429/503 + Retry-After
  contract only. No request is refused yet.
"""
