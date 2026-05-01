"""UTC ISO formatter (v3.0.33).

Server stores naive UTC datetimes (SQLite + SQLAlchemy ``func.now()``).
``datetime.isoformat()`` on a naive value returns ``"2026-05-01T16:40:48"`` —
no timezone marker. JavaScript's ``new Date(...)`` parses unmarked strings
as **local time**, which made the activity log show times that were already
"in your zone" rather than getting converted from UTC. Operator-reported
2026-05-01: timezone preference set to Eastern, server time 16:40 UTC,
display showed 16:40 instead of 12:40.

Use ``utc_iso(dt)`` for any datetime that ships to the browser.
Cluster-sync paths can keep using bare ``isoformat()`` because peer code
parses both forms via ``v.replace("Z", "+00:00")``.
"""
from datetime import datetime
from typing import Optional


def utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Return ISO 8601 with explicit UTC marker (``Z``) so JS treats it as UTC.
    None passes through unchanged."""
    if dt is None:
        return None
    return dt.isoformat() + "Z"
