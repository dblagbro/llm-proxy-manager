"""v5.7.0 — MCP tool-call audit table.

Owns the ``mcp_tool_calls`` table. One row per MCP tool invocation
through the ``/mcp`` aggregation endpoint. v5.7.x will migrate this
into ``compliance_events`` (for cluster replication); shipping as a
dedicated table first keeps the v5.7.0 risk surface tight (no
``compliance_events`` schema change, no audit-chain interaction).

Schema fields:
- ``api_key_id`` — which key initiated the call (auth'd via bearer).
- ``parent_request_id`` — optional FK back to the /v1/messages
  request that spawned this. NULL when the MCP endpoint was reached
  directly from the client (the v5.7.0 case).
- ``tool_name`` — e.g. ``read_xlsx_to_markdown``, ``fetch_url``.
- ``mcp_server_id`` — ``in-process`` in v5.7.0; v5.7.1 fills with the
  prefix the FastMCP root assigned each mounted sub-server.
- ``input_summary`` — short string for inspection (NOT the full
  payload; the model could leak PII otherwise). Capped at 480 chars.
- ``output_bytes`` — size of the tool's return value in bytes.
- ``latency_ms`` — end-to-end tool execution time.
- ``ok`` — boolean success flag.
- ``error_msg`` — capped exception message on failure.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.models.db_base import Base


class McpToolCall(Base):
    __tablename__ = "mcp_tool_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    api_key_id = Column(String, nullable=True, index=True)
    parent_request_id = Column(String, nullable=True, index=True)
    tool_name = Column(String, nullable=False, index=True)
    mcp_server_id = Column(String, nullable=False, default="in-process")
    input_summary = Column(Text, nullable=True)
    output_bytes = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    ok = Column(Boolean, nullable=False, default=False, index=True)
    error_msg = Column(Text, nullable=True)
