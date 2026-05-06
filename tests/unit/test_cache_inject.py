"""Unit tests for the LMRH 1.2 §E2 cache-mode dim implementation
(``app/api/_cache_inject.py``).

Covers:
- ``parse_cache_mode`` parsing of all spec-listed mode tokens + synonyms
- ``;require`` modifier capture
- ``cache=ephemeral`` forces inject below default threshold
- ``cache=none|off|disabled`` suppresses inject
- absence of dim → auto (default heuristic)
- back-compat ``caller_opted_out`` shim still works
"""
from app.api._cache_inject import (
    CacheModeDecision,
    caller_opted_out,
    inject_cache_control,
    parse_cache_mode,
    resolve_min_chars,
)


# ── parse_cache_mode ────────────────────────────────────────────────────────

def test_parse_cache_mode_absent_defaults_to_auto():
    d = parse_cache_mode(None)
    assert d.mode == "auto"
    assert d.required is False
    assert d.force_below_threshold is False


def test_parse_cache_mode_empty_string():
    assert parse_cache_mode("").mode == "auto"


def test_parse_cache_mode_explicit_auto():
    d = parse_cache_mode("cache=auto")
    assert d.mode == "auto"
    assert d.force_below_threshold is False


def test_parse_cache_mode_ephemeral():
    d = parse_cache_mode("cache=ephemeral")
    assert d.mode == "ephemeral"
    assert d.force_below_threshold is True


def test_parse_cache_mode_ephemeral_with_require():
    d = parse_cache_mode("cache=ephemeral;require")
    assert d.mode == "ephemeral"
    assert d.required is True


def test_parse_cache_mode_none():
    assert parse_cache_mode("cache=none").mode == "none"


def test_parse_cache_mode_off_synonym():
    assert parse_cache_mode("cache=off").mode == "none"


def test_parse_cache_mode_disabled_synonym():
    assert parse_cache_mode("cache=disabled").mode == "none"


def test_parse_cache_mode_persistent_falls_back_to_auto():
    """LMRH 1.2 §E2: persistent is reserved; Anthropic doesn't ship it.
    Don't break callers that probe for it — fall through to auto."""
    assert parse_cache_mode("cache=persistent").mode == "auto"


def test_parse_cache_mode_unknown_value_falls_back_to_auto():
    assert parse_cache_mode("cache=lemon-curd").mode == "auto"


def test_parse_cache_mode_alongside_other_dims():
    """The cache dim must be readable when not the only dim."""
    h = "task=reasoning, cost=economy, cache=ephemeral, region=us"
    d = parse_cache_mode(h)
    assert d.mode == "ephemeral"


def test_parse_cache_mode_alongside_multivalue_dim():
    """v3.0.68 fixed comma-list parsing for provider-hint;
    cache mode must still parse correctly when sharing the header."""
    h = "provider-hint=claude-oauth,codex-oauth, cache=none;require"
    d = parse_cache_mode(h)
    assert d.mode == "none"
    assert d.required is True


# ── resolve_min_chars ───────────────────────────────────────────────────────

def test_resolve_min_chars_auto_uses_default():
    d = parse_cache_mode("cache=auto")
    assert resolve_min_chars(d) == 4000
    assert resolve_min_chars(d, default=8000) == 8000


def test_resolve_min_chars_ephemeral_is_zero():
    """cache=ephemeral forces inject regardless of size."""
    d = parse_cache_mode("cache=ephemeral")
    assert resolve_min_chars(d) == 0
    assert resolve_min_chars(d, default=99999) == 0


# ── inject_cache_control end-to-end behavior with mode wiring ──────────────

def test_inject_with_ephemeral_wraps_small_system():
    """A 200-char system prompt is normally below the 4000-char default
    threshold, so auto wouldn't wrap it. With cache=ephemeral, it should
    wrap regardless — proves the min_chars=0 plumbing works."""
    body = {"system": "you are helpful."}
    decision = parse_cache_mode("cache=ephemeral")
    out, injected = inject_cache_control(
        body, "claude-oauth", min_chars=resolve_min_chars(decision),
    )
    sys_field = out["system"]
    assert isinstance(sys_field, list)
    assert sys_field[0]["cache_control"] == {"type": "ephemeral"}
    assert injected is True


def test_inject_auto_below_threshold_does_not_wrap():
    """cache=auto on a small prompt → no wrap (default 4000-char threshold).
    Confirms we didn't accidentally make ephemeral the universal default."""
    body = {"system": "you are helpful."}
    decision = parse_cache_mode("cache=auto")
    out, injected = inject_cache_control(
        body, "claude-oauth", min_chars=resolve_min_chars(decision),
    )
    assert out["system"] == "you are helpful."  # unchanged
    assert injected is False


def test_inject_none_mode_in_callsite_pattern():
    """Replicates the real callsite pattern: when mode is 'none', the
    caller skips the inject_cache_control() call entirely. Test the
    decision plumbing makes that easy."""
    decision = parse_cache_mode("cache=none;require")
    # Real callsites use `if cache_decision.mode != "none"` to gate the call
    assert decision.mode == "none"
    assert decision.required is True


def test_inject_with_ephemeral_above_threshold_still_wraps_one_block():
    """Sanity: ephemeral mode shouldn't double-wrap or skip a normally
    wrappable system prompt."""
    body = {"system": "x" * 5000}
    decision = parse_cache_mode("cache=ephemeral")
    out, injected = inject_cache_control(
        body, "claude-oauth", min_chars=resolve_min_chars(decision),
    )
    sys_field = out["system"]
    assert isinstance(sys_field, list)
    assert len(sys_field) == 1
    assert sys_field[0]["cache_control"] == {"type": "ephemeral"}
    assert injected is True


# v3.0.83 — explicit tests for the injected boolean
def test_inject_returns_false_for_non_anthropic_provider():
    body = {"system": "x" * 5000}
    out, injected = inject_cache_control(body, "openai", min_chars=4000)
    assert out is body  # passthrough, not even copied
    assert injected is False


def test_inject_returns_false_when_caller_already_has_cache_control():
    body = {"system": [
        {"type": "text", "text": "x" * 5000, "cache_control": {"type": "ephemeral"}},
    ]}
    out, injected = inject_cache_control(body, "claude-oauth", min_chars=4000)
    assert injected is False  # caller's cache_control wins; we don't double-wrap
    assert out["system"] == body["system"]


# ── caller_opted_out back-compat shim ──────────────────────────────────────

def test_caller_opted_out_back_compat_none():
    assert caller_opted_out("cache=none") is True
    assert caller_opted_out("cache=off") is True
    assert caller_opted_out("cache=disabled") is True


def test_caller_opted_out_back_compat_not_opted_out():
    assert caller_opted_out(None) is False
    assert caller_opted_out("cache=auto") is False
    assert caller_opted_out("cache=ephemeral") is False
    assert caller_opted_out("task=reasoning") is False


def test_caller_opted_out_with_require_modifier():
    """Modifier on the dim doesn't change opt-out classification."""
    assert caller_opted_out("cache=none;require") is True


# ── builtin dim registration (regression on unknown-dim warning) ──────────

def test_cache_is_in_builtin_dim_names():
    """Regression: pre-v3.0.69 callers using cache=ephemeral got
    X-LMRH-Warnings: unknown-dim:cache despite the proxy already
    acting on the dim. Adding cache to the builtin set silences that."""
    from app.api.lmrh import _builtin_dim_names
    assert "cache" in _builtin_dim_names()


# ── v3.0.87 — build_cache_disclosure helper (LMRH 1.2 §E2) ───────────────────

def test_build_disclosure_no_hint_no_injection_returns_empty():
    from app.api._cache_inject import build_cache_disclosure
    decision = parse_cache_mode(None)
    parts = build_cache_disclosure(
        llm_hint=None, cache_decision=decision,
        cache_injected=False, served_provider_type="claude-oauth",
        usage=None,
    )
    assert parts == []


def test_build_disclosure_injection_only():
    """Auto-injection happened; no caller cache= dim. Single line."""
    from app.api._cache_inject import build_cache_disclosure
    parts = build_cache_disclosure(
        llm_hint="task=analysis", cache_decision=parse_cache_mode(None),
        cache_injected=True, served_provider_type="claude-oauth",
        usage=None,
    )
    assert parts == ["cache-injected=?1"]


def test_build_disclosure_with_caller_dim_and_injection():
    from app.api._cache_inject import build_cache_disclosure
    parts = build_cache_disclosure(
        llm_hint="cache=ephemeral",
        cache_decision=parse_cache_mode("cache=ephemeral"),
        cache_injected=True, served_provider_type="claude-oauth",
        usage={"cache_read_input_tokens": 18432,
               "cache_creation_input_tokens": 512},
    )
    assert parts == [
        "cache=ephemeral", "cache-injected=?1",
        "cache-tokens-read=18432", "cache-tokens-written=512",
    ]


def test_build_disclosure_zero_token_counts_not_emitted():
    from app.api._cache_inject import build_cache_disclosure
    parts = build_cache_disclosure(
        llm_hint=None, cache_decision=parse_cache_mode(None),
        cache_injected=False, served_provider_type="claude-oauth",
        usage={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    )
    assert parts == []  # no zeros surface


def test_build_disclosure_substitution_to_non_anthropic_emits_ignored():
    """Spec §E2: cache=ephemeral substituted to non-Anthropic family →
    cache=ignored (no cache-injected, no token echo since the served
    provider doesn't support cache_control)."""
    from app.api._cache_inject import build_cache_disclosure
    parts = build_cache_disclosure(
        llm_hint="cache=ephemeral;require",
        cache_decision=parse_cache_mode("cache=ephemeral;require"),
        cache_injected=False, served_provider_type="codex-oauth",
        usage=None,
    )
    assert parts == ["cache=ignored"]


def test_build_disclosure_substitution_to_openai_emits_ignored():
    from app.api._cache_inject import build_cache_disclosure
    parts = build_cache_disclosure(
        llm_hint="cache=ephemeral",
        cache_decision=parse_cache_mode("cache=ephemeral"),
        cache_injected=False, served_provider_type="vertex_ai",
        usage=None,
    )
    assert parts == ["cache=ignored"]


def test_build_disclosure_caller_none_dim_not_overridden():
    """cache=none on a non-Anthropic provider: caller wanted no inject,
    served provider doesn't inject anyway. Echo cache=none, NOT
    cache=ignored (the dim was honored — it's a no-op either way)."""
    from app.api._cache_inject import build_cache_disclosure
    parts = build_cache_disclosure(
        llm_hint="cache=none",
        cache_decision=parse_cache_mode("cache=none"),
        cache_injected=False, served_provider_type="codex-oauth",
        usage=None,
    )
    assert parts == ["cache=none"]


def test_append_cache_disclosure_appends_to_existing_capability():
    from app.api._cache_inject import append_cache_disclosure
    headers = {"LLM-Capability": "v=1, provider=Foo, model=x"}
    append_cache_disclosure(headers, ["cache-injected=?1", "cache-tokens-read=1024"])
    assert headers["LLM-Capability"] == (
        "v=1, provider=Foo, model=x, cache-injected=?1, cache-tokens-read=1024"
    )


def test_append_cache_disclosure_creates_when_missing():
    from app.api._cache_inject import append_cache_disclosure
    headers = {}
    append_cache_disclosure(headers, ["cache-injected=?1"])
    assert headers["LLM-Capability"] == "cache-injected=?1"


def test_append_cache_disclosure_empty_parts_noop():
    from app.api._cache_inject import append_cache_disclosure
    headers = {"LLM-Capability": "v=1"}
    append_cache_disclosure(headers, [])
    assert headers["LLM-Capability"] == "v=1"
