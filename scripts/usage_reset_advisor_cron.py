"""Run the usage-reset advisor check once (for cron on the host).

Same code path and same DB marker as the in-process worker, so a redundant
watcher never double-advises. Exit codes: 0 ok / disabled, 1 any account
unreadable or the run crashed.

Usage (inside the app environment, so DATABASE_URL / SMTP_* resolve):
    docker exec llm-proxy2 python /app/scripts/usage_reset_advisor_cron.py [--force]

``--force`` runs even when USAGE_RESET_ADVISOR_ENABLED is false. No crontab
is installed by this repo.
"""

from __future__ import annotations

import asyncio
import sys


async def _main(force: bool) -> int:
    from app.config import settings
    from app.monitoring.usage_reset_advisor import check_all_once

    if not settings.usage_reset_advisor_enabled and not force:
        print("usage_reset_advisor disabled (USAGE_RESET_ADVISOR_ENABLED=false); use --force")
        return 0
    results = await check_all_once()
    for r in results:
        print(f"{r['provider_id']}: {r['action']} - {r['reason']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main("--force" in sys.argv[1:])))
    except Exception as exc:
        print(f"usage_reset_advisor_cron failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
