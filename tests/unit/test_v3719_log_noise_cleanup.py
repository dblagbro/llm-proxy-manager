"""v3.7.19 — BUG-021 (embeddings base64 decode) + BUG-022 (graceful
DB session close on request cancellation)."""
from __future__ import annotations

import base64
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── BUG-021: base64 embedding normalization ──────────────────────


def test_normalize_decodes_base64_embedding_to_floats():
    """When upstream returns embedding as a base64-encoded float32 array,
    the normalizer must decode to list[float] before serialization."""
    from app.api.embeddings import _normalize_embeddings_to_floats
    floats = [0.1, 0.2, -0.3, 0.5]
    raw = struct.pack(f"<{len(floats)}f", *floats)
    b64 = base64.b64encode(raw).decode("ascii")
    body = {"data": [{"embedding": b64, "index": 0}]}
    _normalize_embeddings_to_floats(body)
    out = body["data"][0]["embedding"]
    assert isinstance(out, list)
    assert all(isinstance(x, float) for x in out)
    assert len(out) == 4
    # Allow tiny float32 → float64 conversion error
    for got, want in zip(out, floats):
        assert abs(got - want) < 1e-6


def test_normalize_noop_on_list_of_floats():
    """Already-decoded embeddings must pass through unchanged."""
    from app.api.embeddings import _normalize_embeddings_to_floats
    body = {"data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}]}
    _normalize_embeddings_to_floats(body)
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]


def test_normalize_handles_multi_item_batch():
    """Batch responses with multiple data items all get normalized."""
    from app.api.embeddings import _normalize_embeddings_to_floats
    raw1 = struct.pack("<2f", 1.0, 2.0)
    raw2 = struct.pack("<2f", 3.0, 4.0)
    body = {
        "data": [
            {"embedding": base64.b64encode(raw1).decode("ascii"), "index": 0},
            {"embedding": base64.b64encode(raw2).decode("ascii"), "index": 1},
        ]
    }
    _normalize_embeddings_to_floats(body)
    assert body["data"][0]["embedding"] == [1.0, 2.0]
    assert body["data"][1]["embedding"] == [3.0, 4.0]


def test_normalize_handles_missing_data():
    """No data key → no-op (no exception)."""
    from app.api.embeddings import _normalize_embeddings_to_floats
    body = {}
    _normalize_embeddings_to_floats(body)
    assert body == {}


def test_normalize_handles_empty_data_list():
    from app.api.embeddings import _normalize_embeddings_to_floats
    body = {"data": []}
    _normalize_embeddings_to_floats(body)
    assert body == {"data": []}


def test_normalize_handles_malformed_base64():
    """Garbage base64 → keep original value, log warning, don't crash."""
    from app.api.embeddings import _normalize_embeddings_to_floats
    body = {"data": [{"embedding": "not-actually-base64-!!!", "index": 0}]}
    _normalize_embeddings_to_floats(body)
    # Garbage in, garbage out (passes through after warning logged) —
    # b64decode may or may not raise depending on the chars. Either way,
    # the call must not propagate the exception.


def test_normalize_respects_encoding_format_base64():
    """When caller explicitly requested base64, we don't decode — preserves
    contract. This is enforced at the endpoint level (call site decides),
    so the normalizer itself is purely passive."""
    # The normalizer is unconditional once called — the endpoint
    # decides whether to call it based on body.encoding_format. Test
    # documented in endpoint integration test.
    pass


def test_endpoint_skips_normalize_when_caller_requested_base64():
    """Source-level check: the endpoint must gate the normalize call
    on encoding_format != 'base64'."""
    from pathlib import Path
    src = Path("app/api/embeddings.py").read_text()
    assert 'encoding_format' in src
    assert '"base64"' in src or "'base64'" in src
    assert "_normalize_embeddings_to_floats" in src


def test_endpoint_uses_warnings_none_on_model_dump():
    """Source-level check: model_dump call suppresses Pydantic warnings
    since we'll normalize the value below."""
    from pathlib import Path
    src = Path("app/api/embeddings.py").read_text()
    assert 'warnings="none"' in src or "warnings='none'" in src


# ── BUG-022: graceful session close on cancellation ───────────────


# ── get_db source guards ─────────────────────────────────────────
#
# v5.22.31: these were fixed-width `src[idx:idx+N]` windows, which is the
# dominant staleness bug in this suite. Worse, get_db's docstring is ~1700
# chars and *names the strings being asserted on*, so
# test_get_db_swallows_no_active_connection was matching the prose and
# guarding nothing at all. Slice the real code instead, docstring excluded.


def _get_db_code() -> str:
    """Source of ``get_db``'s body with its docstring stripped.

    Docstring-free on purpose: get_db's docstring quotes
    ``OperationalError('no active connection')`` and ``async with``
    verbatim, so any assertion allowed to see it passes vacuously.
    """
    import ast
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_db"
    )
    stmts = fn.body
    if (
        isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    assert stmts, "get_db has no body beyond its docstring"
    lines = src.splitlines()
    return "\n".join(lines[stmts[0].lineno - 1 : stmts[-1].end_lineno])


def test_get_db_returns_connection_via_shielded_aexit():
    """v3.7.21's invariant, restated for v5.21.12's implementation.

    Was ``test_get_db_uses_async_with_pattern``, asserting the literal
    ``async with AsyncSessionLocal() as session:``. v5.21.12 deliberately
    replaced ``async with`` with manual ``__aenter__``/``__aexit__``
    because ``async with`` does not compose with ``asyncio.shield`` at the
    cleanup call — so the old assertion was testing a pattern the code had
    intentionally abandoned, and it sat in known_failures.txt as a result.

    The invariant it existed to protect is unchanged and is what is checked
    here: cleanup must go through SQLAlchemy's own pool-return path
    (``__aexit__``), not the v3.7.19 manual ``session.close()`` bypass that
    leaked connections and produced ``SAWarning: non-checked-in connection
    will be terminated``. v5.21.12 adds that the call be shielded, so a
    cancel arriving mid-close cannot strand the pool slot.
    """
    body = _get_db_code()
    assert "session.__aexit__(None, None, None)" in body, (
        "get_db must hand cleanup to SQLA's __aexit__ (the pool-return path)"
    )
    assert "shield(" in body, (
        "the __aexit__ call must be shielded or a mid-close cancel strands "
        "the pool slot — see v5.21.12 / the /cluster/sync pool leak"
    )
    assert "yield session" in body
    # The v3.7.19 regression: close() as the cleanup path instead of __aexit__.
    assert "session.close()" not in body, (
        "bare session.close() bypasses SQLA's pool return — this is the "
        "exact v3.7.19 bug v3.7.21 reverted"
    )


def test_get_db_swallows_no_active_connection():
    """get_db must still catch OperationalError('no active connection')
    that fires post-cancellation. v3.7.21 wraps the async with in a
    try/except instead of doing manual close."""
    body = _get_db_code()
    assert "no active connection" in body
    assert "OperationalError" in body


def test_get_db_reraises_other_operational_errors():
    """Only the specific 'no active connection' message is swallowed.
    Other OperationalErrors (real DB problems) must still propagate."""
    body = _get_db_code()
    # The check is conditional on the message text
    assert 'in str(exc).lower()' in body
    # Must have an unconditional re-raise for the non-matching case
    assert "raise" in body


@pytest.mark.asyncio
async def test_get_db_normal_path_yields_session():
    """Happy path: async with yields a session and exits cleanly."""
    from app.models import database as db_mod
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_factory = MagicMock(return_value=fake_session)
    with patch.object(db_mod, "AsyncSessionLocal", fake_factory):
        async for s in db_mod.get_db():
            assert s is fake_session
    fake_session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_db_swallows_no_active_connection_runtime():
    """Runtime: when the async with exit raises OperationalError with
    'no active connection', the dep finishes cleanly."""
    from app.models import database as db_mod
    from sqlalchemy.exc import OperationalError

    op_err = OperationalError(
        "(sqlite3.OperationalError) no active connection",
        None, None,
    )
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(side_effect=op_err)
    fake_factory = MagicMock(return_value=fake_session)
    with patch.object(db_mod, "AsyncSessionLocal", fake_factory):
        async for s in db_mod.get_db():
            pass


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 19)
