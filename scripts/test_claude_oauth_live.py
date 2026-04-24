"""
Comprehensive claude-oauth torture-test. Runs inside the llm-proxy2 container
so it can exercise internal handlers directly AND hit the public API.

Each test prints PASS/FAIL + a one-line summary, and the suite ends with a
totals table. Token usage is tracked and printed at the end.

Designed to be run as:
    sudo docker cp scripts/test_claude_oauth_live.py llm-proxy2:/tmp/burn.py
    sudo docker exec llm-proxy2 sh -c 'cd /app && PYTHONPATH=/app python3 /tmp/burn.py'

Live test — sends real traffic to platform.claude.com and burns tokens.
Requires a configured claude-oauth provider (set PROVIDER_NAME below).
Not run in CI — opt-in via direct invocation.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import traceback
from typing import Any, Callable

import httpx
from sqlalchemy import select

from app.models.database import AsyncSessionLocal
from app.models.db import Provider
from app.providers.claude_oauth import build_headers, PLATFORM_BASE_URL
from app.providers.claude_oauth_flow import refresh_access_token
from app.providers.scanner import scan_provider_models, test_provider
from app.api._messages_streaming import (
    _inject_claude_code_system, _complete_claude_oauth, _stream_claude_oauth,
)


PROVIDER_NAME = "Devin-VG"

# Accumulators
TOTAL_IN = 0
TOTAL_OUT = 0
TOTAL_CACHE_CREATE = 0
TOTAL_CACHE_READ = 0
RESULTS: list[tuple[str, bool, str]] = []


def _track(usage: dict) -> None:
    global TOTAL_IN, TOTAL_OUT, TOTAL_CACHE_CREATE, TOTAL_CACHE_READ
    TOTAL_IN += int(usage.get("input_tokens") or 0)
    TOTAL_OUT += int(usage.get("output_tokens") or 0)
    TOTAL_CACHE_CREATE += int(usage.get("cache_creation_input_tokens") or 0)
    TOTAL_CACHE_READ += int(usage.get("cache_read_input_tokens") or 0)


def _record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {name}: {detail}")


async def _load_provider() -> Provider:
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Provider).where(Provider.name == PROVIDER_NAME))
        p = r.scalar_one()
        # Expunge so we can use it outside the session context
        db.expunge(p)
        return p


async def _raw_call(
    provider: Provider, *, body: dict, url_path: str = "/v1/messages?beta=true",
) -> tuple[int, dict]:
    """Low-level POST against platform.claude.com with the CC marker injected."""
    headers = {
        **build_headers(provider.api_key, model=body.get("model")),
        "Content-Type": "application/json",
    }
    body = _inject_claude_code_system(body)
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as c:
        r = await c.post(f"{PLATFORM_BASE_URL}{url_path}", json=body, headers=headers)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:600]}


# ── Tests ─────────────────────────────────────────────────────────────────


async def t_basic(p: Provider) -> None:
    st, data = await _raw_call(p, body={
        "model": p.default_model, "max_tokens": 32,
        "messages": [{"role": "user", "content": "Say: hello from Claude"}],
    })
    _track(data.get("usage") or {})
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    _record("basic_non_streaming", st == 200 and bool(txt), f"status={st} reply={txt!r}")


async def t_streaming_sse(p: Provider) -> None:
    """Consume a streaming response and assert event-type order."""
    headers = {**build_headers(p.api_key), "Content-Type": "application/json"}
    body = _inject_claude_code_system({
        "model": p.default_model, "max_tokens": 50, "stream": True,
        "messages": [{"role": "user", "content": "Count slowly: one two three"}],
    })
    evts = []
    text = []
    usage: dict = {}
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
        async with c.stream("POST", f"{PLATFORM_BASE_URL}/v1/messages?beta=true",
                             json=body, headers=headers) as r:
            if r.status_code != 200:
                _record("streaming_sse", False, f"HTTP {r.status_code}")
                return
            async for raw in r.aiter_lines():
                if raw.startswith("event: "):
                    evts.append(raw[7:].strip())
                elif raw.startswith("data: "):
                    try:
                        evt = json.loads(raw[6:])
                    except Exception:
                        continue
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text.append(delta.get("text") or "")
                    elif evt.get("type") == "message_start":
                        usage = (evt.get("message") or {}).get("usage") or usage
                    elif evt.get("type") == "message_delta":
                        u = evt.get("usage") or {}
                        if u:
                            usage = {**usage, **u}
    _track(usage)
    ok = ("message_start" in evts and "content_block_delta" in evts
          and "message_stop" in evts and bool("".join(text).strip()))
    _record("streaming_sse", ok, f"events={len(evts)} text={(''.join(text))[:40]!r}")


async def t_system_prompt_preserved(p: Provider) -> None:
    """User-supplied system prompt should still influence output after marker injection."""
    st, data = await _raw_call(p, body={
        "model": p.default_model, "max_tokens": 32,
        "system": "You always respond in French.",
        "messages": [{"role": "user", "content": "Greet me."}],
    })
    _track(data.get("usage") or {})
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    # French should contain accented chars or common French greetings
    french = any(ch in txt for ch in "àâéèêëîïôûç") or any(w in txt.lower() for w in ("bonjour", "salut"))
    _record("system_prompt_preserved", st == 200 and french, f"status={st} french_detected={french} reply={txt[:60]!r}")


async def t_multi_turn(p: Provider) -> None:
    st, data = await _raw_call(p, body={
        "model": p.default_model, "max_tokens": 40,
        "messages": [
            {"role": "user", "content": "My favorite number is 42."},
            {"role": "assistant", "content": "Got it, 42 is your favorite."},
            {"role": "user", "content": "What number did I say is my favorite?"},
        ],
    })
    _track(data.get("usage") or {})
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    _record("multi_turn", st == 200 and "42" in txt, f"status={st} reply={txt[:60]!r}")


async def t_tool_use(p: Provider) -> None:
    """Define a tool, see the model call it, reply with a tool result, get final answer."""
    tool = {
        "name": "get_weather",
        "description": "Look up current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
    # Turn 1: model should emit tool_use
    st, data = await _raw_call(p, body={
        "model": p.default_model, "max_tokens": 200, "tools": [tool],
        "messages": [{"role": "user", "content": "What's the weather in Paris?"}],
    })
    _track(data.get("usage") or {})
    if st != 200:
        _record("tool_use", False, f"turn1 HTTP {st}")
        return
    tool_blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
    if not tool_blocks:
        _record("tool_use", False, f"turn1 no tool_use block; stop={data.get('stop_reason')}")
        return
    tool_use_id = tool_blocks[0]["id"]
    # Turn 2: feed a fake tool_result back, expect a final text response
    st2, data2 = await _raw_call(p, body={
        "model": p.default_model, "max_tokens": 200, "tools": [tool],
        "messages": [
            {"role": "user", "content": "What's the weather in Paris?"},
            {"role": "assistant", "content": data["content"]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_use_id,
                "content": "18°C and raining.",
            }]},
        ],
    })
    _track(data2.get("usage") or {})
    txt = "".join(b.get("text", "") for b in data2.get("content", []) if b.get("type") == "text")
    ok = st2 == 200 and ("18" in txt or "rain" in txt.lower())
    _record("tool_use", ok, f"tool_name={tool_blocks[0].get('name')} final={txt[:60]!r}")


async def t_vision(p: Provider) -> None:
    # Valid 40x40 RGBA PNG (checkerboard), built with zlib+struct
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAYAAACM/rhtAAAATUlEQVR4nO3OsQkAMAwDwYzuzZMN"
        "okq4OYHKhzs3bGa+b/cHEBAQsAzcBqQeEBAQsA3cBqQBAgICtoHbgNQDAgICtoHbgDRAQEDAcv8A"
        "rltxLVilpFEAAAAASUVORK5CYII="
    )
    st, data = await _raw_call(p, body={
        "model": p.default_model, "max_tokens": 80,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": png_b64,
            }},
            {"type": "text", "text": "Describe this image in one short sentence."},
        ]}],
    })
    _track(data.get("usage") or {})
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    _record("vision", st == 200 and bool(txt), f"status={st} reply={txt[:80]!r}")


async def t_prompt_caching(p: Provider) -> None:
    """Send the SAME cachable prompt twice; second call should show cache_read > 0."""
    # Build a long-ish system that crosses the 1024-token cache threshold.
    # Anthropic's docs say Sonnet's minimum cacheable prefix is 1024 tokens.
    # ~4 chars/token means we need >= 4100 chars of unique-ish content.
    big_system = ("You are a careful, meticulous code reviewer who pays attention "
                  "to correctness, style, and security implications. " * 120)  # ~15000 chars, ~3750 tokens
    body = {
        "model": p.default_model, "max_tokens": 16,
        "system": [
            {"type": "text", "text": big_system, "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": "Respond with: cached"}],
    }
    st1, d1 = await _raw_call(p, body=body)
    u1 = d1.get("usage") or {}
    _track(u1)
    if st1 != 200:
        _record("prompt_caching", False, f"turn1 HTTP {st1}")
        return
    # Second call — same body. Give the ephemeral cache a moment to propagate.
    await asyncio.sleep(3)
    st2, d2 = await _raw_call(p, body=body)
    u2 = d2.get("usage") or {}
    _track(u2)
    create = int(u1.get("cache_creation_input_tokens") or 0)
    read = int(u2.get("cache_read_input_tokens") or 0)
    _record("prompt_caching", st2 == 200 and read > 0,
            f"create1={create} read2={read} (cache {'HIT' if read > 0 else 'MISS'})")


async def t_concurrent(p: Provider) -> None:
    async def _one(i: int) -> int:
        st, data = await _raw_call(p, body={
            "model": p.default_model, "max_tokens": 20,
            "messages": [{"role": "user", "content": f"Say number {i}"}],
        })
        _track(data.get("usage") or {})
        return st
    sts = await asyncio.gather(*[_one(i) for i in range(5)])
    ok = all(s == 200 for s in sts)
    _record("concurrent_5x", ok, f"statuses={sts}")


async def t_multiple_models(p: Provider) -> None:
    models = ["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"]
    results = {}
    for m in models:
        st, data = await _raw_call(p, body={
            "model": m, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with model name in one word."}],
        })
        _track(data.get("usage") or {})
        results[m] = st
        await asyncio.sleep(0.3)
    ok = all(s == 200 for s in results.values())
    _record("multiple_models", ok, " ".join(f"{m.split('-')[1]}={s}" for m, s in results.items()))


async def t_scan_models(p: Provider) -> None:
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Provider).where(Provider.id == p.id))
        pdb = r.scalar_one()
        models = await scan_provider_models(db, pdb)
    _record("scan_models", len(models) >= 5, f"discovered {len(models)} models")


async def t_test_provider_button(p: Provider) -> None:
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Provider).where(Provider.id == p.id))
        pdb = r.scalar_one()
    res = await test_provider(pdb)
    _record("test_provider_button", bool(res.get("success")),
            f"success={res.get('success')} reply={res.get('response')!r}")


async def t_refresh_and_persist(p: Provider) -> None:
    """Exercise refresh_and_persist — the production helper that rotates
    the refresh token and writes it back to the DB. The refresh token is
    single-use: once consumed, subsequent calls return invalid_grant
    unless the rotated one was persisted."""
    from app.providers.claude_oauth_flow import refresh_and_persist, OAuthFlowError
    if not p.oauth_refresh_token:
        _record("refresh_and_persist", False, "no refresh_token stored")
        return
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Provider).where(Provider.id == p.id))
        pdb = r.scalar_one()
        original_access = pdb.api_key
        original_refresh = pdb.oauth_refresh_token
        try:
            result = await refresh_and_persist(pdb, db)
        except OAuthFlowError as e:
            if "invalid_grant" in str(e):
                _record("refresh_and_persist", False,
                        "stored refresh_token already consumed by a prior "
                        "test run — re-do browser OAuth to reset")
                return
            _record("refresh_and_persist", False, f"raised: {str(e)[:120]}")
            return
        await db.refresh(pdb)
        access_rotated = pdb.api_key != original_access
        refresh_rotated = pdb.oauth_refresh_token != original_refresh
        ok = bool(result.access_token) and access_rotated and refresh_rotated
        _record("refresh_and_persist", ok,
                f"access_rotated={access_rotated} refresh_rotated={refresh_rotated} "
                f"expires_in={result.expires_at and int(result.expires_at - time.time())}s")


async def t_invalid_model(p: Provider) -> None:
    """An invalid model should fail cleanly with an error status, not 200."""
    st, data = await _raw_call(p, body={
        "model": "this-model-does-not-exist-xyz", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    })
    err = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
    ok = st >= 400
    _record("invalid_model_errors_cleanly", ok, f"status={st} err={str(err)[:60]!r}")


async def t_direct_handler_complete(p: Provider) -> None:
    """Exercise the actual production handler used by /v1/messages."""
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        body = {
            "model": p.default_model, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Respond with: handler-ok"}],
        }
        try:
            data = await _complete_claude_oauth(
                access_token=p.api_key, body=body, provider_id=p.id, db=db,
                key_record_id="", t0=t0,
            )
            _track(data.get("usage") or {})
            txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            _record("direct_handler_complete", bool(txt), f"reply={txt!r}")
        except Exception as e:
            _record("direct_handler_complete", False, f"raised: {e}")


async def t_direct_handler_stream(p: Provider) -> None:
    """Exercise the actual production streaming handler."""
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        body = {
            "model": p.default_model, "max_tokens": 30,
            "messages": [{"role": "user", "content": "Count: 1 2 3"}],
        }
        try:
            chunks: list[bytes] = []
            async for c in _stream_claude_oauth(
                access_token=p.api_key, body=body, provider_id=p.id, db=db,
                key_record_id="", t0=t0,
            ):
                chunks.append(c)
            blob = b"".join(chunks).decode(errors="replace")
            ok = "event: message_start" in blob and "event: message_stop" in blob
            _record("direct_handler_stream", ok, f"bytes={sum(len(c) for c in chunks)} events_ok={ok}")
        except Exception as e:
            _record("direct_handler_stream", False, f"raised: {e}")


async def t_usage_recorded(p: Provider) -> None:
    """After all the above, metrics rows should exist for this provider."""
    from app.models.db import ProviderMetric
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(ProviderMetric).where(ProviderMetric.provider_id == p.id)
                .order_by(ProviderMetric.bucket_ts.desc()).limit(3)
        )
        rows = r.scalars().all()
    ok = len(rows) > 0
    total = sum((row.requests or 0) for row in rows)
    _record("usage_recorded", ok, f"buckets={len(rows)} recent_requests={total}")


async def t_marker_injection_unit(p: Provider) -> None:
    """Pure in-process check that _inject_claude_code_system behaves correctly."""
    MARKER = "You are Claude Code, Anthropic's official CLI for Claude."
    cases = [
        # (input, expected_marker_at_index_0)
        ({}, True),
        ({"system": "User system"}, True),
        ({"system": [{"type": "text", "text": "User block"}]}, True),
        ({"system": [{"type": "text", "text": MARKER + " extra"}]}, False),  # already has marker
    ]
    all_ok = True
    for i, (body, expect_injected) in enumerate(cases):
        out = _inject_claude_code_system(body.copy())
        sys_list = out.get("system") or []
        if isinstance(sys_list, str):
            sys_list = [{"type": "text", "text": sys_list}]
        head = sys_list[0]["text"] if sys_list else ""
        was_injected = head == MARKER  # exactly the marker (not concatenated)
        if expect_injected and not was_injected:
            all_ok = False
            break
        if not expect_injected and was_injected:
            # This case has a longer marker already, _inject should leave it alone
            all_ok = False
            break
    _record("marker_injection_unit", all_ok, f"all 4 shape cases OK" if all_ok else "unexpected injection behavior")


# ── Runner ────────────────────────────────────────────────────────────────

TESTS: list[tuple[str, Callable]] = [
    ("marker_injection_unit", t_marker_injection_unit),
    ("basic_non_streaming", t_basic),
    ("direct_handler_complete", t_direct_handler_complete),
    ("direct_handler_stream", t_direct_handler_stream),
    ("streaming_sse", t_streaming_sse),
    ("system_prompt_preserved", t_system_prompt_preserved),
    ("multi_turn", t_multi_turn),
    ("tool_use", t_tool_use),
    ("vision", t_vision),
    ("prompt_caching", t_prompt_caching),
    ("concurrent_5x", t_concurrent),
    ("multiple_models", t_multiple_models),
    ("scan_models", t_scan_models),
    ("test_provider_button", t_test_provider_button),
    ("refresh_and_persist", t_refresh_and_persist),
    ("invalid_model_errors_cleanly", t_invalid_model),
    ("usage_recorded", t_usage_recorded),
]


async def main() -> int:
    print(f"Loading provider {PROVIDER_NAME!r}...")
    p = await _load_provider()
    print(f"  id={p.id} model={p.default_model} api_key_prefix={p.api_key[:14]}...")
    print()
    print("Running tests:")
    for name, fn in TESTS:
        t0 = time.monotonic()
        try:
            await fn(p)
        except Exception as e:
            traceback.print_exc()
            _record(name, False, f"uncaught: {e}")
        else:
            pass  # individual test prints its own line
        dt = (time.monotonic() - t0) * 1000
        # Re-edit the last-printed line? Skip — dt is informational only
    print()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"{'='*60}")
    print(f"PASSED: {passed}/{len(RESULTS)}   FAILED: {failed}")
    print(f"Tokens — input={TOTAL_IN}  output={TOTAL_OUT}  "
          f"cache_create={TOTAL_CACHE_CREATE}  cache_read={TOTAL_CACHE_READ}  "
          f"billable_total={TOTAL_IN + TOTAL_OUT + TOTAL_CACHE_CREATE}")
    print(f"{'='*60}")
    if failed:
        print("\nFailures:")
        for name, ok, det in RESULTS:
            if not ok:
                print(f"  - {name}: {det}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
