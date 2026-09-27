"""v5.12.1 — hotfix bundle: #483 zero_row_streak silencer (data-side) +
#499 tmrwww02 cert deploy-hook (host-side).

Both items were operator-decision-pending before today's interview. The
silencer's code path already existed (v5.7.11 system_setting); operator
just needed to flip the flag on the clone cluster. The cert hook is a
host-side install (scripts + systemd unit) committed to the repo for
source-of-truth.

This test file pins the structural pieces; the live install is
verified via systemctl status on tmrwww02.
"""
from __future__ import annotations

from pathlib import Path


# ── #483 — silencer setting key already exists (v5.7.11) ─────────────


def test_compliance_audit_worker_honors_zero_row_warning_enabled_flag():
    """The #483 gate must still be the existing v5.7.11 setting key —
    operator's 2026-06-30 decision was to reuse it on the clone cluster
    rather than add a new flag.

    v5.22.34 — this used to grep for three hand-written whitespace variants
    of ``in ("false", "0", "no", "off")``. Two problems. It pinned
    *formatting*, so `ruff format` could break it; and v5.18.2 (2026-07-03)
    inverted the gate from opt-OUT to opt-IN per operator decision #483,
    making all three variants wrong at once. It had been parked in
    known_failures.txt since.

    Now: slice the gate function with the AST and match on normalised
    whitespace, so only a real change to the accepted values breaks it. The
    *behaviour* of the gate is covered by
    ``test_v5711_zero_row_warning_opt_out.py``; this test's job is only to
    pin that #483 is wired to this key and reads as opt-in.
    """
    import ast
    import re

    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert "compliance_audit.zero_row_warning_enabled" in src, (
        "#483 must stay wired to the existing v5.7.11 setting key"
    )

    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_emit_zero_row_warning_if_threshold"
    )
    lines = src.splitlines()
    gate = "\n".join(lines[fn.lineno - 1 : fn.end_lineno])
    compact = re.sub(r"\s+", "", gate)

    # v5.18.2 contract: truthy values opt IN; absent or anything else is silent.
    assert 'in("true","1","yes","on"' in compact, (
        "the zero-row gate is no longer an opt-IN check on "
        '("true","1","yes","on"). v5.18.2 made suppression the default per '
        "operator decision #483 — if this changed back to opt-out, the "
        "warning will fire ~1/day/cluster again, which is what #483 stopped."
    )
    # And it must return early rather than merely logging.
    assert "ifnotis_enabled:return" in compact, (
        "the gate must short-circuit the whole helper when not opted in"
    )


# ── #499 — host-side scripts committed to repo ────────────────────────


def test_cert_hook_scripts_present_in_repo():
    """The three scripts that live on tmrwww02 (deploy-hook + watcher
    + systemd unit) are version-controlled under ops-scripts/ so the
    operator can audit them without SSH'ing to the host."""
    base = Path("ops-scripts/tmrwww02-cert-hook")
    assert (base / "touch-nginx-restart-marker.sh").is_file()
    assert (base / "nginx-cert-restart-watcher.sh").is_file()
    assert (base / "nginx-cert-restart-watcher.service").is_file()
    assert (base / "README.md").is_file()


def test_cert_hook_deploy_script_writes_to_correct_marker_path():
    """The deploy-hook (runs INSIDE certbot container) must touch the
    bind-mounted path so the host-side watcher can see it."""
    script = Path("ops-scripts/tmrwww02-cert-hook/touch-nginx-restart-marker.sh").read_text()
    assert "MARKER=/etc/letsencrypt/.nginx-restart-needed" in script
    assert "touch \"$MARKER\"" in script


def test_cert_hook_watcher_uses_marker_then_deletes_it():
    """Watcher idempotency contract: restart, then delete the marker.
    Otherwise a runaway restart loop would burn nginx every 60s."""
    script = Path("ops-scripts/tmrwww02-cert-hook/nginx-cert-restart-watcher.sh").read_text()
    assert "/home/dblagbro/docker/config/certbot/conf/.nginx-restart-needed" in script
    assert "docker restart nginx" in script
    # Delete the marker AFTER successful restart.
    assert "rm -f \"$MARKER\"" in script


def test_cert_hook_systemd_unit_restart_policy():
    """`Restart=always` so the watcher survives crashes — a stuck
    watcher means the next renewal silently uses the stale cert."""
    unit = Path("ops-scripts/tmrwww02-cert-hook/nginx-cert-restart-watcher.service").read_text()
    assert "Restart=always" in unit
    assert "ExecStart=/home/dblagbro/docker/host-tools/nginx-cert-restart-watcher.sh" in unit


# ── version ──────────────────────────────────────────────────────────


def test_version_bumped():
    """v5.12.x line; exact patch pin lives in the next ship's test file."""
    import re
    from pathlib import Path
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m and (int(m[1]), int(m[2]), int(m[3])) >= (5, 12, 0), "expected >= 5.12.0"
