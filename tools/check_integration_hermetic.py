#!/usr/bin/env python3
"""Fail if an integration test reaches a shared deployment without saying so.

v5.22.38 — the integration suite is now self-contained: it boots a private
throwaway instance (``LLMPROXY_TEST_EPHEMERAL=1``, see tests/_ephemeral.py)
rather than pointing at ``https://www.voipguru.org/llm-proxy2``. That property
is easy to lose one commit at a time, so it is asserted rather than trusted.

A test that genuinely needs the shared nginx estate — the v1 proxy, the
coordinator hub, paperless, real cluster peers — is fine. It just has to carry
the ``shared_deployment`` marker, which skips it unless the run explicitly
targets a real deployment. Without the marker it would quietly pass on any
runner with egress and quietly fail everywhere else, which is the situation this
whole change exists to end.

Checked per FILE, not per line: the marker usually sits on the class, so a
line-based grep cannot see it.

Run: python3 tools/check_integration_hermetic.py
"""
from __future__ import annotations

import pathlib
import sys

INTEGRATION_DIR = pathlib.Path("tests/integration")
# Hosts that mean "a deployment someone else is using".
SHARED_HOSTS = ("voipguru",)
_COMMENT_PREFIXES = ("#", '"""', "'''", "*")


def _live_references(src: str) -> list[tuple[int, str]]:
    """Lines naming a shared host that are not obviously comments or docstrings.

    Deliberately crude: a false positive costs someone a marker or a reworded
    comment, while a false negative costs a suite that only passes on one
    machine.
    """
    out = []
    for n, line in enumerate(src.splitlines(), 1):
        if not any(h in line for h in SHARED_HOSTS):
            continue
        if line.lstrip().startswith(_COMMENT_PREFIXES):
            continue
        out.append((n, line))
    return out


def main() -> int:
    offenders = []
    for path in sorted(INTEGRATION_DIR.glob("*.py")):
        src = path.read_text()
        hits = _live_references(src)
        if hits and "shared_deployment" not in src:
            offenders.append((path, hits))

    if not offenders:
        print("OK — every shared-deployment reference in tests/integration "
              "sits in a file marked shared_deployment")
        return 0

    print("These integration tests reach a shared deployment without the "
          "shared_deployment marker:\n")
    for path, hits in offenders:
        print(f"  {path}")
        for n, line in hits[:5]:
            print(f"    {n}: {line.strip()[:100]}")
        if len(hits) > 5:
            print(f"    ... and {len(hits) - 5} more")
    print("\nEither point the test at BASE_URL (which the ephemeral instance "
          "provides) or mark it @pytest.mark.shared_deployment.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
