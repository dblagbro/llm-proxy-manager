"""Run runtime — server-mediated agent loop (v3.0, coordinator-hub spec).

Modules
-------
state    — pure FSM (no IO); transitions + invariants
ids      — id formatting helpers (run_id, event seq, etc.)

R2 will add ``worker``, R3 ``compaction``, R4 ``event_bus``, R5 ``cluster``.
"""
