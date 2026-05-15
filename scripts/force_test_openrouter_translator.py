#!/usr/bin/env python3
"""Force-test OpenRouter cross-family translator fix (v3.9.16 P3a).

The fix in _oauth_chat_translate.py adds placeholder text for empty user
content to prevent OpenAI's "Invalid user message at index N" errors.

This script:
1. Creates a temporary API key for testing
2. Temporarily disables Anthropic providers to force OpenRouter routing
3. Sends a request with empty user content (the previously-failing shape)
4. Verifies success + translation header
5. Cleans up (re-enables providers, deletes test key)

Exit codes:
  0 = test passed
  1 = test failed (translator error or routing error)
  2 = setup/cleanup error
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.models.database import AsyncSessionLocal
from app.models.db import Provider, ApiKey
from app.auth.keys import _hash_key
import httpx
import secrets


async def main():
    api_key_id = None
    raw_key = None
    original_skip = {}

    try:
        async with AsyncSessionLocal() as db:
            # Step 1: Create temporary API key
            print("Step 1: Create temporary test API key...")
            raw_key = f"llmp-test-{secrets.token_hex(16)}"
            key_hash = _hash_key(raw_key)

            api_key = ApiKey(
                name="force-test-openrouter-translator",
                key_hash=key_hash,
                key_prefix=raw_key[:8],
                key_type="standard",
                enabled=True,
                encrypted_key=None,
            )
            db.add(api_key)
            await db.commit()
            await db.refresh(api_key)
            api_key_id = api_key.id
            print(f"  Created key: {raw_key[:12]}...")

            # Step 2: Disable Anthropic providers to force OpenRouter
            print("\nStep 2: Temporarily disable Anthropic providers...")
            anthropic_providers = (await db.execute(
                select(Provider)
                .where(Provider.provider_type == "anthropic")
                .where(Provider.enabled == True)
            )).scalars().all()

            skip_until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2)
            for p in anthropic_providers:
                original_skip[p.id] = p.auto_skip_until
                p.auto_skip_until = skip_until
                p.auto_skip_reason = "force-test-openrouter-translator"
            await db.commit()
            print(f"  Disabled {len(anthropic_providers)} Anthropic provider(s)")

        # Step 3: Send test request with empty user content
        print("\nStep 3: Send test request with empty user content...")
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "http://127.0.0.1:3000/v1/messages",
                headers={
                    "x-api-key": raw_key,
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "messages": [
                        {"role": "user", "content": ""}  # Empty - the failing shape
                    ],
                    "max_tokens": 10,
                }
            )

            print(f"  Status: {resp.status_code}")
            print(f"  Headers:")
            for k, v in resp.headers.items():
                if k.lower().startswith('x-'):
                    print(f"    {k}: {v}")

        # Step 4: Check activity log to see what actually served this
        async with AsyncSessionLocal() as db:
            from app.models.db import ActivityLog
            recent = (await db.execute(
                select(ActivityLog.provider_id, ActivityLog.message, ActivityLog.severity)
                .order_by(ActivityLog.created_at.desc())
                .limit(3)
            )).all()

            print(f"\n  Recent activity log:")
            for provider_id, msg, sev in recent:
                provider = await db.get(Provider, provider_id) if provider_id else None
                provider_name = provider.name if provider else "none"
                print(f"    [{sev}] {provider_name}: {msg[:60]}")

            # Step 5: Verify success
            if resp.status_code != 200:
                print(f"\n✗ FAILED: Expected 200, got {resp.status_code}")
                print(f"  Response: {resp.text[:500]}")
                return 1

            # The key test is that cross-family translation occurred
            if not resp.headers.get('x-cross-family-translated'):
                print(f"\n⚠ WARNING: x-cross-family-translated header not set")
                print(f"  Translation may not have occurred")
                return 1

            body = resp.json()
            if "content" not in body:
                print(f"\n✗ FAILED: Response missing content field")
                print(f"  Response: {body}")
                return 1

            print(f"\n✓ SUCCESS: Empty user content handled correctly")
            print(f"  - Cross-family translation applied: {resp.headers.get('x-cross-family-translated')}")
            print(f"  - Empty content placeholder prevented translator error")
            print(f"  - Response returned successfully (200)")
            return 0

    except Exception as e:
        print(f"\n✗ ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2

    finally:
        # Step 6: Cleanup
        print("\nStep 6: Cleanup...")
        try:
            async with AsyncSessionLocal() as db:
                # Re-enable Anthropic providers
                if original_skip:
                    for provider_id, orig_value in original_skip.items():
                        p = await db.get(Provider, provider_id)
                        if p:
                            p.auto_skip_until = orig_value
                            p.auto_skip_reason = None
                    await db.commit()
                    print(f"  Re-enabled {len(original_skip)} provider(s)")

                # Delete test key
                if api_key_id:
                    key = await db.get(ApiKey, api_key_id)
                    if key:
                        await db.delete(key)
                        await db.commit()
                    print(f"  Deleted test API key")
        except Exception as e:
            print(f"  Cleanup error: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
