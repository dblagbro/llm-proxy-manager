"""Tiered per-tenant budget tracking (Wave 1 #5).

Three independent caps on each ApiKey:
- hourly_cap_usd   → burst control (429 Too Many Requests)
- daily_hard_cap_usd → daily hard stop (402 Payment Required)
- daily_soft_cap_usd → warning only (X-Budget-Warning response header)

Lifetime spending_cap_usd is preserved from v2.0.0 for backward compat.

Bucket counters live on the ApiKey row itself — no new table. Current-hour
and current-day bucket_ts is stored; if it doesn't match "now", the
corresponding cost is reset to zero before the cap check. This keeps the
check O(1) with no background sweep needed.
"""
from app.budget.tracker import (
    check_budget_pre_request,
    record_cost,
    warnings_for,
    BudgetStatus,
)

__all__ = [
    "check_budget_pre_request",
    "record_cost",
    "warnings_for",
    "BudgetStatus",
]
