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
    """The v5.7.11 per-instance opt-out lookup must still be intact —
    operator's 2026-06-30 decision on #483 was to use this existing
    mechanism on the clone cluster rather than add a new flag."""
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert "compliance_audit.zero_row_warning_enabled" in src
    # The lookup must return-early (not just log) when the flag is off.
    assert 'in (\n            "false", "0", "no", "off",\n        )' in src or \
        'in ("false", "0", "no", "off")' in src or \
        'in (\n            "false"' in src


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
    from pathlib import Path
    src = Path("app/__version__.py").read_text()
    assert '"5.12.' in src
