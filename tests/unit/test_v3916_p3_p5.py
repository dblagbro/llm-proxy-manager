"""v3.9.16 — P3a (OpenRouter translator gap) + P3b (per-node analytics)
+ P3c (disabled-row UI badge) + P5 (Grok-Web 429 auto-skip).

Operator filed Provider Summary improvements after dashboard review.
Three independent fixes batched here:

**P3a — translator gap**: OpenRouter 86% failure rate post-v3.9.1
traced to two unhandled message shapes in
`_anthropic_blocks_to_openai_message_parts`:
  (1) `{role: "user", content: ""}` — empty user string content sent
      through unchanged; OpenAI rejects with "Invalid user message at
      index N"
  (2) `{role: "user", content: []}` or all-empty-blocks — function
      returned `[]`, silently dropping the message and shifting
      downstream indices

Fix: substitute `_EMPTY_USER_CONTENT_PLACEHOLDER` for both cases.
Preserves message position so OpenAI sees the same index numbering
the Anthropic body had.

**P3b — per-node analytics**: `event_meta.node_id` now stamped on
every activity_log row via `_build_event_meta_base`; new
`/api/providers/rolling-stats-by-node` endpoint rolls up by
`(provider_id, node_id)` for the dashboard's "Per-node" toggle.

**P3c — disabled-row UI**: Provider Summary table grays + 🔒-badges
rows whose corresponding provider has `manual_override_until` set.

**P5 — Grok-Web 429 auto-skip**: when grok-web bridge returns 429
with "cool-off N seconds remaining", parse N and set
`Provider.auto_skip_until = now + N`. Router naturally avoids the
provider during cool-off instead of cycling cached 429s.
"""
from __future__ import annotations

from pathlib import Path


# ── P3a: translator placeholders ──────────────────────────────────


def test_empty_user_content_placeholder_defined():
    from app.api._oauth_chat_translate import _EMPTY_USER_CONTENT_PLACEHOLDER
    assert isinstance(_EMPTY_USER_CONTENT_PLACEHOLDER, str)
    assert len(_EMPTY_USER_CONTENT_PLACEHOLDER) > 0


def test_translator_substitutes_placeholder_for_empty_user_string():
    """`{role: "user", content: ""}` MUST be translated to a non-empty
    content message so OpenAI doesn't reject with "invalid user message"."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": ""}],
        body_system=None,
    )
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] != ""
    assert out[0]["content"]  # truthy


def test_translator_emits_placeholder_for_empty_blocks_list():
    """An empty content list MUST emit a placeholder so message
    positions stay aligned for OpenAI's index-based diagnostics."""
    from app.api._oauth_chat_translate import _anthropic_blocks_to_openai_message_parts
    out = _anthropic_blocks_to_openai_message_parts("user", [])
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"]  # non-empty


def test_translator_emits_placeholder_for_all_empty_blocks():
    """All blocks present but all empty (text:"") → still a non-empty
    user message. Caller error pattern from real OpenRouter failures."""
    from app.api._oauth_chat_translate import _anthropic_blocks_to_openai_message_parts
    out = _anthropic_blocks_to_openai_message_parts("user", [
        {"type": "text", "text": ""},
        {"type": "text"},  # no text key
    ])
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"]


def test_translator_index_preservation_replay():
    """Replay-style test: a 5-message conversation with one
    structurally-empty user turn at index 2. Pre-v3.9.16: index shift
    would surface as 'Invalid user message at index 2' from OpenAI.
    Post-v3.9.16: 5 messages out, indices intact."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": []},   # empty list — was the trap
        {"role": "assistant", "content": "anything else?"},
        {"role": "user", "content": "thanks"},
    ]
    out = anthropic_messages_to_openai(msgs, body_system=None)
    assert len(out) == 5
    assert out[2]["role"] == "user"
    assert out[2]["content"]  # non-empty placeholder


def test_translator_unknown_content_shape_uses_placeholder_for_user():
    """Dict/int content (truly unknown shape) should still preserve
    message position with a placeholder for user role."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": {"weird": "shape"}}],
        body_system=None,
    )
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"]


# ── P3b: per-node analytics ───────────────────────────────────────


def test_event_meta_includes_node_id():
    """v3.9.16 — _build_event_meta_base stamps cluster_node_id on every
    log entry so per-node rollups are possible without a schema migration."""
    src = Path("app/monitoring/helpers.py").read_text()
    assert '"node_id":' in src
    assert "cluster_node_id" in src


def test_rolling_stats_by_node_endpoint_registered():
    """New /api/providers/rolling-stats-by-node endpoint exists. v4.4.14
    moved this endpoint from providers.py to providers_stats.py — check
    both routers."""
    from app.api.providers import router as providers_router
    from app.api.providers_stats import router as stats_router
    paths = {r.path for r in providers_router.routes} | {r.path for r in stats_router.routes}
    assert "/api/providers/rolling-stats-by-node" in paths


def test_rolling_stats_by_node_queries_json_extract_node_id():
    """Endpoint uses json_extract on event_meta.node_id — Option B
    approach (no schema migration)."""
    src = Path("app/api/providers.py").read_text() + "\n" + Path("app/api/providers_stats.py").read_text()
    assert 'json_extract(ActivityLog.event_meta, "$.node_id")' in src
    assert "by_node" in src


def test_rolling_stats_by_node_handles_null_node_id_as_unknown():
    """Pre-v3.9.16 rows have null node_id; they roll into an "unknown"
    bucket so the UI can still display them as legacy traffic."""
    # v4.4.14: endpoint moved to providers_stats.py. Read both files and
    # search the concatenated source.
    src = (
        Path("app/api/providers.py").read_text()
        + "\n# providers_stats.py\n"
        + Path("app/api/providers_stats.py").read_text()
    )
    # The function name appears as the route handler in providers_stats.py.
    idx = src.index("provider_rolling_stats_by_node(")
    fn = src[idx:idx + 4000]
    assert 'node_id or "unknown"' in fn


# ── P3c: UI disabled-row badge ─────────────────────────────────────


def test_metrics_page_imports_providers_api():
    """MetricsPage now fetches the provider list to read manual_override_until."""
    src = Path("frontend/src/pages/MetricsPage.tsx").read_text()
    assert "providersApi" in src


def test_metrics_page_renders_lock_badge_for_override():
    """The 🔒 disabled badge renders when manual_override_until is set."""
    src = Path("frontend/src/pages/MetricsPage.tsx").read_text()
    assert "🔒 disabled" in src
    assert "manual_override_until" in src
    # Visual: opacity-60 grays the row
    assert "opacity-60" in src


# ── P5: Grok-Web 429 auto-skip ─────────────────────────────────────


def test_grokweb_cooloff_pattern_regex():
    """Pattern matches the actual grok-web bridge error message format."""
    from app.api._grok_web_dispatch import _GROKWEB_COOLOFF_PATTERN
    m = _GROKWEB_COOLOFF_PATTERN.search('grok.com 429 (cached, cool-off 59s remaining)')
    assert m is not None
    assert int(m.group(1)) == 59


def test_grokweb_cooloff_helper_exists():
    """The helper that sets Provider.auto_skip_until on 429 cool-off."""
    from app.api._grok_web_dispatch import _apply_grokweb_429_cooloff
    import asyncio
    assert asyncio.iscoroutinefunction(_apply_grokweb_429_cooloff)


def test_grokweb_429_wired_at_all_catch_sites():
    """All 4 GrokWebError catch sites in _grok_web_dispatch call the
    cool-off helper when status_code == 429."""
    src = Path("app/api/_grok_web_dispatch.py").read_text()
    # Count: GrokWebError catch sites that follow with cooloff wiring
    catch_count = src.count("except GrokWebError as e:")
    wire_count = src.count("await _apply_grokweb_429_cooloff(")
    assert catch_count == 4
    assert wire_count == catch_count, (
        f"GrokWebError catch sites: {catch_count}; wire-ups: {wire_count}. "
        "Every catch site must call the cool-off helper on 429."
    )


def test_grokweb_cooloff_sanity_bounds():
    """Cool-off helper rejects out-of-range values (negative or > 1h)."""
    src = Path("app/api/_grok_web_dispatch.py").read_text()
    idx = src.index("_apply_grokweb_429_cooloff")
    fn = src[idx:idx + 2500]
    assert "secs <= 0 or secs > 3600" in fn  # 1h max


def test_grokweb_cooloff_does_not_shorten_longer_skips():
    """If auto_skip_until is already further out (e.g. 24h auth skip),
    don't shorten it for a 60s rate-limit cool-off."""
    src = Path("app/api/_grok_web_dispatch.py").read_text()
    idx = src.index("_apply_grokweb_429_cooloff")
    fn = src[idx:idx + 2500]
    assert "existing.replace(tzinfo=timezone.utc) < skip_until" in fn


# ── P6: OpenAI Assistants handler scaffolding ──────────────────────


def test_assistants_module_imports_clean():
    """Handler module imports without error."""
    import importlib
    mod = importlib.import_module("app.memory.handlers_openai_assistants")
    assert hasattr(mod, "flush_openai_assistants")
    assert hasattr(mod, "recover_openai_assistants")
    assert hasattr(mod, "register")


def test_assistants_handlers_registered_at_import():
    """Importing app.memory registers the openai-assistants handlers
    in both the flush + recover registries."""
    import importlib
    # Ensure the memory package has been imported
    importlib.import_module("app.memory")
    from app.memory.flush import _HANDLERS as flush_registry
    from app.memory.recover import _HANDLERS as recover_registry
    assert "openai-assistants" in flush_registry
    assert "openai-assistants" in recover_registry


def test_assistants_flush_handler_signature():
    """Flush handler is a coroutine that takes a ctx dict."""
    import asyncio
    from app.memory.handlers_openai_assistants import flush_openai_assistants
    assert asyncio.iscoroutinefunction(flush_openai_assistants)


def test_assistants_recover_handler_signature():
    """Recover handler is a coroutine that takes a ctx dict."""
    import asyncio
    from app.memory.handlers_openai_assistants import recover_openai_assistants
    assert asyncio.iscoroutinefunction(recover_openai_assistants)


def test_assistants_flush_succeeds_when_no_thread_id():
    """No thread_id known → trivial success; marker still advances."""
    import asyncio
    from app.memory.handlers_openai_assistants import flush_openai_assistants
    result = asyncio.run(flush_openai_assistants({
        "old_provider": None,
        "last_known_external_ref": None,
    }))
    assert result is True


def test_assistants_recover_returns_none_when_no_thread_id():
    """No thread to read back → None (handler caller will retry later)."""
    import asyncio
    from app.memory.handlers_openai_assistants import recover_openai_assistants
    result = asyncio.run(recover_openai_assistants({
        "old_provider": None,
        "last_known_external_ref": None,
    }))
    assert result is None


def test_assistants_uses_beta_header():
    """Assistants API requires the OpenAI-Beta=assistants=v2 header."""
    src = Path("app/memory/handlers_openai_assistants.py").read_text()
    assert "OpenAI-Beta" in src
    assert "assistants=v2" in src


def test_assistants_treats_404_as_success_flush():
    """A 404 on DELETE means the thread is already gone — semantically
    a successful flush (no orphan to clean up)."""
    src = Path("app/memory/handlers_openai_assistants.py").read_text()
    idx = src.index("flush_openai_assistants")
    fn = src[idx:idx + 3000]
    assert "404" in fn
    assert "already 404" in fn or "already gone" in fn.lower()
