"""User + system settings ORM models.

Split out from ``db.py`` in v4.4.11. Owns:

- ``User`` — admin/user accounts.
- ``SystemSetting`` — the runtime-tunable settings store.

The auth ``Session`` model lives in ``db_base.py`` (alongside Base
itself).
"""
import secrets

from sqlalchemy import Column, String, Float, DateTime, Text
from sqlalchemy.sql import func

from app.models.db_base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(8))
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")  # admin|user
    created_at = Column(DateTime, server_default=func.now())
    timezone = Column(String, nullable=True)      # IANA name; NULL = browser default
    time_format = Column(String, nullable=True)   # '12h'|'24h'|NULL = locale default
    # v5.0.22 — soft-delete tombstone + LWW timestamp for the same
    # reason api_keys / providers needed them (v3.0.20 / v2.8.2):
    # cluster sync's "insert-if-missing" merge resurrects deleted
    # users from peers that haven't seen the delete yet. BUG-070.
    deleted_at = Column(DateTime, nullable=True)
    last_user_edit_at = Column(Float, nullable=True)


class SystemSetting(Base):
    """Key/value store for runtime-tunable settings (overlays env-var defaults)."""
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)        # always stored as string
    value_type = Column(String, default="str")  # str|int|float|bool
    updated_at = Column(Float, default=0.0)     # Unix timestamp — used for last-write-wins sync
