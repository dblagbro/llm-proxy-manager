"""Offline fake-host checks; no subprocess, Docker, network or real configuration."""

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import network_nginx as n

NGINX_ID = "1" * 64
BRIDGE_ID = "2" * 64
FIXTURE_ID = "3" * 64
DEFAULT_ID = "4" * 64
DEDICATED_ID = "5" * 64
IMAGE = "sha256:" + "6" * 64
TX = "7" * 32


def original(name, cid):
    return {
        "Name": "/" + name,
        "Id": cid,
        "Image": IMAGE,
        "Config": {"Env": ["PRIVATE=synthetic-secret"], "Labels": {}},
        "HostConfig": {"NetworkMode": "docker_default"},
        "Mounts": [
            {
                "Source": "/private/live/nginx.conf",
                "Destination": "/etc/nginx/nginx.conf",
                "Type": "bind",
                "RW": False,
            },
            {
                "Source": "/private/live/logs",
                "Destination": "/var/log/nginx",
                "Type": "bind",
                "RW": True,
            },
            {
                "Source": "/private/live/certs",
                "Destination": "/etc/certs",
                "Type": "bind",
                "RW": False,
            },
        ],
        "Path": "nginx",
        "Args": [],
        "NetworkSettings": {
            "Networks": {
                n.DEFAULT: {
                    "NetworkID": DEFAULT_ID,
                    "EndpointID": cid,
                    "Gateway": "192.0.2.1",
                }
            }
        },
        "State": {
            "Running": True,
            "Restarting": False,
            "ExitCode": 0,
            "OOMKilled": False,
        },
    }


class FakeHost:
    def __init__(self, folder):
        self.folder = folder
        self.nginx = original("nginx", NGINX_ID)
        self.bridge = original(n.BRIDGE, BRIDGE_ID)
        self.defaults = {
            "Env": ["PATH=/usr/bin"],
            "Labels": {"image": "nginx"},
            "User": "",
            "WorkingDir": "",
            "ExposedPorts": {"80/tcp": {}},
            "Volumes": None,
        }
        self.fixture = None
        self.calls = []
        self.fault = None
        self.mutate = None

    def image(self, image):
        assert image == IMAGE
        return {"Id": image, "Config": copy.deepcopy(self.defaults)}

    def inspect(self, name):
        if name in ("nginx", NGINX_ID):
            return copy.deepcopy(self.nginx)
        if name in (n.BRIDGE, BRIDGE_ID):
            return copy.deepcopy(self.bridge)
        assert self.fixture is not None and name == self.fixture["Id"]
        return copy.deepcopy(self.fixture)

    def build_fixture(self):
        state = json.loads((self.folder / n.LEDGER).read_text())
        hc = {
            "NetworkMode": DEFAULT_ID,
            "ReadonlyRootfs": True,
            "Memory": n.MEMORY,
            "NanoCpus": 500000000,
            "PidsLimit": 64,
            "CapDrop": ["ALL"],
            "OomKillDisable": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Runtime": "runc",
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": n.TMPFS,
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "1m", "max-file": "1"},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": mount["Source"],
                    "Target": mount["Destination"],
                    "ReadOnly": not mount["RW"],
                    "BindOptions": {
                        "Propagation": "rprivate",
                        **({"ReadOnlyForceRecursive": True} if not mount["RW"] else {}),
                    },
                }
                for mount in state["mounts"]
            ],
        }
        config = copy.deepcopy(self.defaults)
        config.update(
            Labels={**config["Labels"], n.OWNER: state["transaction"]},
            Image=IMAGE,
            Hostname=state["name"],
            Domainname="",
            Entrypoint=["nginx"],
            Cmd=["-t"],
        )
        self.fixture = {
            "Name": "/" + state["name"],
            "Id": FIXTURE_ID,
            "Image": IMAGE,
            "Path": "nginx",
            "Args": ["-t"],
            "Config": config,
            "HostConfig": hc,
            "Mounts": copy.deepcopy(state["mounts"]),
            "NetworkSettings": {"Networks": {n.DEFAULT: {"NetworkID": DEFAULT_ID}}},
            "State": {
                "Running": False,
                "Restarting": False,
                "ExitCode": 0,
                "OOMKilled": False,
            },
        }

    def run(self, args, timeout=180):
        self.calls.append(copy.deepcopy(args))
        assert args[0] == "docker"
        state = json.loads((self.folder / n.LEDGER).read_text())
        if args[1:3] == ["container", "ls"]:
            assert "name=^/" + state["name"] + "$" in args
            if self.fault == "lookup-fails":
                raise RuntimeError("synthetic-private-diagnostic")
            return self.fixture["Id"] + "\n" if self.fixture else ""
        if args[1] == "create":
            assert state["phase"] == "create_intent"
            assert state["id"] is None
            self.build_fixture()
            if self.mutate:
                self.mutate(self.fixture)
            if self.fault == "create-after-effect":
                raise TimeoutError("synthetic-private-diagnostic")
            if self.fault == "create-before-effect":
                self.fixture = None
                raise TimeoutError("synthetic-private-diagnostic")
            if self.fault == "wrong-create-receipt":
                return "9" * 64
            return FIXTURE_ID + "\n"
        if args[1] == "start":
            assert state["phase"] == "start_intent" and state["id"] == FIXTURE_ID
            assert args == ["docker", "start", "-a", FIXTURE_ID]
            self.fixture["HostConfig"]["OomKillDisable"] = None
            if self.fault in (
                "start-timeout",
                "stop-timeout-effect",
                "stop-still-running",
            ):
                self.fixture["State"]["Running"] = True
                raise TimeoutError("synthetic-private-diagnostic")
            if self.fault == "start-failed":
                self.fixture["State"]["ExitCode"] = 1
                raise RuntimeError("synthetic-private-diagnostic")
            if self.fault == "oom":
                self.fixture["State"]["OOMKilled"] = True
            if self.fault == "start-returned-running":
                self.fixture["State"]["Running"] = True
            if self.fault == "runtime-changed":
                self.nginx["NetworkSettings"]["Networks"][n.DEFAULT]["EndpointID"] = "changed"
            if self.fault == "stage-changed":
                (self.folder / "nginx.after").write_text("changed")
            return "nginx syntax valid"
        if args[1] == "stop":
            assert args == ["docker", "stop", "--time", "5", FIXTURE_ID]
            assert state["phase"] in {
                "stop_intent",
                "stop_interrupted_validation_intent",
            }
            if self.fault != "stop-still-running":
                self.fixture["State"]["Running"] = False
            if self.fault == "stop-timeout-effect":
                raise TimeoutError("synthetic-private-diagnostic")
            return FIXTURE_ID
        raise AssertionError("Unexpected command")


@pytest.fixture
def context(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    (tmp_path / "nginx.after").write_text("events {}\nhttp {}\n")
    monkeypatch.setattr(n.uuid, "uuid4", lambda: type("UUID", (), {"hex": TX})())
    host = FakeHost(tmp_path)
    return host, copy.deepcopy(host.nginx), copy.deepcopy(host.bridge)


def validate(context, **kwargs):
    host, nginx, bridge = context
    return n.validate_staged_nginx(host, host.folder / "nginx.after", nginx, bridge, **kwargs)


def ledger(host):
    return json.loads((host.folder / n.LEDGER).read_text())


def test_pre_cutover_check_creates_bounded_retained_fixture_and_reuses(context):
    host, _, _ = context
    assert validate(context) == FIXTURE_ID
    assert ledger(host)["phase"] == "passed_stopped_retained"
    create = next(args for args in host.calls if args[1] == "create")
    assert create[create.index("--network") + 1] == DEFAULT_ID
    assert DEDICATED_ID not in create and all("container:" not in arg for arg in create)
    assert "--read-only" in create and "--pull" in create and "never" in create
    assert "--cap-add" not in create
    mounts = [create[i + 1] for i, arg in enumerate(create) if arg == "--mount"]
    assert len(mounts) == 3
    assert sum(arg.endswith(",readonly,bind-recursive=readonly") for arg in mounts) == 2
    assert all("bind-propagation=rprivate" in arg for arg in mounts)
    assert all("synthetic-secret" not in arg for arg in create)
    assert validate(context) == FIXTURE_ID
    assert sum(args[1] == "create" for args in host.calls) == 1
    assert sum(args[1] == "start" for args in host.calls) == 2
    assert not any(args[1] in {"rm", "network", "exec"} for args in host.calls)
    assert n.retained_validation_ids(host.folder, [host.fixture, host.nginx]) == {FIXTURE_ID}


def test_explicit_nginx_second_network_allowed_without_fixture_membership(context):
    host, _, _ = context
    host.nginx["NetworkSettings"]["Networks"][n.DEDICATED] = {"NetworkID": DEDICATED_ID}
    assert validate(context, dedicated_network_id=DEDICATED_ID) == FIXTURE_ID
    assert set(host.fixture["NetworkSettings"]["Networks"]) == {n.DEFAULT}


@pytest.mark.parametrize(
    "change",
    [
        "nginx-id",
        "nginx-config",
        "nginx-endpoint",
        "nginx-extra",
        "bridge-id",
        "bridge-network",
        "bridge-stopped",
    ],
)
def test_live_preconditions_refuse_before_creation(context, change):
    host, _, _ = context
    if change == "nginx-id":
        host.nginx["Id"] = "8" * 64
    if change == "nginx-config":
        host.nginx["Config"]["Env"].append("X=private")
    if change == "nginx-endpoint":
        host.nginx["NetworkSettings"]["Networks"][n.DEFAULT]["EndpointID"] = "changed"
    if change == "nginx-extra":
        host.nginx["NetworkSettings"]["Networks"][n.DEDICATED] = {"NetworkID": DEDICATED_ID}
    if change == "bridge-id":
        host.bridge["Id"] = "8" * 64
    if change == "bridge-network":
        host.bridge["NetworkSettings"]["Networks"] = {n.DEDICATED: {"NetworkID": DEDICATED_ID}}
    if change == "bridge-stopped":
        host.bridge["State"]["Running"] = False
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not host.calls


@pytest.mark.parametrize("fault", ["start-timeout", "start-failed", "oom", "stop-timeout-effect"])
def test_failure_retains_stopped_exact_fixture_without_private_error(context, fault):
    host, _, _ = context
    host.fault = fault
    with pytest.raises(n.NginxValidationError) as error:
        validate(context)
    assert "synthetic-private-diagnostic" not in str(error.value)
    assert host.fixture["State"]["Running"] is False
    assert ledger(host)["phase"] == "stopped_retained"
    assert n.retained_validation_ids(host.folder, [host.fixture]) == {FIXTURE_ID}
    assert not any(args[1] == "rm" for args in host.calls)


def test_ambiguous_creation_adopts_only_strict_intended_fixture(context):
    host, _, _ = context
    host.fault = "create-after-effect"
    assert validate(context) == FIXTURE_ID
    assert ledger(host)["id"] == FIXTURE_ID
    assert sum(args[1] == "create" for args in host.calls) == 1


def test_creation_before_effect_can_resume_without_duplicate_resource(context):
    host, _, _ = context
    host.fault = "create-before-effect"
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert ledger(host)["id"] is None and host.fixture is None
    host.fault = None
    assert validate(context) == FIXTURE_ID


@pytest.mark.parametrize(
    "change",
    [
        "label",
        "image",
        "id",
        "mount",
        "privileged",
        "ports",
        "network",
        "namespace",
        "command",
        "alias",
    ],
)
def test_wrong_fixture_refuses_start_or_stop(context, change):
    host, _, _ = context
    validate(context)
    host.calls.clear()
    if change == "label":
        host.fixture["Config"]["Labels"][n.OWNER] = "other"
    if change == "image":
        host.fixture["Image"] = "sha256:" + "9" * 64
    if change == "id":
        host.fixture["Id"] = "9" * 64
    if change == "mount":
        host.fixture["Mounts"].append(
            {"Type": "bind", "Source": "/", "Destination": "/host", "RW": True}
        )
    if change == "privileged":
        host.fixture["HostConfig"]["Privileged"] = True
    if change == "ports":
        host.fixture["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8080"}]}
    if change == "network":
        host.fixture["NetworkSettings"]["Networks"][n.DEDICATED] = {"NetworkID": DEDICATED_ID}
    if change == "namespace":
        host.fixture["HostConfig"]["NetworkMode"] = "container:" + NGINX_ID
    if change == "command":
        host.fixture["Args"] = ["-g", "daemon off;"]
    if change == "alias":
        host.fixture["NetworkSettings"]["Networks"][n.DEFAULT]["Aliases"] = ["nginx"]
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not any(args[1] in {"start", "stop", "rm"} for args in host.calls)


def test_only_fixture_oom_default_normalization_survives_start(context):
    host, _, _ = context
    assert validate(context) == FIXTURE_ID
    before = copy.deepcopy(host.fixture)
    before["HostConfig"]["OomKillDisable"] = False
    assert host.fixture["HostConfig"]["OomKillDisable"] is None
    assert n._config(before) != n._config(host.fixture)
    assert n._fixture_config(before) == n._fixture_config(host.fixture)
    assert validate(context) == FIXTURE_ID
    assert sum(call[1] == "create" for call in host.calls) == 1


@pytest.mark.parametrize("value", [True, 0, "false"])
def test_nondefault_fixture_oom_setting_refuses_before_start(context, value):
    host, _, _ = context
    host.mutate = lambda fixture: fixture["HostConfig"].update(OomKillDisable=value)
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not any(call[1] == "start" for call in host.calls)


@pytest.mark.parametrize(
    "capabilities",
    [["DAC_READ_SEARCH"], ["DAC_OVERRIDE"], ["DAC_READ_SEARCH", "SYS_ADMIN"]],
)
def test_no_added_capability_is_allowed(context, capabilities):
    host, _, _ = context
    host.mutate = lambda fixture: fixture["HostConfig"].update(CapAdd=capabilities)
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not any(call[1] == "start" for call in host.calls)


@pytest.mark.parametrize("option", ["missing", "nonrecursive", "shared", "extra", "writable"])
def test_recursive_readonly_declaration_is_required(context, option):
    host, _, _ = context

    def mutate(fixture):
        mount = next(m for m in fixture["HostConfig"]["Mounts"] if m["ReadOnly"])
        if option == "missing":
            mount["BindOptions"].pop("ReadOnlyForceRecursive")
        elif option == "nonrecursive":
            mount["BindOptions"]["ReadOnlyNonRecursive"] = True
        elif option == "shared":
            mount["BindOptions"]["Propagation"] = "rshared"
        elif option == "extra":
            mount["BindOptions"]["NonRecursive"] = True
        else:
            mount["ReadOnly"] = False

    host.mutate = mutate
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not any(call[1] == "start" for call in host.calls)


def test_running_owned_fixture_is_not_excluded_and_resumed_safely(context):
    host, _, _ = context
    validate(context)
    host.fixture["State"]["Running"] = True
    with pytest.raises(n.NginxValidationError):
        n.retained_validation_ids(host.folder, [host.fixture])
    assert validate(context) == FIXTURE_ID
    assert any(args[1] == "stop" for args in host.calls)
    assert host.fixture["State"]["Running"] is False


def test_stop_failure_is_retained_as_unresolved_running(context):
    host, _, _ = context
    host.fault = "stop-still-running"
    with pytest.raises(n.NginxValidationError, match="remains running"):
        validate(context)
    assert ledger(host)["phase"] == "stop_intent"
    with pytest.raises(n.NginxValidationError):
        n.retained_validation_ids(host.folder, [host.fixture])


@pytest.mark.parametrize("fault", ["runtime-changed", "stage-changed"])
def test_changed_runtime_or_stage_during_check_is_not_success(context, fault):
    host, _, _ = context
    host.fault = fault
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert host.fixture["State"]["Running"] is False
    assert ledger(host)["phase"] != "passed_stopped_retained"


@pytest.mark.parametrize("missing", [True, False])
def test_omitted_image_user_is_explicit_empty_container_user(context, missing):
    host, _, _ = context
    if missing:
        host.defaults.pop("User")
    else:
        host.defaults["User"] = None
    host.mutate = lambda value: value["Config"].update(User="")
    assert validate(context) == FIXTURE_ID


def test_omitted_image_user_does_not_allow_container_user_override(context):
    host, _, _ = context
    host.defaults.pop("User")
    host.mutate = lambda value: value["Config"].update(User="1000")
    with pytest.raises(n.NginxValidationError, match="image defaults changed"):
        validate(context)
    assert not any(call[1] == "start" for call in host.calls)


def test_absent_or_replaced_retained_fixture_never_hidden(context):
    host, _, _ = context
    validate(context)
    with pytest.raises(n.NginxValidationError):
        n.retained_validation_ids(host.folder, [])
    forged = copy.deepcopy(host.fixture)
    forged["Id"] = "a" * 64
    with pytest.raises(n.NginxValidationError):
        n.retained_validation_ids(host.folder, [forged])


def test_declared_image_volume_refused_without_create(context):
    host, _, _ = context
    host.defaults["Volumes"] = {"/data": {}}
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not host.calls


def test_changed_stage_cannot_reuse_fixture(context):
    host, _, _ = context
    validate(context)
    host.calls.clear()
    (host.folder / "nginx.after").write_text("changed")
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not host.calls


def test_only_canonical_staged_file_allowed(context):
    host, nginx, bridge = context
    other = host.folder / "other.conf"
    other.write_text("same")
    with pytest.raises(n.NginxValidationError):
        n.validate_staged_nginx(host, other, nginx, bridge)
    assert not host.calls


def test_unknown_existing_scratch_refused(context):
    host, _, _ = context
    (host.folder / "nginx-validation-logs").mkdir()
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not host.calls


def test_failed_lookup_is_not_absence_and_error_is_sanitized(context):
    host, _, _ = context
    host.fault = "lookup-fails"
    with pytest.raises(n.NginxValidationError) as error:
        validate(context)
    assert "synthetic-private-diagnostic" not in str(error.value)
    assert not any(args[1] == "create" for args in host.calls)


def test_wrong_create_receipt_never_adopts_other_identity(context):
    host, _, _ = context
    host.fault = "wrong-create-receipt"
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not any(args[1] in {"start", "stop"} for args in host.calls)


def test_ambiguous_create_rejects_unreviewed_fixture_configuration(context):
    host, _, _ = context
    host.fault = "create-after-effect"
    host.mutate = lambda value: value["HostConfig"].update(Privileged=True)
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert not any(args[1] in {"start", "stop"} for args in host.calls)


def test_start_returning_with_running_fixture_cannot_pass_by_stopping_it(context):
    host, _, _ = context
    host.fault = "start-returned-running"
    with pytest.raises(n.NginxValidationError):
        validate(context)
    assert host.fixture["State"]["Running"] is False
    assert ledger(host)["error_class"] == "UnexpectedRunningValidation"


def test_other_prefixed_container_is_never_in_inventory_exclusion(context):
    host, _, _ = context
    validate(context)
    other = copy.deepcopy(host.fixture)
    other["Id"] = "9" * 64
    other["Name"] += "-other"
    assert n.retained_validation_ids(host.folder, [other, host.fixture]) == {FIXTURE_ID}


@pytest.mark.parametrize("schema", [True, 1.0, "1"])
def test_noninteger_ledger_schema_is_refused_before_fixture_action(context, schema):
    host, _, _ = context
    validate(context)
    state = ledger(host)
    state["schema"] = schema
    (host.folder / n.LEDGER).write_text(json.dumps(state))
    host.calls.clear()
    with pytest.raises(n.NginxValidationError, match="Invalid validation ledger"):
        validate(context)
    assert not host.calls


def test_stale_controller_cannot_overwrite_newer_ledger(context):
    host, _, _ = context
    validate(context)
    stale = n._load(host.folder)
    current = n._load(host.folder)
    current["phase"] = "newer_phase"
    n._save(host.folder, current)
    expected = (host.folder / n.LEDGER).read_bytes()
    stale["phase"] = "stale_phase"
    with pytest.raises(n.NginxValidationError, match="changed concurrently"):
        n._save(host.folder, stale)
    assert (host.folder / n.LEDGER).read_bytes() == expected
    assert "_ledger_sha256" not in ledger(host)


def test_interrupted_pending_write_preserves_durable_ledger(context, monkeypatch):
    host, _, _ = context
    validate(context)
    expected = (host.folder / n.LEDGER).read_bytes()
    state = n._load(host.folder)
    state["phase"] = "interrupted_phase"

    def partial_write(path, data):
        path.write_bytes(data[:10])
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(n, "_write_pending", partial_write)
    with pytest.raises(OSError):
        n._save(host.folder, state)
    assert (host.folder / n.LEDGER).read_bytes() == expected
    assert n._load(host.folder)["phase"] == "passed_stopped_retained"
    pending = list(host.folder.glob("nginx-validation.pending-*.private.json"))
    assert (
        len(pending) == 1
        and pending[0].read_bytes()
        == (
            json.dumps(
                {key: value for key, value in state.items() if key != "_ledger_sha256"},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()[:10]
    )


def test_failed_atomic_replace_preserves_old_ledger_and_pending_file(context, monkeypatch):
    host, _, _ = context
    validate(context)
    expected = (host.folder / n.LEDGER).read_bytes()
    state = n._load(host.folder)
    state["phase"] = "not_committed"

    def refuse_replace(*args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(n.os, "replace", refuse_replace)
    with pytest.raises(OSError):
        n._save(host.folder, state)
    assert (host.folder / n.LEDGER).read_bytes() == expected
    pending = list(host.folder.glob("nginx-validation.pending-*.private.json"))
    assert len(pending) == 1 and json.loads(pending[0].read_text())["phase"] == "not_committed"


def test_edit_between_pending_write_and_replace_refuses_overwrite(context, monkeypatch):
    host, _, _ = context
    validate(context)
    state = n._load(host.folder)
    state["phase"] = "stale_after_stage"
    write_pending = n._write_pending
    changed = ledger(host)
    changed["phase"] = "concurrent_change"

    def stage_then_edit(path, data):
        write_pending(path, data)
        (host.folder / n.LEDGER).write_text(json.dumps(changed))

    monkeypatch.setattr(n, "_write_pending", stage_then_edit)
    with pytest.raises(n.NginxValidationError, match="changed concurrently"):
        n._save(host.folder, state)
    assert ledger(host)["phase"] == "concurrent_change"
    assert len(list(host.folder.glob("nginx-validation.pending-*.private.json"))) == 1


@pytest.fixture
def include_context(context, monkeypatch):
    if os.name != "posix":
        pytest.skip("Real absolute Docker source paths require POSIX")
    host, _, bridge = context
    root = host.folder
    attempt = root / "attempt"
    attempt.mkdir(mode=0o700)
    (attempt / "nginx.after").write_bytes((root / "nginx.after").read_bytes())
    host.folder = attempt
    for target, suffix in n.inputs.TARGETS.items():
        source = root / suffix
        source.mkdir(mode=0o700)
        private_file = source / "private.conf"
        private_file.write_bytes(b"# synthetic private include\n")
        private_file.chmod(0o600)
        if target == "/etc/letsencrypt":
            (source / "archive").mkdir(mode=0o700)
            (source / "live").mkdir(mode=0o700)
            certificate = source / "archive/cert1.pem"
            certificate.write_bytes(b"synthetic certificate test input\n")
            certificate.chmod(0o600)
            (source / "live/cert.pem").symlink_to("../archive/cert1.pem")
        host.nginx["Mounts"].append(
            {"Destination": target, "Source": str(source), "Type": "bind", "RW": False}
        )
    # These synthetic trees belong to the CI user. The runtime module separately
    # enforces root; Linux descriptor/mode/atime checks remain active here.
    monkeypatch.setattr(n.inputs, "_private_owner", lambda info: None)
    monkeypatch.setattr(n.inputs.os, "geteuid", lambda: 0)
    return host, copy.deepcopy(host.nginx), bridge


def test_private_include_sources_are_copied_readonly_and_reused(include_context):
    host, original_nginx, _ = include_context
    assert validate(include_context) == FIXTURE_ID
    state = ledger(host)
    assert set(state["config_inputs"]["trees"]) == set(n.inputs.TARGETS)
    for mount in state["mounts"]:
        if mount["Destination"] in n.inputs.TARGETS:
            assert Path(mount["Source"]).is_relative_to(host.folder / n.inputs.COPY_ROOT)
            assert mount["RW"] is False
    assert validate(include_context) == FIXTURE_ID
    assert len([c for c in host.calls if c[1] == "create"]) == 1
    assert len([c for c in host.calls if c[1] == "start"]) == 2
    n.verify_validation_inputs(host.folder, original_nginx)


def test_certificate_link_resolves_inside_private_snapshot(include_context):
    host, _, _ = include_context
    validate(include_context)
    state = ledger(host)
    tls = next(m for m in state["mounts"] if m["Destination"] == "/etc/letsencrypt")
    private = host.folder / n.inputs.COPY_ROOT / "letsencrypt"
    assert Path(tls["Source"]) == private and tls["RW"] is False
    link = private / "live/cert.pem"
    assert link.is_symlink() and link.resolve() == private / "archive/cert1.pem"
    assert "CapAdd" not in host.fixture["HostConfig"]


def test_original_include_changed_after_create_prevents_start(include_context):
    host, _, _ = include_context
    source = Path(host.nginx["Mounts"][-1]["Source"]) / "private.conf"
    host.mutate = lambda fixture: source.write_bytes(b"changed by another writer\n")
    with pytest.raises(n.NginxValidationError):
        validate(include_context)
    assert host.fixture is not None and not host.fixture["State"]["Running"]
    assert not [c for c in host.calls if c[1] == "start"]


def test_private_include_drift_prevents_retained_fixture_restart(include_context):
    host, _, _ = include_context
    validate(include_context)
    copied = host.folder / n.inputs.COPY_ROOT / "conf.d/private.conf"
    copied.write_bytes(b"changed private copy\n")
    with pytest.raises(n.NginxValidationError):
        validate(include_context)
    assert len([c for c in host.calls if c[1] == "start"]) == 1
    assert not host.fixture["State"]["Running"]


def test_include_snapshot_cannot_be_rebound_to_another_source(include_context):
    host, _, _ = include_context
    validate(include_context)
    state = ledger(host)
    state["config_inputs"]["trees"]["/etc/nginx/conf.d"]["source"] = "/different/source"
    (host.folder / n.LEDGER).write_text(json.dumps(state))
    with pytest.raises(n.NginxValidationError):
        validate(include_context)
    assert len([c for c in host.calls if c[1] == "start"]) == 1
