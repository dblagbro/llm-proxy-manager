#!/usr/bin/env python3
"""ARCH-A — DB connection-pool leak reproduction harness.

The latent leak: www01 + GCP saturated the SQLAlchemy QueuePool 13-20h
post-deploy, blocking auth and returning /health 500s. Every
``AsyncSessionLocal()`` is ``async with``-wrapped, so it is NOT naive
session leakage. Three standing hypotheses: a direct ``engine.connect()``,
a session held across a hung ``await``, or a streaming-disconnect
cleanup gap.

This harness compresses the 13-20h window by driving load and isolates
WHICH request path leaks. It runs three phases against a target node
and, for each, measures whether pool ``checked_out`` returns to its
pre-phase floor after the load drains:

  A. non-streaming        /v1/messages stream=false, consumed
  B. streaming-consumed   /v1/messages stream=true, fully read
  C. streaming-abandoned  /v1/messages stream=true, connection dropped
                          after the first chunk (the disconnect suspect)

A phase whose post-drain floor stays above its pre-phase floor leaked.

Pair with ``DB_POOL_TRACE=1`` (config ``db_pool_trace``): when that is
set on the target node, this harness also prints the trace summary and
the command to dump per-connection acquisition stacks — which name the
leaking code path directly.

Run inside the llm-proxy2 container:
  docker exec -e PYTHONPATH=/app llm-proxy2 python3 /tmp/archa_pool_leak_harness.py

Options: --base URL  --requests N  --concurrency C  --model M  --drain SEC

Exit codes: 0 = no leak detected, 1 = a leak reproduced, 2 = setup error.
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

import httpx

from app.models.database import AsyncSessionLocal
from app.models.db import ApiKey
from app.auth.keys import _hash_key


async def _pool_checked_out(client: httpx.AsyncClient, base: str) -> int | None:
    """Read live pool ``checked_out`` from the target's /health (served
    by the uvicorn process — the one whose pool we care about)."""
    try:
        r = await client.get(f"{base}/health", timeout=10.0)
        return ((r.json() or {}).get("dbPool") or {}).get("checked_out")
    except Exception:
        return None


async def _pool_floor(client: httpx.AsyncClient, base: str, samples: int = 4) -> int:
    """Sample ``checked_out`` a few times and take the minimum — the
    floor filters out transient in-flight background-task checkouts."""
    vals: list[int] = []
    for _ in range(samples):
        v = await _pool_checked_out(client, base)
        if v is not None:
            vals.append(v)
        await asyncio.sleep(0.75)
    return min(vals) if vals else -1


def _body(model: str, stream: bool) -> dict:
    return {
        "model": model,
        "max_tokens": 16,
        "stream": stream,
        "messages": [{"role": "user", "content": "reply with the single word: ok"}],
    }


async def _one_request(client: httpx.AsyncClient, base: str, key: str,
                       model: str, phase: str) -> None:
    """Drive one request of the given phase. Exceptions are swallowed —
    a failed upstream still exercised the DB-session path, which is what
    the leak hunt cares about."""
    headers = {"x-api-key": key, "content-type": "application/json"}
    url = f"{base}/v1/messages"
    try:
        if phase == "nonstream":
            await client.post(url, headers=headers, json=_body(model, False),
                               timeout=60.0)
        elif phase == "stream_consumed":
            async with client.stream("POST", url, headers=headers,
                                     json=_body(model, True), timeout=60.0) as resp:
                async for _ in resp.aiter_bytes():
                    pass
        elif phase == "stream_abandoned":
            async with client.stream("POST", url, headers=headers,
                                     json=_body(model, True), timeout=60.0) as resp:
                async for _ in resp.aiter_bytes():
                    break  # got first bytes — drop the connection mid-stream
    except Exception:
        pass


async def _run_phase(client: httpx.AsyncClient, base: str, key: str, model: str,
                     phase: str, requests: int, concurrency: int,
                     drain: int) -> dict:
    before = await _pool_floor(client, base)
    sem = asyncio.Semaphore(concurrency)

    async def _guarded() -> None:
        async with sem:
            await _one_request(client, base, key, model, phase)

    await asyncio.gather(*[_guarded() for _ in range(requests)])
    # Let in-flight work + background record_outcome writes settle.
    await asyncio.sleep(drain)
    after = await _pool_floor(client, base)
    leaked = (after - before) if (before >= 0 and after >= 0) else None
    return {"phase": phase, "before": before, "after": after, "leaked": leaked}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3000")
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--drain", type=int, default=10)
    args = ap.parse_args()

    raw_key = f"llmp-archa-{secrets.token_hex(16)}"
    key_id = None
    try:
        async with AsyncSessionLocal() as db:
            k = ApiKey(name="archa-pool-leak-harness", key_hash=_hash_key(raw_key),
                       key_prefix=raw_key[:8], key_type="standard", enabled=True)
            db.add(k)
            await db.commit()
            await db.refresh(k)
            key_id = k.id

        print(f"ARCH-A pool-leak harness — target {args.base}, model {args.model}")
        print(f"  {args.requests} requests/phase, concurrency {args.concurrency}, "
              f"drain {args.drain}s\n")

        async with httpx.AsyncClient() as client:
            health = await client.get(f"{args.base}/health", timeout=10.0)
            dbpool = (health.json() or {}).get("dbPool") or {}
            print(f"  pool: size={dbpool.get('size')} max={dbpool.get('max')} "
                  f"checked_out={dbpool.get('checked_out')} "
                  f"trace_enabled={dbpool.get('trace_enabled', False)}\n")

            results = []
            for phase in ("nonstream", "stream_consumed", "stream_abandoned"):
                print(f"  running phase: {phase} …")
                r = await _run_phase(client, args.base, raw_key, args.model,
                                     phase, args.requests, args.concurrency,
                                     args.drain)
                results.append(r)
                lk = r["leaked"]
                tag = "LEAK" if (lk is not None and lk > 0) else "ok"
                print(f"    {phase}: checked_out {r['before']} → {r['after']} "
                      f"(Δ {lk}) [{tag}]")

            print("\n=== summary ===")
            leaking = [r for r in results if (r["leaked"] or 0) > 0]
            for r in results:
                print(f"  {r['phase']:18s} leaked={r['leaked']}")

            # Final trace summary if the tracer is on.
            health2 = await client.get(f"{args.base}/health", timeout=10.0)
            dbpool2 = (health2.json() or {}).get("dbPool") or {}
            if dbpool2.get("trace_enabled"):
                print(f"\n  tracer: {dbpool2.get('traced_checked_out')} connections "
                      f"checked out, oldest {dbpool2.get('oldest_checkout_age_sec')}s")
                print(f"  dump acquisition stacks (admin):")
                print(f"    curl -s -H 'x-api-key: <admin>' "
                      f"{args.base}/cluster/db-pool-trace | python3 -m json.tool")
            else:
                print("\n  tracer OFF — set DB_POOL_TRACE=1 on the node + recreate "
                      "the container, then re-run to capture leaking stacks.")

            if leaking:
                print(f"\n✗ LEAK REPRODUCED in: {', '.join(r['phase'] for r in leaking)}")
                return 1
            print("\n✓ no leak detected this run "
                  "(try more --requests, or a model on a different dispatch path)")
            return 0
    except Exception as e:
        print(f"\n✗ ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2
    finally:
        if key_id:
            try:
                async with AsyncSessionLocal() as db:
                    kk = await db.get(ApiKey, key_id)
                    if kk:
                        await db.delete(kk)
                        await db.commit()
                print("(temp key cleaned up)")
            except Exception as e:
                print(f"(cleanup error: {e})", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
