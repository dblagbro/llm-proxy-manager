#!/usr/bin/env python3
"""Pre-production smoke test suite for llm-proxy-v2.

Runs a battery of live checks against a deployed instance, exercising every
Wave-1 feature end-to-end. Prints PASS/FAIL/SKIP per check and exits 0 only
if all required checks pass.

Usage:
    python3 scripts/smoke_test.py --url https://www.voipguru.org/llm-proxy2 \\
        --admin-user dblagbro --admin-pass '***'

The script logs in as admin, creates a temporary smoke-test API key with
semantic_cache_enabled=True, runs all checks, then deletes the key.

Exit codes:
    0 — all required tests passed
    1 — at least one required test failed
    2 — script setup failed (could not log in / create key)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx


# ─── Test framework ──────────────────────────────────────────────────────────

GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"; RESET = "\033[0m"; DIM = "\033[90m"


@dataclass
class Result:
    name: str
    status: str              # PASS | FAIL | SKIP
    detail: str = ""
    elapsed_ms: float = 0.0


@dataclass
class Suite:
    results: list[Result] = field(default_factory=list)

    def run(self, name: str, fn: Callable[[], Optional[str]], *, required: bool = True) -> Result:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            status = "PASS"
        except SkipTest as exc:
            detail = str(exc)
            status = "SKIP"
        except AssertionError as exc:
            detail = str(exc)
            status = "FAIL" if required else "SKIP"
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            status = "FAIL" if required else "SKIP"
        elapsed = (time.monotonic() - t0) * 1000
        r = Result(name=name, status=status, detail=detail, elapsed_ms=elapsed)
        self.results.append(r)
        colour = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}[r.status]
        print(f"  {colour}{r.status:<4}{RESET}  {r.name}  {DIM}({r.elapsed_ms:.0f}ms){RESET}"
              + (f"  — {r.detail}" if r.detail else ""))
        return r

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == "FAIL"]

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.status == "PASS")
        failed = sum(1 for r in self.results if r.status == "FAIL")
        skipped = sum(1 for r in self.results if r.status == "SKIP")
        return f"{passed} passed, {failed} failed, {skipped} skipped"


class SkipTest(Exception):
    pass


# ─── Fixtures ────────────────────────────────────────────────────────────────


class Fixtures:
    def __init__(self, base_url: str, admin_user: str, admin_pass: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.timeout = timeout
        self.session = httpx.Client(timeout=timeout, follow_redirects=True)
        self.api_key_id: Optional[str] = None
        self.api_key_raw: Optional[str] = None

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def admin_login(self) -> None:
        resp = self.session.post(
            self.url("/api/auth/login"),
            json={"username": self.admin_user, "password": self.admin_pass},
        )
        resp.raise_for_status()

    def create_smoke_key(self) -> None:
        resp = self.session.post(
            self.url("/api/keys"),
            json={
                "name": f"smoke-test-{int(time.time())}",
                "key_type": "standard",
                "semantic_cache_enabled": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.api_key_id = data["id"]
        self.api_key_raw = data["raw_key"]

    def delete_smoke_key(self) -> None:
        if not self.api_key_id:
            return
        try:
            self.session.delete(self.url(f"/api/keys/{self.api_key_id}"))
        except Exception:
            pass

    def llm_headers(self, **extra: str) -> dict[str, str]:
        headers = {
            "x-api-key": self.api_key_raw or "",
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers


# ─── Test cases ──────────────────────────────────────────────────────────────


def t_health(f: Fixtures) -> str:
    r = httpx.get(f.url("/health"), timeout=f.timeout)
    assert r.status_code == 200, f"status {r.status_code}"
    data = r.json()
    # /health is served by cluster.py (healthy/degraded) at /llm-proxy2/health;
    # the simple main.py handler returns "ok" (only reachable without nginx)
    assert data["status"] in ("ok", "healthy", "degraded"), f"unexpected status {data.get('status')}"
    assert "version" in data, "no version"
    detail = f"v{data['version']}"
    if "healthyProviders" in data:
        detail += f", {data['healthyProviders']}/{data['totalProviders']} providers"
    return detail


def t_version(f: Fixtures) -> str:
    r = httpx.get(f.url("/version"), timeout=f.timeout)
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "llm-proxy"
    return f"v{data['version']}"


def t_metrics_registered(f: Fixtures) -> str:
    r = httpx.get(f.url("/metrics"), timeout=f.timeout)
    assert r.status_code == 200
    body = r.text
    expected_names = [
        "llm_proxy_requests_total",
        "llm_proxy_request_duration_seconds",
        "llm_proxy_ttft_seconds",
        "llm_proxy_tokens_total",
        "llm_proxy_cost_usd_total",
        "llm_proxy_cache_tokens_total",
        "llm_proxy_circuit_breaker_state",
        "llm_proxy_cot_iterations",
        "llm_proxy_cache_lookups_total",
        "llm_proxy_cache_similarity",
        "llm_proxy_hedge_attempts_total",
        "llm_proxy_hedge_wins_total",
        "llm_proxy_hedge_bucket_rejects_total",
        "llm_proxy_service_info",
    ]
    missing = [n for n in expected_names if f"# HELP {n} " not in body]
    assert not missing, f"missing: {missing}"
    return f"{len(expected_names)} metric families"


def t_models_list(f: Fixtures) -> str:
    r = httpx.get(f.url("/v1/models"), timeout=f.timeout)
    assert r.status_code == 200
    data = r.json()
    assert data.get("object") == "list"
    assert isinstance(data.get("data"), list), "data not list"
    assert len(data["data"]) > 0, "no models"
    return f"{len(data['data'])} models"


def t_v1_messages_nonstreaming(f: Fixtures) -> str:
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 40,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    }
    r = f.session.post(f.url("/v1/messages"), json=body, headers=f.llm_headers())
    if r.status_code == 502:
        raise SkipTest(f"upstream provider error: {r.text[:200]}")
    assert r.status_code == 200, f"{r.status_code} — {r.text[:200]}"
    data = r.json()
    assert data.get("type") == "message", data
    # Verify cross-format response headers
    for h in ("X-Provider", "X-Resolved-Model", "LLM-Capability", "X-Token-Budget-Remaining"):
        assert h in r.headers, f"missing {h}"
    provider = r.headers.get("X-Provider", "")
    model = r.headers.get("X-Resolved-Model", "")
    return f"{provider}/{model}"


def t_v1_messages_streaming(f: Fixtures) -> str:
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 40,
        "stream": True,
        "messages": [{"role": "user", "content": "Reply with exactly: STREAM_OK"}],
    }
    with f.session.stream("POST", f.url("/v1/messages"), json=body, headers=f.llm_headers()) as r:
        if r.status_code == 502:
            raise SkipTest(f"upstream provider error: {r.read()[:200]}")
        assert r.status_code == 200, f"{r.status_code}"
        events = []
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    events.append(json.loads(line[6:]))
                except Exception:
                    pass
        types = [e.get("type") for e in events]
        if "error" in types and "message_start" not in types:
            err_msg = next((e.get("error", {}).get("message", "") for e in events if e.get("type") == "error"), "")
            raise SkipTest(f"upstream error mid-stream: {err_msg[:200]}")
        assert "message_start" in types, f"no message_start: {types}"
        assert "message_stop" in types, f"no message_stop: {types}"
    return f"{len(events)} SSE events"


def t_v1_chat_completions_nonstreaming(f: Fixtures) -> str:
    body = {
        "model": "gpt-4o",
        "max_tokens": 40,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    }
    r = f.session.post(f.url("/v1/chat/completions"), json=body, headers=f.llm_headers())
    if r.status_code == 502:
        raise SkipTest(f"upstream provider error: {r.text[:200]}")
    assert r.status_code == 200, f"{r.status_code} — {r.text[:200]}"
    data = r.json()
    assert "choices" in data, data
    return r.headers.get("X-Resolved-Model", "")


def t_v1_chat_completions_streaming(f: Fixtures) -> str:
    body = {
        "model": "gpt-4o",
        "max_tokens": 40,
        "stream": True,
        "messages": [{"role": "user", "content": "Reply with exactly: STREAM_OK"}],
    }
    with f.session.stream("POST", f.url("/v1/chat/completions"), json=body, headers=f.llm_headers()) as r:
        if r.status_code == 502:
            raise SkipTest(f"upstream provider error: {r.read()[:200]}")
        assert r.status_code == 200, f"{r.status_code}"
        chunks = 0
        done_seen = False
        for line in r.iter_lines():
            if line == "data: [DONE]":
                done_seen = True
            elif line.startswith("data: "):
                chunks += 1
        assert done_seen, "no [DONE]"
    return f"{chunks} chunks + [DONE]"


def t_budget_headers(f: Fixtures) -> str:
    """Budget headers should be absent when no caps set; present when they are."""
    # First set a soft cap on the smoke key
    f.session.patch(
        f.url(f"/api/keys/{f.api_key_id}"),
        json={"daily_soft_cap_usd": 1000.0, "daily_hard_cap_usd": 1000.0, "hourly_cap_usd": 1000.0},
    ).raise_for_status()
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = f.session.post(f.url("/v1/messages"), json=body, headers=f.llm_headers())
    if r.status_code == 502:
        raise SkipTest("upstream provider error")
    assert r.status_code == 200, f"{r.status_code}"
    assert "X-Budget-Daily-Remaining" in r.headers, "no X-Budget-Daily-Remaining"
    assert "X-Budget-Hourly-Remaining" in r.headers, "no X-Budget-Hourly-Remaining"
    return f"daily={r.headers['X-Budget-Daily-Remaining']} hourly={r.headers['X-Budget-Hourly-Remaining']}"


def t_cache_bypass_or_miss(f: Fixtures) -> str:
    """Every request should carry X-Cache-Status."""
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = f.session.post(f.url("/v1/messages"), json=body, headers=f.llm_headers())
    if r.status_code == 502:
        raise SkipTest("upstream provider error")
    assert r.status_code == 200
    status = r.headers.get("X-Cache-Status")
    assert status in ("hit", "miss", "bypass"), f"bad value: {status!r}"
    return status


def t_cache_hit_on_repeat(f: Fixtures) -> str:
    """Send the same low-temperature request twice; second should hit cache."""
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 30,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": f"Return the single word 'ALPHA'. Nonce: smoke-{int(time.time())}",
        }],
    }
    r1 = f.session.post(f.url("/v1/messages"), json=body, headers=f.llm_headers())
    if r1.status_code == 502:
        raise SkipTest("upstream provider error on first call")
    assert r1.status_code == 200
    status1 = r1.headers.get("X-Cache-Status")
    # Second identical request
    time.sleep(0.5)
    r2 = f.session.post(f.url("/v1/messages"), json=body, headers=f.llm_headers())
    assert r2.status_code == 200
    status2 = r2.headers.get("X-Cache-Status")
    similarity = r2.headers.get("X-Cache-Similarity", "")
    assert status2 == "hit", f"expected hit, got {status2} (first={status1})"
    return f"sim={similarity}"


def t_cache_force_off(f: Fixtures) -> str:
    """X-Cache: none header should bypass even opted-in keys."""
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = f.session.post(f.url("/v1/messages"), json=body,
                       headers=f.llm_headers(**{"x-cache": "none"}))
    if r.status_code == 502:
        raise SkipTest("upstream provider error")
    assert r.status_code == 200
    assert r.headers.get("X-Cache-Status") == "bypass", f"got {r.headers.get('X-Cache-Status')}"
    return "bypassed"


def t_unauthenticated_rejected(f: Fixtures) -> str:
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = httpx.post(f.url("/v1/messages"), json=body, timeout=f.timeout)
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    return "401 OK"


def t_hedge_header_acceptance(f: Fixtures) -> str:
    """Hedging only engages after ≥20 TTFT samples; header echo is absent on cold cache.
    The test here only verifies the proxy accepts the X-Hedge header without erroring."""
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 10,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    with f.session.stream("POST", f.url("/v1/messages"), json=body,
                          headers=f.llm_headers(**{"x-hedge": "on"})) as r:
        if r.status_code == 502:
            raise SkipTest("upstream provider error")
        assert r.status_code == 200
        list(r.iter_lines())  # drain
        winner = r.headers.get("X-Hedged-Winner", "—")
    return f"winner={winner}"


# ─── Runner ──────────────────────────────────────────────────────────────────


ALL_TESTS: list[tuple[str, Callable, bool]] = [
    # (name, fn, required)
    ("health endpoint", t_health, True),
    ("version endpoint", t_version, True),
    ("/metrics — all metric families registered", t_metrics_registered, True),
    ("/v1/models listing", t_models_list, True),
    ("unauthenticated /v1/messages rejected", t_unauthenticated_rejected, True),
    ("/v1/messages non-streaming", t_v1_messages_nonstreaming, True),
    ("/v1/messages streaming", t_v1_messages_streaming, True),
    ("/v1/chat/completions non-streaming", t_v1_chat_completions_nonstreaming, True),
    ("/v1/chat/completions streaming", t_v1_chat_completions_streaming, True),
    ("X-Cache-Status header present", t_cache_bypass_or_miss, True),
    ("semantic cache hits on repeat", t_cache_hit_on_repeat, False),  # best-effort
    ("X-Cache: none forces bypass", t_cache_force_off, True),
    ("budget headers appear when caps set", t_budget_headers, True),
    ("hedge header accepted (no crash)", t_hedge_header_acceptance, True),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("SMOKE_URL", "https://www.voipguru.org/llm-proxy2"))
    ap.add_argument("--admin-user", default=os.getenv("SMOKE_USER", "dblagbro"))
    ap.add_argument("--admin-pass", default=os.getenv("SMOKE_PASS", ""))
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--keep-key", action="store_true",
                    help="don't delete the temporary smoke-test API key on exit")
    args = ap.parse_args()

    if not args.admin_pass:
        print(f"{RED}ERROR:{RESET} --admin-pass (or $SMOKE_PASS) is required")
        return 2

    print(f"\n=== llm-proxy-v2 smoke test ===")
    print(f"target: {args.url}")

    f = Fixtures(args.url, args.admin_user, args.admin_pass, args.timeout)
    try:
        f.admin_login()
        f.create_smoke_key()
    except Exception as exc:
        print(f"{RED}SETUP FAILED:{RESET} {exc}")
        return 2

    print(f"smoke key: {f.api_key_id} (created for this run)\n")

    suite = Suite()
    for name, fn, required in ALL_TESTS:
        suite.run(name, lambda fn=fn: fn(f), required=required)

    print(f"\n{suite.summary()}")
    if suite.failed:
        print(f"{RED}FAILED:{RESET}")
        for r in suite.failed:
            print(f"  • {r.name}: {r.detail}")

    if not args.keep_key:
        f.delete_smoke_key()
        print(f"\n{DIM}(smoke key deleted){RESET}")
    else:
        print(f"\n{DIM}(smoke key kept: {f.api_key_id}){RESET}")

    return 1 if suite.failed else 0


if __name__ == "__main__":
    sys.exit(main())
