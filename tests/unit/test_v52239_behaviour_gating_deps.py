"""v5.22.39 — the dependencies whose presence changes what the tests prove.

Two bugs reached CI red in v5.22.38 because this machine's environment differed
from a clean runner's, and both differences **silently flipped test outcomes**:

- **`http-sfv` was declared in `requirements.txt` but not installed here.**
  Without it `parse_hint` falls back to a comma-tolerant legacy parser, so four
  LMRH tests passed. Every real deployment has it and runs the strict RFC 8941
  path, where multi-value dims were being truncated — BUG-093, live since May,
  narrowing callers' `region=` sovereignty constraints. The tests that should
  have caught it *only passed because the dependency was missing.*
- **`fastapi` was older here than a clean runner resolves.** 0.141.1 stopped
  flattening included routers into `app.routes`, so six tests reported core
  endpoints unregistered — BUG-094.

A blanket "everything in requirements.txt must be installed" check would be
wrong: nine declared packages (asyncpg, redis, the OpenTelemetry trio, …) are
optional subsystems guarded by `ImportError`, and their absence changes nothing
about what the suite proves. The dangerous ones are the few that gate behaviour
under test, so those are listed explicitly, each with the reason it earns a
place. **Add an entry when you find a dependency whose absence or version makes
a test lie.**

A failure here does not mean the code is broken. It means this environment
cannot reproduce CI, so a green local run proves less than it appears to.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

# package -> why its presence/version changes test outcomes
BEHAVIOUR_GATING = {
    "http-sfv": (
        "gates the strict RFC 8941 branch of app/routing/lmrh/parse.py. Absent, "
        "the legacy comma-tolerant parser runs and the LMRH list tests pass "
        "while production (which has it) truncates multi-value dims — BUG-093."
    ),
    "fastapi": (
        "0.141.1 stopped flattening included routers into app.routes, changing "
        "what route-introspection tests see — BUG-094."
    ),
    "litellm": (
        "used as a library for dispatch and usage parsing. 26 test modules stub "
        "it in sys.modules; if the real package is absent the stub wins for the "
        "whole session and cost/usage assertions pass vacuously — BUG-088."
    ),
    "sqlalchemy": (
        "the asyncio extra pulls greenlet, which is required for every async DB "
        "path. Undeclared, CI failed to import the app at all — v5.22.27."
    ),
    "greenlet": (
        "platform-marker-gated in SQLAlchemy's own metadata, so it can be absent "
        "on a runner that resolves differently — v5.22.27."
    ),
    "bcrypt": (
        "password hashing is called directly (passlib crashes on 3.13). A version "
        "below the declared floor is a real prod/dev divergence on an auth path."
    ),
    "aiosqlite": (
        "the async SQLite driver every test DB uses; its connection threading is "
        "what produced the 'Event loop is closed' teardown noise."
    ),
    "pydantic": (
        "settings and request models; validation behaviour differs across majors."
    ),
}


def _declared_specifiers() -> dict:
    """name -> specifier, parsed from requirements.txt."""
    from packaging.requirements import Requirement

    out = {}
    for raw in Path("requirements.txt").read_text().splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        # sqlalchemy[asyncio] normalises to sqlalchemy
        out[req.name.lower()] = req.specifier
    return out


def test_every_gating_dependency_is_declared():
    """If it gates behaviour, requirements.txt must ask for it.

    greenlet is the exception: SQLAlchemy's ``[asyncio]`` extra pulls it, so it
    is required without being named directly.
    """
    declared = _declared_specifiers()
    undeclared = [
        name for name in BEHAVIOUR_GATING
        if name not in declared and name != "greenlet"
    ]
    assert not undeclared, (
        f"these gate test behaviour but requirements.txt does not declare them: "
        f"{undeclared}. An undeclared dependency is one CI may not install."
    )


@pytest.mark.parametrize("package", sorted(BEHAVIOUR_GATING))
def test_gating_dependency_is_installed(package):
    why = BEHAVIOUR_GATING[package]
    try:
        version(package)
    except PackageNotFoundError:
        pytest.fail(
            f"{package} is not installed in this environment.\n\n"
            f"Why it matters: {why}\n\n"
            f"A green run here proves less than it appears to, because CI "
            f"installs it and will behave differently. Fix with:\n"
            f"    pip install -r requirements.txt\n"
        )


@pytest.mark.parametrize("package", sorted(BEHAVIOUR_GATING))
def test_gating_dependency_satisfies_its_declared_range(package):
    """Installed version must satisfy requirements.txt.

    This is the check that would have caught the fastapi divergence: the floor
    was satisfied, but the *installed* version differed from what a clean runner
    resolves. A floor cannot express that, so the honest guarantee is only "in
    range" — the remaining gap is closed by running the suite on CI's
    interpreter before pushing (see Makefile: verify-ci).
    """
    from packaging.version import Version

    declared = _declared_specifiers()
    spec = declared.get(package)
    if spec is None or not str(spec):
        pytest.skip(f"{package} has no version constraint to check")
    try:
        installed = version(package)
    except PackageNotFoundError:
        pytest.skip("covered by test_gating_dependency_is_installed")
    assert spec.contains(Version(installed), prereleases=True), (
        f"{package} {installed} does not satisfy the declared {spec}.\n"
        f"Why it matters: {BEHAVIOUR_GATING[package]}\n"
        f"Fix with: pip install -r requirements.txt"
    )
