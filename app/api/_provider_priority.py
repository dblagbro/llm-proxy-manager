"""Priority conflict resolution for provider updates."""

from sqlalchemy.ext.asyncio import AsyncSession


async def bump_priority_conflicts(
    db: AsyncSession,
    target_priority: int,
    *,
    exclude_id: str | None = None,
    provider_model,
    select_fn,
) -> int:
    """v2.8.2: when a provider takes priority P, bump every other provider
    already at P (and any chain-reaction conflicts) by +1 so the new/updated
    row gets the slot it asked for.

    Example: providers at 1,2,3,4,5,6. New provider asks for 2 →
    existing-2 → 3, existing-3 → 4, existing-4 → 5, existing-5 → 6,
    existing-6 → 7. Final order: 1, NEW@2, 3, 4, 5, 6, 7.

    ``exclude_id`` is the row that's TAKING the slot — exclude it from the
    conflict lookup so we don't bump our own row in the create-then-bump
    or PUT flow.

    Returns the number of rows bumped (for logging / response telemetry).
    """
    bumped = 0
    # Snapshot all candidates upfront so the iteration doesn't re-query inside
    # an open transaction (avoids autoflush quirks with in-memory SQLite).
    snap = (
        (
            await db.execute(
                select_fn(provider_model)
                .where(
                    (provider_model.id != exclude_id)
                    if exclude_id is not None
                    else (provider_model.id != "")
                )
                .order_by(
                    provider_model.priority.asc(),
                    provider_model.created_at.asc(),
                    provider_model.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    # Group by ORIGINAL priority — chain-reactions only fire when an original
    # row sits at the next priority. Already-bumped rows don't re-bump.
    by_priority: dict[int, list] = {}
    for row in snap:
        by_priority.setdefault(row.priority, []).append(row)

    current_priority = target_priority
    while current_priority in by_priority:
        for row in by_priority[current_priority]:
            row.priority = current_priority + 1
            bumped += 1
        # Done with this bucket — don't re-process the bumped rows on the
        # next iteration. Chain-reaction continues only if ORIGINAL rows
        # already sat at current_priority+1.
        del by_priority[current_priority]
        current_priority += 1
    return bumped


async def normalize_priority_ties(db: AsyncSession, *, provider_model, logger_name) -> int:
    """v2.8.2 / v3.0.24: resolve any priority ties among ROUTABLE providers
    by bumping the younger duplicates +1 in created_at order. Idempotent.
    Returns count of providers bumped (0 means already normalized).

    v3.0.24 (#136): scope changed to ``deleted_at IS NULL AND enabled=True``.
    Tombstoned and disabled rows don't participate in routing — letting them
    contribute to tie detection caused observed ping-pong on the first gateway (45 fires
    in 3h with count=2 each, while peers fired 0). Logs the bumped row IDs
    so future debugging knows what moved.

    Example before: [a@1, b@2, c@2, d@3, e@3] (b created before c, d before e)
    After: [a@1, b@2, c@3, d@4, e@5] — younger row at each tie shifts up,
    and the cascade from c=3 collides with d=3, so d→4. The net effect is
    a strict total order by (priority, created_at) within the active set.
    """
    from sqlalchemy import select as _select

    rows = (
        (
            await db.execute(
                _select(provider_model)
                .where(provider_model.deleted_at.is_(None), provider_model.enabled.is_(True))
                .order_by(
                    provider_model.priority.asc(),
                    provider_model.created_at.asc(),
                    provider_model.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    bumped: list[tuple[str, int, int]] = []  # (id, old, new)
    seen_priorities: set[int] = set()
    for row in rows:
        if row.priority not in seen_priorities:
            seen_priorities.add(row.priority)
            continue
        # Tie — find the next free slot at or above row.priority
        old_pri = row.priority
        new_pri = old_pri
        while new_pri in seen_priorities:
            new_pri += 1
        row.priority = new_pri
        seen_priorities.add(new_pri)
        bumped.append((row.id, old_pri, new_pri))
    if bumped:
        await db.flush()
        try:
            import logging as _logging

            _logging.getLogger(logger_name).info(
                "providers.normalize_priority_ties bumped=%d details=%s",
                len(bumped),
                [{"id": pid, "from": o, "to": n} for (pid, o, n) in bumped],
            )
        except Exception:
            pass
    return len(bumped)
