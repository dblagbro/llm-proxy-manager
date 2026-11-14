"""Offline acceptance-runner tests; subprocess is forbidden in every test."""

import contextlib
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import rehearse_nginx as h
from test_network_nginx import BRIDGE_ID, DEFAULT_ID, FIXTURE_ID, NGINX_ID, FakeHost


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No subprocess, Docker, SSH or network allowed in unit tests")

    monkeypatch.setattr(h.subprocess, "run", forbidden)


class AcceptanceFake(FakeHost):
    validate_nginx = h.r.Host.validate_nginx

    def __init__(self, folder):
        super().__init__(folder)
        self.baseline_ids = None
        self.networks = {
            DEFAULT_ID: {
                "Id": DEFAULT_ID,
                "Name": h.n.DEFAULT,
                "Containers": {NGINX_ID: {"Name": "nginx"}},
            }
        }
        self.extra = {}
        self.after_start = None
        self.inventory_reads = 0
        self.final_inventory_fail = False

    def rows(self):
        values = [self.nginx, self.bridge, *self.extra.values()]
        if self.fixture:
            values.append(self.fixture)
        return {v["Id"]: copy.deepcopy(v) for v in values}

    def run(self, args, timeout=180):
        if args[1:3] == ["container", "ls"] and "--filter" not in args:
            self.inventory_reads += 1
            if self.final_inventory_fail and self.fixture:
                raise RuntimeError("PRIVATE-DOCKER-OUTPUT")
            return "\n".join(self.rows())
        if args[1:3] == ["network", "ls"]:
            return "\n".join(self.networks)
        if args[1:3] == ["network", "inspect"]:
            return json.dumps([self.networks[cid] for cid in args[3:]])
        if args[1:3] == ["image", "inspect"]:
            return json.dumps([self.image(args[3])])
        if args[1] == "inspect":
            aliases = {"nginx": NGINX_ID, h.r.SERVICE: BRIDGE_ID}
            return json.dumps([self.rows()[aliases.get(cid, cid)] for cid in args[2:]])
        if args[1] == "create":
            assert self.baseline_ids == {NGINX_ID, BRIDGE_ID}
            assert (self.folder / "inventory-before.private.json").exists()
            assert (self.folder / "inputs.private.json").exists()
            assert (self.folder / "nginx.after").exists()
        result = super().run(args, timeout)
        if args[1] == "start" and self.after_start:
            self.after_start(self)
        return result


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    production = tmp_path / "production"
    canonical = production / "config/nginx/projects-locations.d/grok-bridge.conf"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"# old private fragment\n")
    (production / "docker-compose.yml").write_bytes(b"# PRIVATE-COMPOSE-CONTENT\nservices: {}\n")
    compiled = production / "nginx.conf"
    compiled.write_bytes(
        (
            "events {}\nhttp {\nserver {\n    "
            + h.r.MARKER
            + "\n    location /old { return 200; }\n"
            "    # ── from projects-locations.d/next.conf ──\n"
            "    location /unrelated { return 204; }\n}\n}\n"
        ).encode()
    )
    fragment = tmp_path / "nginx.grok-bridge.conf"
    fragment.write_bytes(
        b"location /grok-bridge { proxy_pass https://bridge:8443/vnc/websockify; }\n"
    )
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    monkeypatch.setattr(h.r, "ROOT", production)
    host = AcceptanceFake(folder)
    # Keep Linux Docker mount metadata even when these offline tests run on
    # Windows; only the synthetic input read maps to the temporary test file.
    original_reader = h.read_regular
    monkeypatch.setattr(
        h,
        "read_regular",
        lambda path: original_reader(
            compiled if path == Path("/private/live/nginx.conf") else path
        ),
    )
    monkeypatch.setattr(h.n.uuid, "uuid4", lambda: SimpleNamespace(hex="7" * 32))
    return host, fragment, canonical, compiled


def perform(acceptance, source=None):
    host, fragment, _, _ = acceptance
    return h.exercise(host, fragment, source or (lambda: {"commit": "a" * 40}))


def test_real_validator_method_runs_twice_reuses_and_retains_exact_fixture(acceptance):
    host, _, canonical, compiled = acceptance
    originals = canonical.read_bytes(), compiled.read_bytes()
    result = perform(acceptance)
    assert result["acceptance"] == "passed", result
    assert result["invariants"] == "passed" and result["fixture"]["id"] == FIXTURE_ID
    assert result["fixture"]["state"] == "stopped"
    assert sum(call[1] == "create" for call in host.calls) == 1
    assert sum(call[1] == "start" for call in host.calls) == 2
    assert not any(call[1] in {"rm", "stop", "pull", "build", "exec"} for call in host.calls)
    assert host.fixture is not None
    assert (canonical.read_bytes(), compiled.read_bytes()) == originals
    staged = (host.folder / "nginx.after").read_bytes()
    assert b"location /unrelated { return 204; }" in staged and b"location /old" not in staged
    before = json.loads((host.folder / "inventory-before.private.json").read_text())
    assert set(before["containers"]) == {NGINX_ID, BRIDGE_ID}
    assert "synthetic-secret" not in json.dumps(result)
    assert "PRIVATE-COMPOSE-CONTENT" not in json.dumps(result)
    assert result["reviewed_source"] == {"commit": "a" * 40}
    assert result["preexisting_counts"] == {"containers": 2, "networks": 1}
    assert result["input_sha256"]["canonical_compose"] == h.checksum(
        (h.r.ROOT / "docker-compose.yml").read_bytes()
    )
    assert result["input_sha256"]["staged_nginx"] == h.checksum(staged)
    assert not any("approved" in key or "gate" in key for key in result)


def test_guarded_real_host_and_helper_complete_two_runs_against_fake_daemon(
    acceptance, monkeypatch
):
    backend, fragment, _, _ = acceptance
    backend.baseline_ids = {NGINX_ID, BRIDGE_ID}
    guarded = h.GuardedHost(backend.folder)
    monkeypatch.setattr(h.r.Host, "run", lambda self, args, timeout=180: backend.run(args, timeout))
    result = h.exercise(guarded, fragment, lambda: {"commit": "a" * 40})
    assert result["acceptance"] == "passed", result
    assert guarded.create_calls == 1 and guarded.start_calls == 2
    assert result["fixture"]["id"] == FIXTURE_ID


@pytest.mark.parametrize(
    "fault",
    [
        "create-before-effect",
        "start-failed",
        "start-timeout",
        "stop-still-running",
        "runtime-changed",
        "stage-changed",
    ],
)
def test_failures_keep_evidence_and_audit_without_touching_production(acceptance, fault):
    host = acceptance[0]
    host.fault = fault
    before_reads = host.inventory_reads
    result = perform(acceptance)
    assert result["acceptance"] == "failed"
    assert host.inventory_reads > before_reads
    assert (host.folder / "inventory-after.private.json").exists()
    assert (host.folder / "failure.private.log").exists()
    assert "synthetic-private-diagnostic" not in json.dumps(result)
    for call in host.calls:
        if call[1] == "stop":
            assert call[-1] == FIXTURE_ID
        assert call[1] not in {"rm", "pull", "build", "exec", "restart", "network"}
    if fault != "create-before-effect":
        assert host.fixture is not None
    if fault in {"runtime-changed", "stage-changed", "stop-still-running"}:
        assert result["invariants"] == "failed"


def test_lost_create_receipt_reconciles_exact_fixture_and_reuses(acceptance):
    acceptance[0].fault = "create-after-effect"
    result = perform(acceptance)
    assert result["acceptance"] == "passed"
    assert sum(call[1] == "create" for call in acceptance[0].calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "config",
        "endpoint",
        "state",
        "network",
        "new-container",
        "canonical",
        "source",
        "compose",
    ],
)
def test_unrelated_drift_refuses_second_run_and_reports_invariants(acceptance, change):
    host, _, canonical, _ = acceptance
    source = {"commit": "a" * 40}

    def drift(value):
        if change == "config":
            value.bridge["Config"]["Labels"]["new"] = "concurrent-edit"
        elif change == "endpoint":
            value.bridge["NetworkSettings"]["Networks"][h.n.DEFAULT]["EndpointID"] = "changed"
        elif change == "state":
            value.bridge["State"]["Paused"] = True
        elif change == "network":
            value.networks[DEFAULT_ID]["Options"] = {"unreviewed": "true"}
        elif change == "new-container":
            row = copy.deepcopy(value.bridge)
            row.update(Id="9" * 64, Name="/unrelated-new")
            value.extra[row["Id"]] = row
        elif change == "canonical":
            canonical.write_bytes(b"concurrent edit")
        elif change == "source":
            source["commit"] = "b" * 40
        elif change == "compose":
            (h.r.ROOT / "docker-compose.yml").write_bytes(b"concurrent compose edit")

    host.after_start = drift
    result = perform(acceptance, lambda: copy.deepcopy(source))
    assert result["acceptance"] == "failed"
    assert result["invariants"] == "failed"
    assert sum(call[1] == "start" for call in host.calls) == 1


def test_input_audit_still_runs_after_final_docker_inventory_failure(acceptance):
    host, _, canonical, _ = acceptance

    def drift(value):
        value.final_inventory_fail = True
        canonical.write_bytes(b"changed privately")

    host.after_start = drift
    result = perform(acceptance)
    assert any(error.startswith("InventoryAudit:") for error in result["errors"])
    assert any(error.startswith("InputAudit:") for error in result["errors"])
    assert "PRIVATE-DOCKER-OUTPUT" not in json.dumps(result)


def test_health_probe_history_may_advance_but_health_status_may_not():
    original = {
        "Id": "a" * 64,
        "Name": "/test",
        "NetworkSettings": {},
        "State": {"Health": {"Status": "healthy", "Log": ["old"]}},
    }
    current = copy.deepcopy(original)
    current["State"]["Health"]["Log"] = ["new"]
    assert h.container_fingerprint(current) == h.container_fingerprint(original)
    current["State"]["Health"]["Status"] = "unhealthy"
    assert h.container_fingerprint(current) != h.container_fingerprint(original)


@pytest.mark.parametrize(
    "args",
    [
        ["docker", "rm", "anything"],
        ["docker", "network", "create", "anything"],
        ["docker", "network", "connect", "a", "b"],
        ["docker", "exec", "nginx", "nginx", "-s", "reload"],
        ["docker", "pull", "nginx"],
        ["docker", "build", "."],
        ["docker", "compose", "up"],
        ["ssh", "host"],
        ["docker", "--host", "tcp://host", "ps"],
    ],
)
def test_command_boundary_forbids_every_unrelated_mutation(tmp_path, monkeypatch, args):
    host = h.GuardedHost(tmp_path)
    host.baseline_ids = set()
    monkeypatch.setattr(h.n, "_load", lambda folder: {"id": None})
    with pytest.raises(h.Refused):
        host.run(args)


def test_command_boundary_forbids_mutation_before_private_baseline(tmp_path):
    with pytest.raises(h.Refused, match="baseline"):
        h.GuardedHost(tmp_path).run(["docker", "create"])


def test_command_boundary_uses_full_ledger_identity_and_never_stops_preexisting(
    acceptance, monkeypatch
):
    host = acceptance[0]
    assert perform(acceptance)["acceptance"] == "passed"
    guarded = h.GuardedHost(host.folder)
    guarded.baseline_ids = {NGINX_ID, BRIDGE_ID}
    monkeypatch.setattr(guarded, "inspect", host.inspect)
    calls = []
    monkeypatch.setattr(h.r.Host, "run", lambda self, args, timeout=180: calls.append(args))
    guarded.run(["docker", "stop", "--time", "5", FIXTURE_ID])
    assert calls == [["docker", "stop", "--time", "5", FIXTURE_ID]]
    with pytest.raises(h.Refused):
        guarded.run(["docker", "stop", "--time", "5", NGINX_ID])
    guarded.baseline_ids.add(FIXTURE_ID)
    with pytest.raises(h.Refused):
        guarded.run(["docker", "stop", "--time", "5", FIXTURE_ID])


def test_command_boundary_refuses_third_start_and_changed_owned_fixture(acceptance, monkeypatch):
    host = acceptance[0]
    assert perform(acceptance)["acceptance"] == "passed"
    guarded = h.GuardedHost(host.folder)
    guarded.baseline_ids = {NGINX_ID, BRIDGE_ID}
    monkeypatch.setattr(guarded, "inspect", host.inspect)
    monkeypatch.setattr(h.r.Host, "run", lambda *args, **kwargs: "")
    for _ in range(2):
        guarded.run(["docker", "start", "-a", FIXTURE_ID])
    with pytest.raises(h.Refused, match="two"):
        guarded.run(["docker", "start", "-a", FIXTURE_ID])
    host.fixture["Config"]["Labels"][h.n.OWNER] = "wrong"
    with pytest.raises(h.n.NginxValidationError):
        guarded.run(["docker", "stop", "--time", "5", FIXTURE_ID])


def test_cli_refuses_nonlinux_before_filesystem_or_commands(monkeypatch, capsys):
    monkeypatch.setattr(h.sys, "platform", "win32")
    assert (
        h.main(
            [
                "--source",
                "/private",
                "--expected-commit",
                "a" * 40,
                "--fragment",
                "/private/fragment",
                "--attempt",
                "/private/attempt",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "acceptance": "refused",
        "error_class": "Refused",
    }


def test_platform_requires_exact_guard_parent(monkeypatch):
    monkeypatch.setattr(h.sys, "platform", "linux")
    monkeypatch.setattr(h.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"python\0/tmp/capacity_guard.py\0")
    with pytest.raises(h.Refused, match="exact"):
        h.require_platform()
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: b"python\0" + h.os.fsencode(h.GUARD) + b"\0"
    )
    h.require_platform()


@pytest.mark.parametrize(
    "path",
    [
        "/mnt/s/code/private/nginx-test",
        "/mnt/s/backups/codex-security-20261109/nginx-test",
        "/mnt/s/backups/codex-private-security-20261109/nginx-validation/sub/test",
    ],
)
def test_attempt_cannot_enter_shared_source_or_p1_tree(monkeypatch, path):
    monkeypatch.setattr(h, "no_symlinks", lambda value: value)
    with pytest.raises(h.Refused, match="direct child"):
        h.validate_attempt(Path(path))


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    source = tmp_path / "source"
    deploy = source / h.DEPLOYMENT
    deploy.mkdir(parents=True)
    paths = [h.FRAGMENT, h.DEPLOYMENT / "rehearse_nginx.py"]
    monkeypatch.setattr(h, "__file__", str(source / paths[-1]))
    for name in h.MODULES:
        relative = h.DEPLOYMENT / (name + ".py")
        paths.append(relative)
        monkeypatch.setattr(h.sys.modules[name], "__file__", str(source / relative))
    for relative in paths:
        (source / relative).write_bytes(b"reviewed\n")

    def git(root, *args):
        assert root == source
        if args == ("rev-parse", "HEAD"):
            return ("a" * 40 + "\n").encode()
        if args == ("rev-parse", "--show-toplevel"):
            return (str(source) + "\n").encode()
        if args[0] == "status":
            return b""
        assert args[0] == "show"
        return b"reviewed\n"

    monkeypatch.setattr(h, "git_read", git)
    return source, git


def test_source_identity_binds_commit_all_modules_runner_and_fragment(source_tree):
    source, _ = source_tree
    result = h.source_identity(source, "a" * 40, source / h.FRAGMENT)
    assert result["commit"] == "a" * 40 and len(result["sha256"]) == len(h.MODULES) + 2


@pytest.mark.parametrize("fault", ["head", "dirty", "bytes", "foreign-module", "fragment"])
def test_source_identity_refuses_unreviewed_inputs(source_tree, monkeypatch, fault):
    source, git = source_tree
    fragment, commit = source / h.FRAGMENT, "a" * 40
    if fault == "head":
        commit = "b" * 40
    elif fault == "dirty":
        monkeypatch.setattr(
            h,
            "git_read",
            lambda root, *args: b" M private\n" if args[0] == "status" else git(root, *args),
        )
    elif fault == "bytes":
        fragment.write_bytes(b"concurrent edit")
    elif fault == "foreign-module":
        monkeypatch.setattr(h.n, "__file__", str(source / "other.py"))
    elif fault == "fragment":
        fragment = source / "other.conf"
    with pytest.raises(h.Refused):
        h.source_identity(source, commit, fragment)


def test_symlink_and_broken_symlink_are_rejected_without_following(tmp_path, monkeypatch):
    path = tmp_path / "unsafe"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path or original(self))
    with pytest.raises(h.Refused, match="Symlink"):
        h.no_symlinks(path)


def test_parent_creation_is_bounded_and_never_changes_shared_permissions(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    base = backup / "codex-private-security-20261109" / "nginx-validation"
    monkeypatch.setattr(h, "BASE", base)
    checked = []
    monkeypatch.setattr(h, "private_directory", checked.append)
    original = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *args, **kwargs: (
            original(tmp_path, *args, **kwargs)
            if self == Path("/mnt/s")
            else original(self, *args, **kwargs)
        ),
    )
    mode = backup.stat().st_mode
    attempt = base / "nginx-test"
    h.create_attempt(attempt)
    assert checked == [base.parent, base, attempt]
    assert backup.stat().st_mode == mode
    assert attempt.is_dir()


def test_run_holds_release_lock_before_any_directory_creation(tmp_path, monkeypatch):
    events = []
    args = SimpleNamespace(
        source=tmp_path,
        expected_commit="a" * 40,
        fragment=tmp_path / "fragment",
        attempt=tmp_path / "attempt",
    )
    monkeypatch.setattr(h, "require_platform", lambda: events.append("platform"))
    monkeypatch.setattr(h, "source_identity", lambda *args: events.append("source"))

    @contextlib.contextmanager
    def lock():
        events.append("lock")
        yield
        events.append("unlock")

    monkeypatch.setattr(h.r, "operation_lock", lock)
    monkeypatch.setattr(h, "validate_attempt", lambda path: events.append("validate-path-capacity"))
    monkeypatch.setattr(h, "create_attempt", lambda path: events.append("create-exact-dirs"))
    monkeypatch.setattr(h, "exercise", lambda *args: events.append("exercise") or {"ok": True})
    assert h.run(args) == {"ok": True}
    assert events == [
        "platform",
        "source",
        "lock",
        "validate-path-capacity",
        "create-exact-dirs",
        "exercise",
        "unlock",
    ]


def test_lock_refusal_prevents_private_directories_and_fixture_actions(tmp_path, monkeypatch):
    args = SimpleNamespace(
        source=tmp_path,
        expected_commit="a" * 40,
        fragment=tmp_path / "fragment",
        attempt=tmp_path / "attempt",
    )
    monkeypatch.setattr(h, "require_platform", lambda: None)
    monkeypatch.setattr(h, "source_identity", lambda *args: {})

    @contextlib.contextmanager
    def locked():
        raise h.Refused("Another maintenance owner holds lock")
        yield  # pragma: no cover

    monkeypatch.setattr(h.r, "operation_lock", locked)
    with pytest.raises(h.Refused, match="lock"):
        h.run(args)
    assert not args.attempt.exists()


def test_success_rechecks_exact_owned_fixture_exit_after_second_run(acceptance):
    host = acceptance[0]
    original_inventory = host.run
    checks = 0

    def inspect_after_run(args, timeout=180):
        nonlocal checks
        if args[1:3] == ["container", "ls"] and "--filter" not in args:
            checks += 1
            # Four snapshots precede/follow the two helper calls. The final
            # inventory starts after the successful second helper return.
            if checks == 7:
                host.fixture["State"]["ExitCode"] = 1
        return original_inventory(args, timeout)

    host.run = inspect_after_run
    result = perform(acceptance)
    assert result["acceptance"] == "failed"
    assert result["fixture"]["exit_code"] == 1


def test_short_inventory_ids_cannot_authorize_a_fixture(acceptance):
    host = acceptance[0]
    original_run = host.run
    host.run = lambda args, timeout=180: (
        "short-id" if args[1:3] == ["container", "ls"] else original_run(args, timeout)
    )
    result = perform(acceptance)
    assert result["acceptance"] == "failed" and host.fixture is None
    assert result["invariants"] == "unverified"


def test_source_failure_reports_unverified_identity_without_inventing_counts(
    acceptance,
):
    def fail_source():
        raise h.Refused("Private source context")

    result = perform(acceptance, fail_source)
    assert result["reviewed_source"] is None and result["preexisting_counts"] is None
    assert result["input_sha256"] == {} and result["invariants"] == "unverified"
    assert acceptance[0].fixture is None
    assert "Private source context" not in json.dumps(result)


def test_public_receipt_retains_source_hashes(acceptance):
    source = {"commit": "a" * 40, "sha256": {"public-source.py": "b" * 64}}
    result = perform(acceptance, lambda: copy.deepcopy(source))
    assert result["acceptance"] == "passed" and result["reviewed_source"] == source


def test_new_network_refuses_even_if_all_containers_unchanged(acceptance):
    host = acceptance[0]
    host.after_start = lambda value: value.networks.update(
        {"9" * 64: {"Id": "9" * 64, "Name": "unexpected"}}
    )
    result = perform(acceptance)
    assert result["acceptance"] == "failed" and result["invariants"] == "failed"


def test_only_owned_ephemeral_default_endpoint_is_tolerated(acceptance):
    host = acceptance[0]
    host.after_start = lambda value: value.networks[DEFAULT_ID]["Containers"].update(
        {FIXTURE_ID: {"Name": value.fixture["Name"].lstrip("/")}}
    )
    result = perform(acceptance)
    assert result["acceptance"] == "passed"


def damage_fixture_configuration(host):
    # Unlike Docker's false/null OomKillDisable representation, this is actual
    # unreviewed configuration drift after the original fingerprint was saved.
    host.fixture["HostConfig"]["OomScoreAdj"] = 123


@pytest.mark.parametrize("status", ["created", [], {}, None, "PRIVATE-INVALID-STATUS"])
def test_created_fixture_zero_exit_code_is_not_validation_success(acceptance, status):
    host = acceptance[0]

    def wrong_capability(fixture):
        fixture["HostConfig"]["CapAdd"] = ["CHOWN"]
        fixture["State"].update(Status=status, ExitCode=0)

    host.mutate = wrong_capability
    result = perform(acceptance)
    assert result["acceptance"] == "failed"
    assert result["fixture"] is None
    assert result["fixture_observation"]["docker_status"] == (
        "created" if status == "created" else "unknown"
    )
    assert result["fixture_observation"]["exit_code"] == 0
    assert result["fixture_observation"]["observation_only"] is True
    assert not any(c[1] == "start" for c in host.calls)


def test_bad_fixture_fingerprint_does_not_suppress_original_or_input_audits(acceptance):
    host = acceptance[0]
    host.after_start = damage_fixture_configuration
    result = perform(acceptance)
    assert result["acceptance"] == "failed" and result["invariants"] == "failed"
    assert result["fixture"] is None
    assert result["fixture_observation"] == {
        "name": host.fixture["Name"].lstrip("/"),
        "id": FIXTURE_ID,
        "state": "stopped",
        "exit_code": 0,
        "oom_killed": False,
        "configuration_verified": False,
        "observation_only": True,
        "docker_status": "unknown",
    }
    assert result["audit_status"] == {
        "inventory_capture": "passed",
        "inventory_evidence": "passed",
        "ownership": "failed",
        "preexisting_containers": "passed",
        "preexisting_networks": "passed",
        "inputs": "passed",
        "include_inputs": "passed",
        "source": "passed",
    }
    assert result["preexisting_audit"]["containers"]["checked"] == 2
    assert result["preexisting_audit"]["networks"]["checked"] == 1
    assert result["input_audit"]["checked"] == 5
    assert sum(call[1] == "start" for call in host.calls) == 1
    assert not any(call[1] == "stop" for call in host.calls)


def test_bad_fixture_still_reports_all_original_config_endpoint_and_lifecycle_changes(
    acceptance,
):
    host = acceptance[0]

    def drift(value):
        damage_fixture_configuration(value)
        value.nginx["Config"]["Labels"]["unexpected"] = "PRIVATE-DRIFT"
        value.bridge["NetworkSettings"]["Networks"][h.n.DEFAULT]["EndpointID"] = "changed"
        value.bridge["State"]["Paused"] = True

    host.after_start = drift
    result = perform(acceptance)
    assert result["acceptance"] == "failed"
    assert result["audit_status"]["ownership"] == "failed"
    assert result["preexisting_audit"]["containers"] == {
        "expected": 2,
        "checked": 2,
        "missing": 0,
        "configuration_changed": 1,
        "endpoints_changed": 1,
        "lifecycle_changed": 1,
        "unreadable": 0,
        "status": "failed",
    }
    assert result["audit_status"]["preexisting_networks"] == "passed"
    assert result["audit_status"]["inputs"] == "passed"
    assert result["audit_status"]["source"] == "passed"
    assert "PRIVATE-DRIFT" not in json.dumps(result)


def test_weaker_fixture_observation_does_not_exempt_its_network_endpoint(acceptance):
    host = acceptance[0]

    def drift(value):
        damage_fixture_configuration(value)
        value.networks[DEFAULT_ID]["Containers"][FIXTURE_ID] = {
            "Name": value.fixture["Name"].lstrip("/")
        }

    host.after_start = drift
    result = perform(acceptance)
    assert result["fixture_observation"]["id"] == FIXTURE_ID
    assert result["fixture"] is None
    assert result["audit_status"]["ownership"] == "failed"
    assert result["audit_status"]["preexisting_containers"] == "passed"
    assert result["preexisting_audit"]["networks"]["changed"] == 1
    assert result["audit_status"]["preexisting_networks"] == "failed"


def test_final_inventory_failure_still_independently_audits_files_and_source(
    acceptance,
):
    host, _, canonical, _ = acceptance
    source = {"commit": "a" * 40}

    def drift(value):
        value.final_inventory_fail = True
        canonical.write_bytes(b"PRIVATE-CONCURRENT-INPUT")
        source["commit"] = "b" * 40

    host.after_start = drift
    result = perform(acceptance, lambda: copy.deepcopy(source))
    assert result["audit_status"] == {
        "inventory_capture": "failed",
        "inventory_evidence": "unverified",
        "ownership": "unverified",
        "preexisting_containers": "unverified",
        "preexisting_networks": "unverified",
        "inputs": "failed",
        "include_inputs": "passed",
        "source": "failed",
    }
    for category in ("InventoryAudit:", "InputAudit:", "SourceAudit:"):
        assert any(error.startswith(category) for error in result["errors"])
    assert result["input_audit"]["checked"] == 5
    assert result["input_audit"]["changed"] == 1
    assert result["fixture_observation"] is None
    assert "PRIVATE-CONCURRENT-INPUT" not in json.dumps(result)
    assert "PRIVATE-DOCKER-OUTPUT" not in json.dumps(result)


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_weaker_observation_cannot_authorize_lifecycle_mutation(acceptance, monkeypatch, operation):
    host = acceptance[0]
    assert perform(acceptance)["acceptance"] == "passed"
    damage_fixture_configuration(host)
    observation = h.fixture_observation(host.folder, h.inventory(host), {NGINX_ID, BRIDGE_ID})
    assert observation["configuration_verified"] is False
    guarded = h.GuardedHost(host.folder)
    guarded.baseline_ids = {NGINX_ID, BRIDGE_ID}
    monkeypatch.setattr(guarded, "inspect", host.inspect)
    calls = []
    monkeypatch.setattr(h.r.Host, "run", lambda self, args, timeout=180: calls.append(args))
    args = (
        ["docker", "start", "-a", FIXTURE_ID]
        if operation == "start"
        else ["docker", "stop", "--time", "5", FIXTURE_ID]
    )
    with pytest.raises(h.n.NginxValidationError):
        guarded.run(args)
    assert calls == [] and guarded.start_calls == 0


@pytest.mark.parametrize("mismatch", ["id", "name", "owner", "preexisting"])
def test_weaker_observation_requires_persisted_full_id_name_and_owner(acceptance, mismatch):
    host = acceptance[0]
    assert perform(acceptance)["acceptance"] == "passed"
    current = h.inventory(host)
    before_ids = {NGINX_ID, BRIDGE_ID}
    if mismatch == "id":
        current["containers"][FIXTURE_ID]["Id"] = "9" * 64
    elif mismatch == "name":
        current["containers"][FIXTURE_ID]["Name"] += "-different"
    elif mismatch == "owner":
        current["containers"][FIXTURE_ID]["Config"]["Labels"][h.n.OWNER] = "not-owner"
    else:
        before_ids.add(FIXTURE_ID)
    assert h.fixture_observation(host.folder, current, before_ids) is None


def test_weaker_observation_never_adopts_matching_name_without_persisted_id(
    acceptance, monkeypatch
):
    host = acceptance[0]
    assert perform(acceptance)["acceptance"] == "passed"
    state = h.n._load(host.folder)
    state["id"] = None
    monkeypatch.setattr(h.n, "_load", lambda _: state)
    assert h.fixture_observation(host.folder, h.inventory(host), {NGINX_ID, BRIDGE_ID}) is None
    state["id"] = FIXTURE_ID[:12]
    assert h.fixture_observation(host.folder, h.inventory(host), {NGINX_ID, BRIDGE_ID}) is None


def test_failed_private_audit_log_does_not_suppress_later_checks(acceptance, monkeypatch):
    host = acceptance[0]
    host.after_start = damage_fixture_configuration
    original_write = h.r.private_write

    def cannot_write_ownership_log(path, data):
        if path.name == "audit-ownership.private.log":
            raise OSError("PRIVATE-EVIDENCE-FAILURE")
        return original_write(path, data)

    monkeypatch.setattr(h.r, "private_write", cannot_write_ownership_log)
    result = perform(acceptance)
    assert "EvidenceWriteAudit:OSError" in result["errors"]
    for category in (
        "preexisting_containers",
        "preexisting_networks",
        "inputs",
        "source",
    ):
        assert result["audit_status"][category] == "passed"
    assert result["fixture_observation"]["configuration_verified"] is False
    assert "PRIVATE-EVIDENCE-FAILURE" not in json.dumps(result)


def test_input_audit_continues_after_unreadable_file(monkeypatch):
    first, second = Path("first-private"), Path("second-private")
    visited = []

    def read(path):
        visited.append(path)
        if path == first:
            raise OSError("PRIVATE-ERROR")
        return b"changed"

    monkeypatch.setattr(h, "read_regular", read)
    audit = h.input_audit({first: b"first", second: b"second"})
    assert visited == [first, second]
    assert audit == {
        "expected": 2,
        "checked": 2,
        "changed": 1,
        "unreadable": 1,
        "status": "failed",
    }


def test_preexisting_audit_continues_after_malformed_original(acceptance):
    host = acceptance[0]
    before = host.rows()
    after = copy.deepcopy(before)
    after[NGINX_ID]["State"] = None
    after[BRIDGE_ID]["Config"]["Labels"]["changed"] = "yes"
    audit = h.preexisting_container_audit(before, after)
    assert audit["checked"] == 2 and audit["unreadable"] == 1
    assert audit["configuration_changed"] == 1 and audit["status"] == "failed"


def test_include_audit_runs_independently_when_fixture_validation_fails(acceptance, monkeypatch):
    host = acceptance[0]

    def changed_include(folder, original_nginx):
        raise h.n.NginxValidationError("Synthetic include changed")

    def damage(value):
        damage_fixture_configuration(value)
        monkeypatch.setattr(h.n, "verify_validation_inputs", changed_include)

    host.after_start = damage
    result = perform(acceptance)
    assert result["audit_status"]["ownership"] == "failed"
    assert result["audit_status"]["include_inputs"] == "failed"
    assert result["audit_status"]["preexisting_containers"] == "passed"
    assert result["audit_status"]["preexisting_networks"] == "passed"
    assert result["audit_status"]["inputs"] == "passed"
    assert result["audit_status"]["source"] == "passed"
    assert "IncludeInputAudit:NginxValidationError" in result["errors"]
    assert result["acceptance"] == result["invariants"] == "failed"
