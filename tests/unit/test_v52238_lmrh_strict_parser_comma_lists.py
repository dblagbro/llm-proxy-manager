"""v5.22.38 (BUG-093) — multi-value LMRH dims were truncated in production.

``parse_hint`` tries RFC 8941 first and falls back to a comma-tolerant legacy
parser. The trouble is that ``region=us,ca;require`` is *valid* 8941 — it just
means something else: key ``region`` = ``us``, plus a separate boolean member
``ca``. So the strict parser won and every multi-value dim lost everything after
the first comma:

    region=us,ca;require            -> region=us      (constraint narrowed)
    provider-hint=a,b               -> provider-hint=a
    task=reasoning, exclude=foo,bar -> exclude=foo

``region`` gates sovereignty routing, so this silently narrowed a caller's
declared compliance constraint.

**Why it stayed hidden for four months.** The tests asserting the list behaviour
only pass when ``http-sfv`` is *absent*, because then the strict parser returns
None and the legacy path runs. ``requirements.txt`` declares ``http-sfv>=0.9.9``,
so every real deployment has it — but the dev box did not, so locally the legacy
path always ran and the tests were green. It surfaced the first time CI gated
the full unit suite (v5.22.38): a clean runner installs the declared deps.

That is the lesson worth keeping: **a test that only passes when a declared
dependency is missing is not testing the deployed configuration.** The pin below
asserts the behaviour holds either way.
"""
from __future__ import annotations

import pytest

from app.routing.lmrh.parse import parse_hint

# (header, {dim: value}) — must hold whether or not http-sfv is installed.
CASES = [
    ("region=us,ca;require", {"region": "us,ca"}),
    ("provider-hint=claude-oauth,codex-oauth", {"provider-hint": "claude-oauth,codex-oauth"}),
    ("task=reasoning, exclude=foo,bar", {"task": "reasoning", "exclude": "foo,bar"}),
    # The strict InnerList spelling must agree with the comma spelling.
    ("region=(us ca);require", {"region": "us,ca"}),
    ("provider-hint=(a b c)", {"provider-hint": "a,b,c"}),
    # Single values and modifiers keep working.
    ("task=reasoning;require", {"task": "reasoning"}),
]


def _dims(header: str) -> dict:
    hint = parse_hint(header)
    return {d.key: d.value for d in (hint.dimensions if hint else [])}


@pytest.mark.parametrize("header,expected", CASES)
def test_multi_value_dims_survive_parsing(header, expected):
    got = _dims(header)
    for key, value in expected.items():
        assert got.get(key) == value, (
            f"{header!r}: {key} parsed as {got.get(key)!r}, expected {value!r}. "
            f"All dims: {got}"
        )


def test_require_modifier_survives_a_comma_list():
    """``;require`` on a multi-value dim must stay attached to that dim.

    Under the bug, 8941 attached ``;require`` to the trailing boolean member
    (``ca``), so the region constraint was both truncated AND no longer required.
    """
    hint = parse_hint("region=us,ca;require")
    assert hint is not None
    dim = hint.get("region")
    assert dim is not None and dim.value == "us,ca"
    assert dim.required is True


def test_no_phantom_dims_from_comma_separated_values():
    """The list members must not become dimensions of their own.

    ``region=us,ca`` produced two dims (``region`` and ``ca``) — the second a
    phantom that downstream scoring then tried to match against providers.
    """
    hint = parse_hint("region=us,ca;require")
    assert hint is not None
    assert [d.key for d in hint.dimensions] == ["region"]


def test_strict_parser_rejects_inputs_with_boolean_members():
    """The mechanism of the fix, pinned directly.

    LMRH has no boolean dimensions — every dim is ``key=value``, and
    ``require``/``sovereign`` are params. So a boolean member can only mean the
    input was a legacy comma list, and the strict parser must decline it so the
    legacy parser gets a turn.
    """
    from app.routing.lmrh.parse import _parse_hint_rfc8941

    pytest.importorskip("http_sfv", reason="strict path needs http-sfv")
    assert _parse_hint_rfc8941("region=us,ca;require") is None
    assert _parse_hint_rfc8941("task=reasoning, exclude=foo,bar") is None
    # A genuine 8941 header still takes the strict path.
    assert _parse_hint_rfc8941("task=reasoning;require") is not None


def test_declared_runtime_dependency_is_installed():
    """http-sfv is declared in requirements.txt, so the test environment must
    have it — otherwise the strict path above is never exercised at all and this
    whole file passes vacuously, which is exactly how BUG-093 survived.
    """
    from pathlib import Path

    declared = "http-sfv" in Path("requirements.txt").read_text()
    assert declared, "requirements.txt no longer declares http-sfv — update this test"
    pytest.importorskip(
        "http_sfv",
        reason=(
            "http-sfv is declared in requirements.txt but not installed here. "
            "Install it: the strict RFC 8941 parser is unreachable without it, "
            "so the LMRH tests silently stop testing the deployed configuration "
            "(this is how BUG-093 hid for four months). pip install -r requirements.txt"
        ),
    )
