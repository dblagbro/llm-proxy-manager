"""AIRI per-user notification preferences — v4.0.3.

The global alert mailbox (``settings.smtp_to``) always receives AIRI
notifications. On top of that, each operator can subscribe their own email
address and choose which categories and what minimum severity reach them.
This module is the service layer: read/write a user's preference and resolve
the set of personal recipients for a given notification.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select

from app.models.db import AiriNotificationPref

logger = logging.getLogger(__name__)

CATEGORIES = ("monitor", "automation")
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_MIN_SEVERITY_CHOICES = ("info", "warning", "critical")


def _rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "").lower(), 1)  # unknown -> warning


def _clean_categories(raw) -> dict:
    """Keep only known category keys, coerce to bool; default each to True."""
    raw = raw if isinstance(raw, dict) else {}
    return {c: bool(raw.get(c, True)) for c in CATEGORIES}


def _pref_dict(p: AiriNotificationPref | None, user_id: str) -> dict:
    """A UI-shaped dict — for a user with no row, a sensible default with
    ``configured=False`` so the panel can show 'not yet set up'."""
    if p is None:
        return {
            "user_id": user_id, "configured": False,
            "email": None, "enabled": True,
            "categories": _clean_categories(None), "min_severity": "warning",
        }
    return {
        "user_id": p.user_id, "configured": True,
        "email": p.email, "enabled": bool(p.enabled),
        "categories": _clean_categories(p.categories),
        "min_severity": p.min_severity or "warning",
    }


async def get_pref(db, user_id: str) -> dict:
    p = (await db.execute(
        select(AiriNotificationPref).where(AiriNotificationPref.user_id == user_id)
    )).scalar_one_or_none()
    return _pref_dict(p, user_id)


async def set_pref(db, user_id: str, *, email, enabled, categories,
                   min_severity: str) -> dict:
    """Upsert a user's preference. Returns the saved dict or {'error': ...}."""
    email = (email or "").strip() or None
    if email and ("@" not in email or len(email) > 320):
        return {"error": "that does not look like a valid email address"}
    min_severity = (min_severity or "warning").lower()
    if min_severity not in _MIN_SEVERITY_CHOICES:
        return {"error": f"min_severity must be one of {list(_MIN_SEVERITY_CHOICES)}"}
    cats = _clean_categories(categories)

    p = (await db.execute(
        select(AiriNotificationPref).where(AiriNotificationPref.user_id == user_id)
    )).scalar_one_or_none()
    if p is None:
        p = AiriNotificationPref(id=secrets.token_hex(8), user_id=user_id)
        db.add(p)
    p.email = email
    p.enabled = bool(enabled)
    p.categories = cats
    p.min_severity = min_severity
    await db.commit()
    logger.info("airi.notify_pref saved user=%s email=%s enabled=%s",
                user_id, "set" if email else "none", p.enabled)
    return await get_pref(db, user_id)


async def resolve_recipients(db, *, category: str, severity: str) -> set[str]:
    """Personal-subscription emails that should receive this notification —
    enabled, with an email, the category opted-in, and severity at/above the
    user's minimum. The global ``smtp_to`` is handled separately by the
    notifier; this is only the per-user layer."""
    rows = (await db.execute(
        select(AiriNotificationPref).where(AiriNotificationPref.enabled == True)  # noqa: E712
    )).scalars().all()
    want_rank = _rank(severity)
    out: set[str] = set()
    for p in rows:
        if not p.email:
            continue
        cats = _clean_categories(p.categories)
        if not cats.get(category, True):
            continue
        if want_rank < _rank(p.min_severity or "warning"):
            continue
        out.add(p.email.strip())
    return out
