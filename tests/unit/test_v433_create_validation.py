"""v4.3.3 — Pydantic validators on POST /api/keys + POST /api/users.

Closes BUG-041 (`/api/keys` persisted negative `rate_limit_rpm`) and
BUG-042 (`/api/users` persisted users with empty password).

Both bugs were surfaced by the F2 coverage pass (2026-05-19); the fix
is a `Field(..., ge=0)` / `min_length=8` constraint at the request
schema layer so bad input is rejected before reaching the DB.

The PATCH (update) endpoints retain their original semantics (negative
caps = "clear" sentinel; empty password = "no change") — only CREATE
gets the boundary check, because a fresh resource has nothing to clear
and the empty-string case there represents a missing required value.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.apikeys import KeyCreate, KeyUpdate
from app.api.users import UserCreate, UserUpdate


# ── BUG-041 — KeyCreate rejects negative numeric fields ─────────────


def test_keycreate_rejects_negative_rate_limit_rpm():
    with pytest.raises(ValidationError) as ex:
        KeyCreate(name="x", rate_limit_rpm=-5)
    assert "greater than or equal to 0" in str(ex.value).lower() or \
        "ge" in str(ex.value).lower()


def test_keycreate_rejects_negative_spending_cap():
    with pytest.raises(ValidationError):
        KeyCreate(name="x", spending_cap_usd=-1.0)


def test_keycreate_rejects_negative_daily_soft_cap():
    with pytest.raises(ValidationError):
        KeyCreate(name="x", daily_soft_cap_usd=-0.01)


def test_keycreate_rejects_negative_daily_hard_cap():
    with pytest.raises(ValidationError):
        KeyCreate(name="x", daily_hard_cap_usd=-0.01)


def test_keycreate_rejects_negative_hourly_cap():
    with pytest.raises(ValidationError):
        KeyCreate(name="x", hourly_cap_usd=-100)


def test_keycreate_accepts_zero_rate_limit():
    """Zero is a meaningful (if drastic) value — explicit-block semantics."""
    k = KeyCreate(name="x", rate_limit_rpm=0)
    assert k.rate_limit_rpm == 0


def test_keycreate_accepts_positive_rate_limit():
    k = KeyCreate(name="x", rate_limit_rpm=60)
    assert k.rate_limit_rpm == 60


def test_keycreate_accepts_omitted_numeric_fields():
    """All cap/limit fields are Optional — omission means "no limit"."""
    k = KeyCreate(name="x")
    assert k.rate_limit_rpm is None
    assert k.spending_cap_usd is None
    assert k.daily_soft_cap_usd is None
    assert k.daily_hard_cap_usd is None
    assert k.hourly_cap_usd is None


def test_keyupdate_still_accepts_negative_one_as_clear_sentinel():
    """PATCH retains the documented "-1 to clear" semantic. This is the
    *update* model — not the *create* one we just hardened."""
    u = KeyUpdate(rate_limit_rpm=-1)
    assert u.rate_limit_rpm == -1
    u2 = KeyUpdate(spending_cap_usd=-1.0)
    assert u2.spending_cap_usd == -1.0


# ── BUG-042 — UserCreate rejects empty username / short password ────


def test_usercreate_rejects_empty_password():
    with pytest.raises(ValidationError) as ex:
        UserCreate(username="alice", password="")
    msg = str(ex.value).lower()
    assert "password" in msg
    assert "min" in msg or "length" in msg or "8" in msg


def test_usercreate_rejects_short_password():
    with pytest.raises(ValidationError):
        UserCreate(username="alice", password="short")


def test_usercreate_rejects_empty_username():
    with pytest.raises(ValidationError) as ex:
        UserCreate(username="", password="goodpassword")
    assert "username" in str(ex.value).lower()


def test_usercreate_accepts_normal_credentials():
    u = UserCreate(username="alice", password="atleast8chars")
    assert u.username == "alice"
    assert u.password == "atleast8chars"
    assert u.role == "user"


def test_usercreate_accepts_8_char_password_boundary():
    """The minimum is exactly 8 — confirm boundary is inclusive."""
    u = UserCreate(username="alice", password="12345678")
    assert u.password == "12345678"


def test_userupdate_still_treats_empty_password_as_no_change():
    """PATCH retains its "empty = no change" semantic via the falsy
    check in update_user (`if body.password:`). Confirm the schema
    accepts None and empty string."""
    u_none = UserUpdate(password=None)
    assert u_none.password is None
    # Empty-string is still accepted by the schema (the route-level
    # falsy check is what skips the update).
    u_empty = UserUpdate(password="")
    assert u_empty.password == ""
