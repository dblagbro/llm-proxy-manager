"""v5.0.5 — apply_sync commits per major section.

2026-06-04 incident: a SLOW DEGRADATION on tmrwww01 walked the cluster-
sync apply time from 1.5s after the v5.0.4 deploy up to 19.6s over
10 hours of soak. SQLite's 10s busy_timeout meant any concurrent
write (the per-request provider_metrics rollup, an auth lookup, the
allowed_paths middleware) eventually started throwing
``sqlite3.OperationalError: database is locked``.

Root cause: pre-v5.0.5 ``apply_sync`` wrapped 12+ table sub-applies in
one transaction with a single trailing commit. The fix commits between
sections so the SQLite write lock is held in short bursts.

These tests pin the contract:
- ``apply_sync`` calls ``db.commit()`` multiple times during its run
  (not just at the end), one per major sub-section.
- A failure inside ``_section_commit`` rolls back THAT section's writes
  and continues to the next section instead of bubbling up + losing
  everything else (the legacy single-commit behavior).
- The final ``await db.commit()`` is no longer the only commit (catches
  a future regression where someone removes the section commits and
  reintroduces the single-transaction pattern).
"""
import inspect

import pytest

from app.cluster import sync as sync_mod


def test_apply_sync_source_calls_section_commit_helper_multiple_times():
    """Static inspection: the apply_sync source must include multiple
    `_section_commit(...)` calls, one per major sub-section. If a future
    refactor collapses them back into a single commit, the slow-write-
    lock regression returns."""
    src = inspect.getsource(sync_mod.apply_sync)
    # Count helper invocations — at least 8 distinct section labels
    # (users, api_keys, providers, settings, runs, lmrh group,
    # blocked_ips, compliance+tail …).
    n = src.count("await _section_commit(")
    assert n >= 8, (
        f"apply_sync committed only {n} times — v5.0.5 chunking has "
        f"regressed. Expected ≥8 section commits."
    )


def test_apply_sync_helper_swallows_commit_errors():
    """The helper logs + continues on commit failure so a transient
    section failure (e.g. a malformed peer payload) doesn't abort the
    entire sync. Static-grep for the rollback fallback so a refactor
    that removes the safety net is caught."""
    src = inspect.getsource(sync_mod.apply_sync)
    assert "section_commit_failed" in src, (
        "_section_commit no longer logs failures — partial-sync safety lost"
    )
    assert "rollback" in src, (
        "_section_commit no longer rolls back on commit failure — next "
        "section starts in a broken transaction state"
    )


@pytest.mark.asyncio
async def test_apply_sync_commits_at_least_4_times_on_empty_payload():
    """End-to-end behavior: even an empty payload triggers all the
    section commits (no early-return paths skip them). Counted via a
    monkeypatched commit method that records each call."""
    from app.models.database import AsyncSessionLocal
    commit_calls = []

    async with AsyncSessionLocal() as db:
        real_commit = db.commit

        async def counted_commit():
            commit_calls.append(True)
            return await real_commit()

        db.commit = counted_commit
        # Empty payload — every section's loop runs zero iterations,
        # but section commits still fire.
        await sync_mod.apply_sync(db, {})

    # At least 4 commits (we won't pin the exact number — future
    # refactors may add or remove sections — but the OLD single-commit
    # behavior would be exactly 1).
    assert len(commit_calls) >= 4, (
        f"apply_sync committed {len(commit_calls)} time(s) on empty "
        f"payload — single-transaction regression."
    )
