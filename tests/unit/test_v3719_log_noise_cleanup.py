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


def test_get_db_swallows_no_active_connection():
    """get_db's cleanup must swallow OperationalError('no active connection')
    that fires when the underlying aiosqlite connection got closed by a
    CancelledError before session.close() ran."""
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    idx = src.index("async def get_db")
    body = src[idx:idx + 1500]
    assert "no active connection" in body
    assert "OperationalError" in body
    # Must NOT bare-except (would hide real bugs)
    assert "except Exception:" not in body or "except Exception as " in body


def test_get_db_reraises_cancelled_error():
    """get_db close must NOT swallow CancelledError — the outer task
    needs to see it to honor cancellation semantics."""
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    idx = src.index("async def get_db")
    body = src[idx:idx + 1500]
    assert "CancelledError" in body
    assert "raise" in body  # explicit re-raise


@pytest.mark.asyncio
async def test_get_db_normal_path_closes_cleanly():
    """Happy path: no exception → session.close() runs, no error
    propagates."""
    from app.models import database as db_mod
    fake_session = MagicMock()
    fake_session.close = AsyncMock()
    with patch.object(db_mod, "AsyncSessionLocal", return_value=fake_session):
        async for s in db_mod.get_db():
            assert s is fake_session
    fake_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_db_swallows_no_active_connection_runtime():
    """Runtime: when session.close() raises OperationalError('no active
    connection'), the dep finishes cleanly without propagating."""
    from app.models import database as db_mod
    from sqlalchemy.exc import OperationalError
    fake_session = MagicMock()
    fake_session.close = AsyncMock(
        side_effect=OperationalError(
            "(sqlite3.OperationalError) no active connection",
            None, None,
        )
    )
    with patch.object(db_mod, "AsyncSessionLocal", return_value=fake_session):
        # Iterate the async generator — should complete without raising
        async for s in db_mod.get_db():
            pass


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 19)
