"""v5.0.0 compliance — semantic cache source_company filtering.

Covers ``app.cache.semantic.SemanticCache.check()``:
- Hits whose ``source_company`` is in the blocklist are dropped.
- Hits whose ``source_company`` is None are also dropped when the
  blocklist is non-empty (decision 7: NULL = unknown = banned).
- Empty/None blocklist returns hits regardless of ``source_company``
  (legacy behavior — no compliance overhead when feature unused).

These tests stub ``_ensure_init`` + ``_embed`` so we don't need a live
Redis or embedding provider. ``self._index.query`` returns synthetic
rows that mimic the RedisVL ``Document`` shape (``.get`` lookups).
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stub_redisvl(monkeypatch):
    """The check() body does ``from redisvl.query import VectorQuery`` /
    ``from redisvl.query.filter import Tag`` inside the try-block.
    redisvl isn't installed in the unit test env, so without these stubs
    the ImportError gets swallowed and check() always returns None. We
    install no-op shims that accept any kwargs and return objects whose
    behavior the test doesn't observe (the stubbed ``_index.query`` is
    what produces results)."""
    redisvl_mod = types.ModuleType("redisvl")
    redisvl_query_mod = types.ModuleType("redisvl.query")
    redisvl_query_filter_mod = types.ModuleType("redisvl.query.filter")

    class _VectorQuery:
        def __init__(self, *a, **kw):
            self.args = a
            self.kwargs = kw

    class _Tag:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return ("tag", self.name, other)

    redisvl_query_mod.VectorQuery = _VectorQuery
    redisvl_query_filter_mod.Tag = _Tag
    redisvl_mod.query = redisvl_query_mod

    monkeypatch.setitem(sys.modules, "redisvl", redisvl_mod)
    monkeypatch.setitem(sys.modules, "redisvl.query", redisvl_query_mod)
    monkeypatch.setitem(sys.modules, "redisvl.query.filter", redisvl_query_filter_mod)


class _StubIndex:
    """Minimal stand-in for ``redisvl.index.AsyncSearchIndex`` — only the
    ``.query()`` method is exercised here."""
    def __init__(self, rows):
        self._rows = rows

    async def query(self, vq):
        # SemanticCache.check() doesn't introspect the VectorQuery; it
        # just consumes the returned list, treating each item as a dict.
        return list(self._rows)


def _make_cache(rows):
    """Build a SemanticCache pre-wired with synthetic results + a stubbed
    embed step. Returns the cache."""
    from app.cache.semantic import SemanticCache

    cache = SemanticCache()
    cache._init_attempted = True
    cache._init_ok = True
    cache._index = _StubIndex(rows)

    async def _fake_embed(_text):
        return [0.0]

    cache._embed = _fake_embed  # type: ignore[assignment]
    return cache


def _hit_row(source_company, distance=0.0):
    """One synthetic RedisVL result row. ``distance=0`` ⇒ similarity=1.0,
    safely above the default 0.9-ish threshold."""
    return {
        "response": f"cached answer from {source_company!r}",
        "prompt": "irrelevant",
        "source_company": source_company,
        "vector_distance": distance,
    }


# ── Decision 7: banned company drops the hit ──────────────────────────


@pytest.mark.asyncio
async def test_check_drops_hit_when_source_company_banned():
    cache = _make_cache([_hit_row("anthropic")])
    out = await cache.check(
        "ns-1", "what time is it?", 0.5,
        blocked_companies={"anthropic"},
    )
    assert out is None


# ── Decision 7: NULL source_company = banned with non-empty list ──────


@pytest.mark.asyncio
async def test_check_drops_hit_when_source_company_is_null_and_blocklist_nonempty():
    """The whole point of decision 7 — pre-v5 cache rows have no
    ``source_company`` tag, so we MUST NOT serve them to a key that
    has any blocklist policy. Otherwise the upgrade silently leaks
    banned content for the rebuild window."""
    cache = _make_cache([_hit_row(None)])
    out = await cache.check(
        "ns-1", "q", 0.5,
        blocked_companies={"anthropic"},
    )
    assert out is None


# ── Empty blocklist: hits pass through regardless of source_company ──


@pytest.mark.asyncio
async def test_check_returns_hit_when_blocklist_empty_even_if_null_company():
    cache = _make_cache([_hit_row(None)])
    out = await cache.check("ns-1", "q", 0.5, blocked_companies=None)
    assert out is not None
    response_text, similarity = out
    assert "cached answer" in response_text
    assert similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_check_returns_hit_when_blocklist_empty_set():
    """Empty set behaves identically to None — no filter is in force."""
    cache = _make_cache([_hit_row("openai")])
    out = await cache.check("ns-1", "q", 0.5, blocked_companies=set())
    assert out is not None


# ── Mixed candidates: drop the banned one, keep the legit runner-up ──


@pytest.mark.asyncio
async def test_check_skips_banned_row_and_serves_legitimate_runner_up():
    """When the top hit is banned but a runner-up has a known-good
    company, ``check()`` MUST serve the runner-up rather than miss.
    Otherwise a single bad row at the top would starve the cache for
    every banned key."""
    cache = _make_cache([
        _hit_row("anthropic", distance=0.0),  # banned — drop
        _hit_row("openai", distance=0.05),    # legit — keep
    ])
    out = await cache.check(
        "ns-1", "q", 0.5,
        blocked_companies={"anthropic"},
    )
    assert out is not None
    response_text, _ = out
    assert "openai" in response_text


# ── Source-level guards: schema + return_fields wiring ────────────────


def test_index_schema_includes_source_company_tag():
    from app.cache.semantic import _INDEX_SCHEMA
    names = {f["name"] for f in _INDEX_SCHEMA["fields"]}
    assert "source_company" in names
    sc = next(f for f in _INDEX_SCHEMA["fields"] if f["name"] == "source_company")
    assert sc["type"] == "tag", "source_company must be a TAG field for Redis filter syntax"


def test_check_signature_accepts_blocked_companies():
    """Compile-time guard: the new kwarg must be reachable from the
    middleware caller (keyword-only)."""
    import inspect
    from app.cache.semantic import SemanticCache
    sig = inspect.signature(SemanticCache.check)
    assert "blocked_companies" in sig.parameters
    assert sig.parameters["blocked_companies"].kind == inspect.Parameter.KEYWORD_ONLY


def test_store_signature_accepts_source_company():
    import inspect
    from app.cache.semantic import SemanticCache
    sig = inspect.signature(SemanticCache.store)
    assert "source_company" in sig.parameters
    assert sig.parameters["source_company"].kind == inspect.Parameter.KEYWORD_ONLY
