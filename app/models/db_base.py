"""SQLAlchemy declarative ``Base`` + the auth-session table.

Split out from ``db.py`` in v4.4.11 (file was 994 LOC and one ORM
table away from the project's 1,000-LOC ceiling). Every other
``db_*.py`` module imports ``Base`` from here.

The auth ``Session`` table lives with ``Base`` because:
- It has no FK relationships to other tables (self-contained).
- It's referenced by ``app/auth/*`` modules that don't import any
  other ORM models.
- Putting it here means the auth layer has one (cheap) import to
  reach both ``Base`` (rarely needed by auth) and ``Session``.
"""
from sqlalchemy import Column, String, Float
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Session(Base):
    """Persisted login sessions — survives container restarts."""
    __tablename__ = "sessions"

    token = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    username = Column(String, nullable=False)
    role = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)   # Unix timestamp
    last_seen_at = Column(Float, nullable=False)  # updated on each /me call
