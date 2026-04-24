"""Unit tests for model alias resolver."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.routing.aliases import resolve_alias


class _FakeAliasRow:
    def __init__(self, alias, provider_id, model_id):
        self.alias = alias
        self.provider_id = provider_id
        self.model_id = model_id


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, row):
        self._row = row
        self.last_query = None

    async def execute(self, query):
        self.last_query = query
        return _FakeScalarResult(self._row)


class TestResolveAlias:
    @pytest.mark.asyncio
    async def test_none_model_returns_none(self):
        db = _FakeDB(row=None)
        assert await resolve_alias(db, None) is None

    @pytest.mark.asyncio
    async def test_empty_model_returns_none(self):
        db = _FakeDB(row=None)
        assert await resolve_alias(db, "") is None

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        db = _FakeDB(row=None)
        got = await resolve_alias(db, "claude-opus-4")
        assert got is None

    @pytest.mark.asyncio
    async def test_match_returns_row(self):
        row = _FakeAliasRow("gpt-4o-main", "prov-1", "gpt-4o")
        db = _FakeDB(row=row)
        got = await resolve_alias(db, "gpt-4o-main")
        assert got is row
        assert got.provider_id == "prov-1"
        assert got.model_id == "gpt-4o"

    @pytest.mark.asyncio
    async def test_query_is_executed_for_non_empty(self):
        db = _FakeDB(row=None)
        await resolve_alias(db, "some-alias")
        assert db.last_query is not None
