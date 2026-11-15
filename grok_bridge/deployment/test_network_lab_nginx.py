"""Descriptor-bound Nginx preparation with synthetic raw inspections; no Docker."""

import copy
import json
import os
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import network_lab_context as lab
import network_nginx as n
import pytest
from test_network_lab_context import record
from test_network_nginx import BRIDGE_ID, DEFAULT_ID, FIXTURE_ID, NGINX_ID, FakeHost, original


class LabHost(FakeHost):
    def __init__(self, folder, context):
        super().__init__(folder)
        self.disk = folder
        self.folder = str(context.generation_root)
        self.context = context
        self.io = []
        self.callback = None
        names, pins = context.resource_map(), dict(context.images)
        for role, cid, pin in (("nginx", NGINX_ID, "nginx"), (n.BRIDGE, BRIDGE_ID, "original")):
            value = original(names[role], cid)
            value["Image"] = pins[pin]
            value["HostConfig"]["NetworkMode"] = names[n.DEFAULT]
            value["NetworkSettings"]["Networks"] = {
                names[n.DEFAULT]: value["NetworkSettings"]["Networks"][n.DEFAULT]
            }
            for mount in value["Mounts"]:
                mount["Source"] = str(context.generation_root / PurePosixPath(mount["Source"]).name)
            if role == "nginx":
                self.nginx = value
            else:
                self.bridge = value

    def image(self, image_id):
        self.io.append(("image", image_id))
        assert image_id == dict(self.context.images)["nginx"]
        return {"Id": image_id, "Config": copy.deepcopy(self.defaults)}

    def inspect(self, name):
        self.io.append(("inspect", name))
        values = [self.nginx, self.bridge, self.fixture]
        for value in values:
            if value and name in (value["Name"][1:], value["Id"]):
                return copy.deepcopy(value)
        raise AssertionError("Unexpected inspection")

    def build_fixture(self):
        state = self.ledger()
        hc = {
            "NetworkMode": DEFAULT_ID,
            "ReadonlyRootfs": True,
            "Memory": n.MEMORY,
            "NanoCpus": 500000000,
            "PidsLimit": 64,
            "CapDrop": ["ALL"],
            "CapAdd": ["CAP_CHOWN"],
            "OomKillDisable": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Runtime": "runc",
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": copy.deepcopy(n.TMPFS),
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
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
            Labels={
                **config["Labels"],
                n.OWNER: state["transaction"],
                n.LAB_OWNER: self.context.digest(),
            },
            Image=state["image"],
            Hostname=state["name"],
            Domainname="",
            Entrypoint=["nginx"],
            Cmd=["-t"],
        )
        self.fixture = {
            "Name": "/" + state["name"],
            "Id": FIXTURE_ID,
            "Image": state["image"],
            "Path": "nginx",
            "Args": ["-t"],
            "Config": config,
            "HostConfig": hc,
            "Mounts": copy.deepcopy(state["mounts"]),
            "NetworkSettings": {
                "Networks": {self.context.resource_map()[n.DEFAULT]: {"NetworkID": DEFAULT_ID}}
            },
            "State": {"Running": False, "Restarting": False, "ExitCode": 0, "OOMKilled": False},
        }

    def ledger(self):
        return json.loads((self.disk / n.LAB_LEDGER).read_text())

    def run(self, args, timeout=180):
        self.calls.append(copy.deepcopy(args))
        self.io.append(("run", args[1]))
        state = self.ledger()
        assert args[0] == "docker"
        if args[1:3] == ["container", "ls"]:
            assert "name=^/" + state["name"] + "$" in args
            result = self.fixture["Id"] if self.fixture else ""
        elif args[1] == "create":
            assert state["phase"] == "create_intent" and state["id"] is None
            self.build_fixture()
            result = FIXTURE_ID
        elif args[1] == "start":
            assert args == ["docker", "start", "-a", FIXTURE_ID]
            assert state["phase"] == "start_intent"
            self.fixture["HostConfig"]["OomKillDisable"] = None
            if self.fault == "start-timeout":
                self.fixture["State"]["Running"] = True
            result = "synthetic validation"
        elif args[1] == "stop":
            assert args == ["docker", "stop", "--time", "5", FIXTURE_ID]
            self.fixture["State"]["Running"] = False
            result = FIXTURE_ID
        else:
            raise AssertionError("Unexpected command")
        if self.callback:
            self.callback(args)
        if (args[1], self.fault) in {
            ("create", "create-after-effect"),
            ("start", "start-timeout"),
        }:
            raise TimeoutError("synthetic-private-diagnostic")
        return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Only filesystem anchoring is simulated; raw Docker records stay physical."""
    ctx = lab.from_record(record())
    tmp_path.chmod(0o700)
    for name in ("nginx.conf", "nginx.after"):
        (tmp_path / name).write_text("events {}\nhttp {}\n")
        (tmp_path / name).chmod(0o600)
    for name in ("logs", "certs"):
        (tmp_path / name).mkdir(mode=0o700)

    def anchored(logical, generation):
        assert generation == ctx.generation_root
        relative = logical.relative_to(generation)
        target = tmp_path.joinpath(*relative.parts)
        chain = []
        for item in [
            tmp_path,
            *(tmp_path.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)),
        ]:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise n.LabBindingError("Synthetic symlink refusal")
            chain.append((info.st_dev, info.st_ino, info.st_mode))
        return target, tuple(chain)

    monkeypatch.setattr(n, "_lab_filesystem_path", anchored)
    host = LabHost(tmp_path, ctx)
    return host, ctx


def validate(setup, **kwargs):
    host, ctx = setup
    return n.validate_staged_nginx(
        host,
        ctx.generation_root / "nginx.after",
        host.nginx,
        host.bridge,
        lab_context=ctx,
        **kwargs,
    )


def test_exact_raw_physical_fixture_is_retained_reused_and_context_bound(setup):
    host, ctx = setup
    before = copy.deepcopy((host.nginx, host.bridge))
    assert validate(setup) == validate(setup) == FIXTURE_ID
    state = host.ledger()
    assert state["schema"] == 2 and state["mode"] == "lab"
    ctx.verify_journal_binding(state["lab_context"])
    assert state["phase"] == "passed_stopped_retained"
    assert before == (host.nginx, host.bridge)
    assert not (host.disk / n.LEDGER).exists()
    assert len([a for a in host.calls if a[1] == "create"]) == 1
    assert len([a for a in host.calls if a[1] == "start"]) == 2
    assert not any("rm" in a or "prune" in a or "connect" in a for a in host.calls)
    assert host.fixture["HostConfig"]["CapAdd"] == ["CAP_CHOWN"]
    assert host.fixture["HostConfig"]["ReadonlyRootfs"] is True
    assert set(host.fixture["NetworkSettings"]["Networks"]) == {ctx.resource_map()[n.DEFAULT]}
    assert [m["Destination"] for m in host.fixture["Mounts"] if m["RW"]] == ["/var/log/nginx"]
    n.verify_validation_inputs(host.folder, host.nginx, lab_context=ctx)
    assert n.retained_validation_ids(host.folder, [host.fixture], lab_context=ctx) == {FIXTURE_ID}


@pytest.mark.parametrize("field", ["vm_uuid", "vm_name", "engine_id", "source_commit", "images"])
@pytest.mark.parametrize("api", ["validate", "inputs", "retained"])
def test_cold_resume_full_context_drift_refuses_same_namespace_before_io(setup, field, api):
    host, ctx = setup
    validate(setup)
    changes = {
        "vm_uuid": "12345678-1234-1234-1234-123456789abd",
        "vm_name": "codex-grok-lab-other",
        "engine_id": "other-engine",
        "source_commit": "c" * 40,
        "images": tuple(
            (role, "sha256:" + "4" * 64 if role == "candidate" else pin) for role, pin in ctx.images
        ),
    }
    other = replace(ctx, **{field: changes[field]})
    assert other.generation_root == ctx.generation_root
    prior_io, prior_ledger = copy.deepcopy(host.io), (host.disk / n.LAB_LEDGER).read_bytes()
    with pytest.raises(n.LabBindingError):
        if api == "validate":
            validate((host, other))
        elif api == "inputs":
            n.verify_validation_inputs(host.folder, host.nginx, lab_context=other)
        else:
            n.retained_validation_ids(host.folder, [host.fixture], lab_context=other)
    assert host.io == prior_io and (host.disk / n.LAB_LEDGER).read_bytes() == prior_ledger


@pytest.mark.parametrize(
    "change", ["volume", "outside", "traversal", "duplicate-slash", "relative", "root"]
)
def test_non_generation_sources_refuse_before_copy_or_host(setup, monkeypatch, change):
    host, ctx = setup
    mount = host.nginx["Mounts"][0]
    if change == "volume":
        mount["Type"] = "volume"
    else:
        mount["Source"] = {
            "outside": "/etc/letsencrypt",
            "traversal": str(ctx.generation_root) + "/../private",
            "duplicate-slash": str(ctx.generation_root) + "//nginx.conf",
            "relative": "nginx.conf",
            "root": str(ctx.generation_root),
        }[change]
    copied = []
    monkeypatch.setattr(n.inputs, "prepare", lambda *args: copied.append(args))
    with pytest.raises(n.LabBindingError):
        validate(setup)
    assert host.io == [] and copied == []
    assert not (host.disk / n.LAB_LEDGER).exists()


@pytest.mark.parametrize(
    "role,field",
    [
        ("nginx", "Name"),
        ("nginx", "Image"),
        ("bridge", "Name"),
        ("bridge", "Image"),
        ("nginx", "NetworkSettings"),
    ],
)
def test_original_physical_identity_and_pins_refuse_before_host(setup, role, field):
    host, _ = setup
    target = getattr(host, role)
    target[field] = {} if field == "NetworkSettings" else "unreviewed"
    with pytest.raises(n.LabBindingError):
        validate(setup)
    assert host.io == []


@pytest.mark.parametrize("supplied", [None, {}, False, "lab"])
def test_missing_or_malformed_context_never_reaches_host(setup, supplied):
    host, ctx = setup
    with pytest.raises(n.LabBindingError):
        n.validate_staged_nginx(
            host, ctx.generation_root / "nginx.after", host.nginx, host.bridge, lab_context=supplied
        )
    assert host.io == []


@pytest.mark.parametrize("opposite", [n.LEDGER, n.LAB_LEDGER])
def test_opposite_mode_ledger_cannot_be_ignored(setup, opposite):
    host, ctx = setup
    (host.disk / opposite).write_text("{}")
    (host.disk / opposite).chmod(0o600)
    with pytest.raises(n.LabBindingError):
        if opposite == n.LEDGER:
            validate(setup)
        else:
            n.retained_validation_ids(host.disk, [])
    assert host.io == []


@pytest.mark.parametrize("point", ["create", "start"])
@pytest.mark.parametrize("drift", ["ledger", "folder", "nginx", "bridge", "context"])
def test_callback_drift_is_not_rebased_or_swallowed_and_never_reconciled(setup, point, drift):
    host, ctx = setup
    after = []

    def callback(args):
        if args[1] != point:
            return
        if drift == "ledger":
            state = host.ledger()
            state["phase"] = "external-edit"
            (host.disk / n.LAB_LEDGER).write_text(json.dumps(state))
        elif drift == "folder":
            host.folder += "-other"
        elif drift in ("nginx", "bridge"):
            getattr(host, drift)["Config"]["Env"].append("CHANGED=1")
        else:
            object.__setattr__(ctx, "engine_id", "changed-engine")
        after.extend(copy.deepcopy(host.io))
        if point == "start":
            host.fixture["State"]["Running"] = True
        raise TimeoutError("synthetic-private-diagnostic")

    host.callback = callback
    with pytest.raises(n.LabBindingError) as caught:
        validate(setup)
    assert after and host.io == after
    assert "synthetic-private" not in str(caught.value)
    assert not any(args[1] == "stop" for args in host.calls)
    assert host.fixture is not None
    if drift == "ledger":
        assert host.ledger()["phase"] == "external-edit"


@pytest.mark.parametrize("fault", ["create-after-effect", "start-timeout"])
def test_ambiguous_effect_reconciles_only_exact_bound_fixture(setup, fault):
    host, ctx = setup
    host.fault = fault
    if fault == "create-after-effect":
        assert validate(setup) == FIXTURE_ID
    else:
        with pytest.raises(n.NginxValidationError, match="fixture retained"):
            validate(setup)
        assert [args for args in host.calls if args[1] == "stop"] == [
            ["docker", "stop", "--time", "5", FIXTURE_ID]
        ]
    assert host.fixture["State"]["Running"] is False
    assert n.retained_validation_ids(host.folder, [host.fixture], lab_context=ctx) == {FIXTURE_ID}


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 1),
        ("schema", 2.0),
        ("mode", "production"),
        ("lab_context", None),
    ],
)
def test_lab_receipt_tampering_refuses_before_host(setup, field, value):
    host, _ = setup
    validate(setup)
    state = host.ledger()
    state[field] = value
    (host.disk / n.LAB_LEDGER).write_text(json.dumps(state))
    host.io.clear()
    with pytest.raises(n.LabBindingError):
        validate(setup)
    assert host.io == []


@pytest.mark.parametrize(
    "change", ["label", "network", "capability", "writable", "recursive", "running"]
)
def test_retained_lookup_keeps_strict_whole_inspection_contract(setup, change):
    host, ctx = setup
    validate(setup)
    value = host.fixture
    if change == "label":
        value["Config"]["Labels"][n.LAB_OWNER] = "0" * 64
    elif change == "network":
        value["NetworkSettings"]["Networks"][n.DEFAULT] = {"NetworkID": DEFAULT_ID}
    elif change == "capability":
        value["HostConfig"]["CapAdd"].append("DAC_READ_SEARCH")
    elif change == "writable":
        value["Mounts"][0]["RW"] = True
    elif change == "recursive":
        value["HostConfig"]["Mounts"][0]["BindOptions"].pop("ReadOnlyForceRecursive")
    else:
        value["State"]["Running"] = True
    with pytest.raises(n.NginxValidationError):
        n.retained_validation_ids(host.folder, [value], lab_context=ctx)


def test_pending_write_drift_preserves_external_ledger_and_private_pending(setup, monkeypatch):
    host, _ = setup
    original_write = n._write_pending

    def race(path, data):
        original_write(path, data)
        (host.disk / n.LAB_LEDGER).write_text('{"external":true}')

    monkeypatch.setattr(n, "_write_pending", race)
    with pytest.raises(n.LabBindingError):
        validate(setup)
    assert (host.disk / n.LAB_LEDGER).read_text() == '{"external":true}'
    assert list(host.disk.glob("nginx-validation.pending-*"))
    assert not host.calls


def test_binding_refusal_latches_even_when_guard_fields_are_unchanged(setup, monkeypatch):
    host, _ = setup
    original_save = n._save
    calls_at_refusal = []

    def refuse_start(folder, state, **kwargs):
        if state["phase"] == "start_intent":
            calls_at_refusal.extend(copy.deepcopy(host.io))
            raise n.LabBindingError("Synthetic receipt refusal")
        return original_save(folder, state, **kwargs)

    monkeypatch.setattr(n, "_save", refuse_start)
    with pytest.raises(n.LabBindingError, match="fixture retained"):
        validate(setup)
    assert host.io == calls_at_refusal
    assert host.ledger()["phase"] == "created_retained"
    assert not any(args[1] in {"start", "stop"} for args in host.calls)


def test_production_save_refuses_lab_record_before_pending_write(setup):
    host, _ = setup
    validate(setup)
    original_files = {p.name: p.read_bytes() for p in host.disk.iterdir() if p.is_file()}
    with pytest.raises(n.LabBindingError):
        n._save(host.disk, host.ledger())
    assert {p.name: p.read_bytes() for p in host.disk.iterdir() if p.is_file()} == original_files


@pytest.mark.parametrize("api", ["inputs", "retained"])
def test_public_audits_use_pinned_receipt_without_reloading(setup, monkeypatch, api):
    host, ctx = setup
    validate(setup)
    real_load = n._load
    calls = []

    def once(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(n, "_load", once)
    if api == "inputs":
        n.verify_validation_inputs(host.folder, host.nginx, lab_context=ctx)
    else:
        n.retained_validation_ids(host.folder, [host.fixture], lab_context=ctx)
    assert calls == [1]


def test_explicit_physical_dedicated_binding_preserves_default_only_fixture(setup):
    host, ctx = setup
    originals = copy.deepcopy((host.nginx, host.bridge))
    host.nginx["NetworkSettings"]["Networks"][ctx.resource_map()[n.DEDICATED]] = {
        "NetworkID": "5" * 64
    }
    assert (
        n.validate_staged_nginx(
            host,
            ctx.generation_root / "nginx.after",
            *originals,
            dedicated_network_id="5" * 64,
            lab_context=ctx,
        )
        == FIXTURE_ID
    )
    assert set(host.fixture["NetworkSettings"]["Networks"]) == {ctx.resource_map()[n.DEFAULT]}


@pytest.mark.parametrize(
    "folder", ["/tmp/attempt", "/var/lib/codex-security-lab/grok-network-lifecycle", "../attempt"]
)
def test_wrong_generation_refuses_before_filesystem_or_host(setup, monkeypatch, folder):
    host, _ = setup
    host.folder = folder
    touched = []
    monkeypatch.setattr(n, "_lab_filesystem_path", lambda *args: touched.append(args))
    with pytest.raises(n.LabBindingError):
        validate(setup)
    assert touched == [] and host.io == []


def test_duplicate_lab_ledger_fields_are_not_silently_rebased(setup):
    host, _ = setup
    validate(setup)
    path = host.disk / n.LAB_LEDGER
    path.write_text(path.read_text().replace('"schema": 2', '"schema": 1, "schema": 2'))
    host.io.clear()
    with pytest.raises(n.NginxValidationError, match="Invalid private validation ledger"):
        validate(setup)
    assert host.io == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixtures")
@pytest.mark.parametrize("kind", ["source", "ancestor", "opposite"])
def test_symlink_and_broken_opposite_ledger_refusal(setup, kind):
    host, ctx = setup
    if kind == "opposite":
        (host.disk / n.LEDGER).symlink_to(host.disk / "missing")
    elif kind == "source":
        (host.disk / "bad").symlink_to(host.disk / "nginx.conf")
        host.nginx["Mounts"][0]["Source"] = str(ctx.generation_root / "bad")
    else:
        (host.disk / "linked").symlink_to(host.disk / "certs", target_is_directory=True)
        (host.disk / "certs" / "file").write_text("synthetic")
        host.nginx["Mounts"][0]["Source"] = str(ctx.generation_root / "linked/file")
    with pytest.raises(n.LabBindingError):
        validate(setup)
    assert host.io == []


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX path metadata")
@pytest.mark.parametrize(
    "change", [None, "directory-mode", "file-mode", "owner", "source-link", "ancestor-link"]
)
def test_real_path_walk_checks_every_ancestor_and_private_type(tmp_path, monkeypatch, change):
    """Real lstat/inodes, with root ownership/outer tmp ancestry simulated only here."""
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    sub = generation / "config"
    sub.mkdir(mode=0o700)
    source = sub / "nginx.conf"
    source.write_text("synthetic")
    source.chmod(0o600)
    if change == "directory-mode":
        sub.chmod(0o750)
    elif change == "file-mode":
        source.chmod(0o644)
    elif change == "source-link":
        link = sub / "link.conf"
        link.symlink_to(source)
        source = link
    elif change == "ancestor-link":
        link = generation / "linked"
        link.symlink_to(sub, target_is_directory=True)
        source = link / "nginx.conf"
    real_lstat = Path.lstat
    allowed = {*source.parents, source}

    def metadata(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if path not in allowed:
            return info
        inside = generation == path or generation in path.parents
        return SimpleNamespace(
            st_mode=info.st_mode if inside else info.st_mode & ~0o022,
            st_uid=1000 if change == "owner" and path == sub else 0,
            st_gid=0,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
        )

    monkeypatch.setattr(Path, "lstat", metadata)
    if change is None:
        found, fingerprint = n._lab_filesystem_path(
            PurePosixPath(source), PurePosixPath(generation)
        )
        assert found == source and len(fingerprint) == len(source.parents) + 1
    else:
        with pytest.raises(n.LabBindingError):
            n._lab_filesystem_path(PurePosixPath(source), PurePosixPath(generation))
