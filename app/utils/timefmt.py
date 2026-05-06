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
    None passes through unchanged.

    v3.0.73: handle tz-aware datetimes correctly. Pre-fix this function did
    naive ``dt.isoformat() + "Z"`` which on a tz-aware UTC datetime produced
    ``"2026-01-15T12:30:45+00:00Z"`` — malformed (mixes ``+00:00`` and ``Z``)
    and rejected by strict ISO 8601 parsers. Production was unaffected
    because SQLAlchemy + ``func.now()`` stores naive datetimes server-side,
    but anywhere that passed a tz-aware value (tests, manual callsites)
    got a broken string. Now: strip ``+00:00`` if present before appending.
    """
    if dt is None:
        return None
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[:-6]
    return s + "Z"
