"""Unit tests for audit log JSONL export (Wave 6)."""
import sys
import types
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

import app.monitoring.audit_export as audit_mod
from app.monitoring.audit_export import (
    _serialize_row, list_exports, prune_old_exports, export_activity_log,
)


class _FakeRow:
    def __init__(
        self,
        id=1,
        event_type="llm_request",
        severity="info",
        message="ok",
        provider_id="prov-1",
        api_key_id="key-1",
        event_meta=None,
        created_at=None,
    ):
        self.id = id
        self.event_type = event_type
        self.severity = severity
        self.message = message
        self.provider_id = provider_id
        self.api_key_id = api_key_id
        self.event_meta = event_meta or {"foo": "bar"}
        self.created_at = created_at or datetime(2026, 4, 23, tzinfo=timezone.utc)


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows
    def scalars(self):
        return self
    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
    async def execute(self, query):
        return _FakeScalars(self._rows)


@pytest.fixture
def tmp_export_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "EXPORT_DIR", tmp_path)
    yield tmp_path


# ── Serialization ────────────────────────────────────────────────────────────


class TestSerializeRow:
    def test_basic_fields(self):
        r = _FakeRow(id=42, event_type="login", severity="warning")
        out = _serialize_row(r)
        assert out["id"] == 42
        assert out["event_type"] == "login"
        assert out["severity"] == "warning"

    def test_iso_timestamp(self):
        ts = datetime(2026, 1, 15, 12, 30, 45, tzinfo=timezone.utc)
        r = _FakeRow(created_at=ts)
        out = _serialize_row(r)
        assert out["created_at"] == ts.isoformat()

    def test_none_timestamp(self):
        r = _FakeRow()
        r.created_at = None  # bypass fixture default
        out = _serialize_row(r)
        assert out["created_at"] is None

    def test_empty_metadata_becomes_dict(self):
        r = _FakeRow()
        r.event_meta = None  # bypass fixture default
        out = _serialize_row(r)
        assert out["event_meta"] == {}


# ── Export writer ────────────────────────────────────────────────────────────


class TestExportActivityLog:
    @pytest.mark.asyncio
    async def test_writes_jsonl_file(self, tmp_export_dir):
        rows = [_FakeRow(id=i) for i in range(1, 4)]
        db = _FakeDB(rows)
        result = await export_activity_log(db)
        assert result.row_count == 3
        assert result.path.exists()
        lines = result.path.read_text().strip().split("\n")
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["id"] == 1

    @pytest.mark.asyncio
    async def test_zero_rows_creates_empty_file(self, tmp_export_dir):
        db = _FakeDB([])
        result = await export_activity_log(db)
        assert result.row_count == 0
        assert result.path.exists()
        assert result.path.read_text() == ""

    @pytest.mark.asyncio
    async def test_filename_has_utc_timestamp(self, tmp_export_dir):
        db = _FakeDB([_FakeRow()])
        result = await export_activity_log(db)
        assert result.path.name.startswith("audit-")
        assert result.path.name.endswith(".jsonl")
        # Format: audit-YYYYMMDDTHHMMSSZ.jsonl
        stem = result.path.stem[len("audit-"):]
        assert stem.endswith("Z")
        assert "T" in stem

    @pytest.mark.asyncio
    async def test_bytes_written_counts_utf8(self, tmp_export_dir):
        rows = [_FakeRow(message="hello" * 10)]
        db = _FakeDB(rows)
        result = await export_activity_log(db)
        assert result.bytes_written > 0
        assert result.bytes_written == result.path.stat().st_size

    @pytest.mark.asyncio
    async def test_s3_skipped_when_no_bucket(self, tmp_export_dir):
        db = _FakeDB([_FakeRow()])
        result = await export_activity_log(db)
        assert result.s3_key is None
        assert result.s3_bucket is None

    @pytest.mark.asyncio
    async def test_keys_sorted_in_output(self, tmp_export_dir):
        """sort_keys=True ensures deterministic output for auditors."""
        db = _FakeDB([_FakeRow()])
        result = await export_activity_log(db)
        line = result.path.read_text().strip().split("\n")[0]
        # Find positions of keys — they must be in alphabetical order
        keys_in_order = [
            "api_key_id", "created_at", "event_meta", "event_type",
            "id", "message", "provider_id", "severity",
        ]
        positions = [line.index(f'"{k}"') for k in keys_in_order]
        assert positions == sorted(positions)


# ── Listing + pruning ────────────────────────────────────────────────────────


class TestListExports:
    def test_empty_when_no_exports(self, tmp_export_dir):
        assert list_exports() == []

    def test_lists_existing_files(self, tmp_export_dir):
        (tmp_export_dir / "audit-20260101T000000Z.jsonl").write_text("one\n")
        (tmp_export_dir / "audit-20260102T000000Z.jsonl").write_text("two\nthree\n")
        exports = list_exports()
        assert len(exports) == 2
        names = {e["filename"] for e in exports}
        assert names == {"audit-20260101T000000Z.jsonl", "audit-20260102T000000Z.jsonl"}

    def test_newest_first(self, tmp_export_dir):
        p1 = tmp_export_dir / "audit-20260101T000000Z.jsonl"
        p2 = tmp_export_dir / "audit-20260102T000000Z.jsonl"
        p1.write_text("")
        p2.write_text("")
        exports = list_exports()
        # sorted reverse lexically → p2 first
        assert exports[0]["filename"] == "audit-20260102T000000Z.jsonl"

    def test_ignores_non_audit_files(self, tmp_export_dir):
        (tmp_export_dir / "audit-valid.jsonl").write_text("")
        (tmp_export_dir / "README.md").write_text("")
        (tmp_export_dir / "secrets.txt").write_text("")
        names = {e["filename"] for e in list_exports()}
        assert names == {"audit-valid.jsonl"}


class TestPruneOldExports:
    def test_removes_old_files(self, tmp_export_dir):
        import os, time
        old_path = tmp_export_dir / "audit-old.jsonl"
        new_path = tmp_export_dir / "audit-new.jsonl"
        old_path.write_text("old")
        new_path.write_text("new")
        # Backdate old_path by 200 days
        old_ts = time.time() - 200 * 86400
        os.utime(old_path, (old_ts, old_ts))

        removed = prune_old_exports(retention_days=90)
        assert removed == 1
        assert not old_path.exists()
        assert new_path.exists()

    def test_returns_zero_when_all_recent(self, tmp_export_dir):
        (tmp_export_dir / "audit-a.jsonl").write_text("")
        (tmp_export_dir / "audit-b.jsonl").write_text("")
        assert prune_old_exports(retention_days=90) == 0

    def test_returns_zero_when_empty(self, tmp_export_dir):
        assert prune_old_exports(retention_days=90) == 0
