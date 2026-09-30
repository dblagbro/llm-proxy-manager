#!/usr/bin/env python3
"""Run tests/unit with every non-loopback connection forbidden.

v5.22.38 — the unit suite being network-free is what lets CI gate on it, so it
is asserted rather than assumed. Historically it was NOT hermetic: session
fixtures in tests/conftest.py POSTed to the live deployment with admin
credentials, which is why CI ran a hand-picked list of 8 files for over a year
instead of the suite.

That is fixed, and this keeps it fixed. Any ``connect()`` to an address that is
not loopback raises, so the next accidental live fixture fails on the pull
request that introduces it rather than months later.

Exit code is pytest's, so this can stand in for a plain pytest call.

Run: python3 tools/run_unit_suite_hermetic.py [extra pytest args...]
"""
from __future__ import annotations

import socket
import sys

_real_connect = socket.socket.connect
_LOOPBACK_OK = ("::1", "localhost", "")


def _is_local(host: object) -> bool:
    if not isinstance(host, str):
        # AF_UNIX and friends pass a path; those never leave the machine.
        return True
    return host.startswith("127.") or host in _LOOPBACK_OK


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local(host):
        raise AssertionError(
            f"tests/unit attempted an outbound connection to {address!r}. "
            "The unit suite must be hermetic — CI gates on it and it has to "
            "pass on a runner with no access to any deployment. If the test "
            "genuinely needs one, it belongs in tests/integration (which boots "
            "its own instance), not here."
        )
    return _real_connect(self, address)


def main(argv: list[str]) -> int:
    socket.socket.connect = _guarded_connect
    import pytest  # imported after patching so its own imports are covered too
    return pytest.main(["-q", "-p", "no:cacheprovider", "tests/unit", *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
