"""Run runtime ORM models (v3.0 — coordinator-hub spec, R1).

Split out from ``db.py`` in v4.4.11. Owns the 4 tables that back the
server-mediated agent loop:

- ``Run`` — the run row itself + its state machine columns.
- ``RunMessage`` — conversation history for a run.
- ``RunEvent`` — SSE event ring buffer.
- ``RunIdempotency`` — (api_key_id, idempotency_key) → run_id map.
"""
from sqlalchemy import Column, String, Integer, Float, Text, JSON, ForeignKey

from app.models.db_base import Base


class Run(Base):
    """A server-mediated agent loop scoped to one hub task.

    State machine (per spec B.1):
      queued → running → requires_tool → running → ... → completed
              ↘ failed  ↘ expired  ↘ cancelled

    See app/runs/state.py for the FSM. Persistence is per-row; transitions
    bump ``updated_at`` so cluster sync can replicate via last-write-wins.
    """
    __tablename__ = "runs"

    id = Column(String, primary_key=True)         # 'run_' + 16 hex chars
    api_key_id = Column(String, nullable=False, index=True)
    owner_node_id = Column(String, nullable=False)  # which node spawned the worker
    status = Column(String, nullable=False, default="queued")
    current_step = Column(String, nullable=True)    # model_call|tool_dispatch|tool_wait|complete|fail
    deadline_ts = Column(Float, nullable=False)
    max_turns = Column(Integer, nullable=False)
    model_preference = Column(JSON, default=list)   # ordered list of model ids
    compaction_model = Column(String, nullable=True)
    system_prompt = Column(Text, nullable=True)
    tools_spec = Column(JSON, default=list)         # Anthropic-format tool schemas
    metadata_json = Column(JSON, default=dict)
    trace_id = Column(String, nullable=True)        # OTEL parent span id (top-level on create)
    # Counters / accounting
    model_calls = Column(Integer, default=0)
    tool_calls = Column(Integer, default=0)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    last_provider_id = Column(String, nullable=True)
    context_summarized_at_turn = Column(Integer, nullable=True)
    # Pending tool_use waiting for /tool_result
    current_tool_use_id = Column(String, nullable=True)
    current_tool_name = Column(String, nullable=True)
    current_tool_input = Column(JSON, nullable=True)
    # Terminal payloads
    result_text = Column(Text, nullable=True)
    error_kind = Column(String, nullable=True)      # error_provider|tool_loop_exceeded|context_exhausted|...
    error_message = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False)      # Unix; matches idempotency TTL anchor
    updated_at = Column(Float, nullable=False)      # bumped on every transition
    completed_at = Column(Float, nullable=True)


class RunMessage(Base):
    """Conversation history for a Run, ordered by ``seq``.

    Stored verbatim in Anthropic Messages format (role + content blocks).
    Compaction replaces a span of messages with a single 'assistant'
    summary message — see ``compacted_from_seq``/``compacted_to_seq``."""
    __tablename__ = "run_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)           # monotonic per run, dense
    role = Column(String, nullable=False)           # system|user|assistant
    content = Column(JSON, nullable=False)          # str or list[block]
    tokens = Column(Integer, default=0)             # estimate, for compaction trigger
    compacted_from_seq = Column(Integer, nullable=True)  # if this row is a summary
    compacted_to_seq = Column(Integer, nullable=True)
    created_at = Column(Float, nullable=False)


class RunEvent(Base):
    """SSE event ring buffer for a Run. Last 1000 per run kept; older pruned.

    ``seq`` is monotonic per run; SSE clients resume via ``Last-Event-ID``.
    Event ``kind`` matches the spec table (run_started, model_call_start, ...).
    """
    __tablename__ = "run_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)
    payload = Column(JSON, default=dict)
    ts = Column(Float, nullable=False)


class RunIdempotency(Base):
    """``(api_key_id, idempotency_key)`` → ``run_id`` map.

    24h TTL from ``created_at``; lookups beyond TTL miss and a new Run is
    created. Domain is per-API-key per the locked Q1 decision.
    """
    __tablename__ = "run_idempotency"

    api_key_id = Column(String, primary_key=True)
    idempotency_key = Column(String, primary_key=True)  # caller-supplied; ≤256 chars
    run_id = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)
