"""v4.4.11 db.py refactor — invariants for the 10-module split.

The pre-split ``app/models/db.py`` was 994 LOC — one new ORM table
away from the project's de-facto 1,000-LOC ceiling. v4.4.11 split it
into ``db_base.py`` + 9 domain modules + a re-export shim at
``db.py``. This test guards two invariants:

1. **The re-export shim re-imports every model class.** If a future
   change adds a new model to one of the domain modules but forgets
   to add it to ``db.py``'s re-export block, existing
   ``from app.models.db import X`` callers will silently break. We
   compare the set of classes per module against ``db.__all__`` and
   fail if anything is missing.

2. **The registry is still complete.** ``Base.metadata.tables`` must
   contain all 32 tables after importing ``db.py``. Pre-split there
   were 32; this test pins that count so a new table addition that
   forgets the re-export-then-registry-population path is caught.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import inspect


def _classes_in(module_name: str) -> set[str]:
    """Return the names of all SQLAlchemy ORM classes (subclasses of
    Base) defined in the given module."""
    mod = importlib.import_module(module_name)
    from app.models.db_base import Base
    out = set()
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if obj is Base:
            continue
        # `Session` is in db_base — count it as part of db_base
        if issubclass(obj, Base) and obj.__module__ == module_name:
            out.add(name)
    return out


def test_all_domain_modules_load_cleanly():
    """Each domain module imports without errors."""
    for mod in (
        "app.models.db_base",
        "app.models.db_provider",
        "app.models.db_apikey",
        "app.models.db_user",
        "app.models.db_activity",
        "app.models.db_run",
        "app.models.db_lmrh",
        "app.models.db_oauth",
        "app.models.db_caller_memory",
        "app.models.db_airi",
        "app.models.db_compliance",
    ):
        importlib.import_module(mod)


def test_re_export_shim_includes_every_model_class():
    """``app.models.db.__all__`` must include every model class from
    every domain module — otherwise existing callers using
    ``from app.models.db import X`` will silently break when X
    moves to a new domain module."""
    from app.models import db

    expected = set()
    for mod_name in (
        "app.models.db_base",
        "app.models.db_provider",
        "app.models.db_apikey",
        "app.models.db_user",
        "app.models.db_activity",
        "app.models.db_run",
        "app.models.db_lmrh",
        "app.models.db_oauth",
        "app.models.db_caller_memory",
        "app.models.db_airi",
        "app.models.db_compliance",
    ):
        expected |= _classes_in(mod_name)
    # Also add Base itself
    expected.add("Base")

    declared = set(db.__all__)
    missing = expected - declared
    extra = declared - expected

    assert not missing, (
        f"app/models/db.py __all__ is missing these classes: {sorted(missing)}. "
        f"Add a `from app.models.db_<domain> import X` line AND list X in __all__."
    )
    assert not extra, (
        f"app/models/db.py __all__ has names that don't correspond to any "
        f"db_*.py model class: {sorted(extra)}."
    )


def test_registry_has_all_tables():
    """``Base.metadata.tables`` must contain every domain-module table.
    Pre-v5.0.0 the count was 32; v5.0.0 added 3 (compliance_events,
    compliance_policy_changes, compliance_audit_chain) → 35. If this
    count drops, a new table was likely added to a domain module but
    ``db.py`` doesn't import that domain module (so ``Base.metadata``
    never sees it)."""
    # Importing db.py triggers imports of every domain module
    from app.models import db  # noqa: F401
    from app.models.db_base import Base
    tables = set(Base.metadata.tables.keys())
    assert len(tables) == 35, (
        f"Expected 35 tables in Base.metadata, got {len(tables)}: "
        f"{sorted(tables)}"
    )


def test_no_domain_module_exceeds_500_loc():
    """Soft ceiling for the split modules — keeps any one domain from
    re-collecting into the same mass that triggered the refactor.
    If you hit this, split the domain again."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    domain_files = list((repo_root / "app" / "models").glob("db_*.py"))
    assert domain_files, "no db_*.py domain files found"
    too_big = []
    for f in domain_files:
        loc = sum(1 for _ in f.read_text().splitlines())
        if loc > 500:
            too_big.append((f.name, loc))
    assert not too_big, (
        f"these domain modules now exceed 500 LOC and should be re-split: "
        f"{too_big}"
    )
