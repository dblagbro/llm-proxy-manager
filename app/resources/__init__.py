"""Local accelerator resource telemetry and (later) admission.

This package is **resource admission / accelerator telemetry** — whether
the host can physically serve a local model right now. It is not the MCP
capability-signalling "back-pressure" in
``docs/5.10-mcp-backpressure-design.md``.

Slice 1 (this package's first landing) is read-only: ``probe.py`` plus
``GET /api/local/accelerators``. No request is refused here.
"""
