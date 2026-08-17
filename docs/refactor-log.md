# Refactor log — moved

**The canonical refactor log is [`../refactor-log.md`](../refactor-log.md) at the repo root.**

This file used to hold a second refactor log carrying the entry template plus four early
passes (R1-R5, 2026-05-09/10) that appeared **nowhere** in the root log. Same failure mode as
the duplicated bug logs: one name, two files, disjoint content, so anyone reading one was
missing history that only lived in the other.

On 2026-08-17 those four entries were merged into the root log under "Archived — early
refactor passes R1-R5". Nothing was dropped.

`AGENTS.md` names `refactor-log.md` as the history file. **Add new entries there, not here.**

The entry template now lives at the top of the root log.
