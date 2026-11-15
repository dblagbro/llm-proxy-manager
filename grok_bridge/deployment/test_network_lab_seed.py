"""Pure recipes and fixed script tests; no image, Docker or host /data execution.

Linux cases execute only the generated definitions with a narrow / -> private
temporary-root mapping, simulated root ownership, and actual FD/filesystem calls.
The generated entrypoint is never launched. These are not named-volume, mount,
image/interpreter compatibility, admission or power-loss acceptance results.
"""

import ast
import base64
import builtins
import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import network_lab_seed as seed
import pytest
from test_network_lab_auxiliary_state import context


def recipe(role="working", ctx=None):
    ctx = ctx or context()
    return seed.seed_helper_recipe(ctx, lab_binding=ctx.journal_binding(), role=role)


def definitions(value=None):
    value = value or recipe()
    script = value["create_body"]["Entrypoint"][-1]
    namespace = {"__name__": "seed_source_test"}
    # Defines the fixed functions and decodes the constant payload only. The
    # __main__ guard stays false; no raw /data entrypoint is ever executed here.
    exec(compile(script, "<synthetic-seed-definitions>", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("role", ["working", "reference"])
def test_exact_declared_helper_contract_and_synthetic_manifest(role):
    value = recipe(role)
    ctx = context()
    body = value["create_body"]
    host = body["HostConfig"]
    assert body["Image"] == dict(ctx.images)["original"]
    assert body["User"] == "0:0" and body["WorkingDir"] == "/"
    assert body["Entrypoint"][:-1] == ["/usr/bin/python3", "-I", "-B", "-S", "-c"]
    assert body["Cmd"] == [] and body["Healthcheck"] == {"Test": ["NONE"]}
    assert body["Tty"] is False and body["OpenStdin"] is False and body["StdinOnce"] is False
    # These are request declarations only; empty fields do not prove that Docker
    # has removed image-inherited environment or anonymous-volume declarations.
    assert body["Env"] == [] and body["Volumes"] == {}
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and host["CapAdd"] == []
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["NetworkMode"] == "none" and host["IpcMode"] == "private"
    assert host["PidMode"] == "" and host["UTSMode"] == "" and host["UsernsMode"] == ""
    assert host["CgroupnsMode"] == "private"
    assert host["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
    assert all(host[key] is False for key in ("AutoRemove", "Privileged", "PublishAllPorts"))
    assert host["Binds"] == [] and host["PortBindings"] == {}
    assert host["PidsLimit"] == 32 and host["Memory"] == 64 * 1024 * 1024
    assert host["NanoCpus"] == 500_000_000
    assert host["LogConfig"] == {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}}
    suffix = "original-data" if role == "working" else "reference-data"
    assert host["Mounts"] == [
        {
            "Type": "volume",
            "Source": ctx.project + "-" + suffix,
            "Target": "/data",
            "ReadOnly": False,
            "VolumeOptions": {"NoCopy": True},
        }
    ]
    namespace = definitions(value)
    name, data = namespace["payload"]()
    if role == "working":
        expected = seed.fixtures.original_fixture_inputs(ctx, lab_binding=ctx.journal_binding())[
            seed.fixtures.SEED
        ]
    else:
        expected = seed.auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=ctx.journal_binding())[
            seed.auxiliary.REFERENCE_SEED
        ]
    assert data == expected and ctx.digest().encode() in data
    assert value["seed_manifest"] == [
        {
            "path": name,
            "type": "regular",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    ]
    assert value["script_sha256"] == hashlib.sha256(body["Entrypoint"][-1].encode()).hexdigest()
    assert len(body["Entrypoint"][-1].encode()) <= seed.MAX_SCRIPT_BYTES
    assert value["execution_admission"] is False and value["compatibility_verified"] is False
    assert "com.docker.compose.project" not in body["Labels"]


def test_helpers_have_distinct_names_volumes_and_do_not_change_existing_inputs():
    ctx = context()
    binding = ctx.journal_binding()
    before = (
        seed.fixtures.original_fixture_inputs(ctx, lab_binding=binding),
        seed.auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding),
    )
    working, reference = recipe("working"), recipe("reference")
    names = {working["helper_name"], reference["helper_name"]}
    assert len(names) == 2 and not names.intersection(ctx.resource_map().values())
    assert not names.intersection(
        {ctx.project + "-frozen-reference", ctx.project + "-outsider-probe"}
    )
    assert (
        working["create_body"]["HostConfig"]["Mounts"]
        != reference["create_body"]["HostConfig"]["Mounts"]
    )
    assert before == (
        seed.fixtures.original_fixture_inputs(ctx, lab_binding=binding),
        seed.auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding),
    )


def test_recipe_is_deterministic_detached_and_context_attributed():
    ctx = context()
    original = recipe(ctx=ctx)
    assert original == recipe(ctx=ctx)
    changed = replace(ctx, source_commit="f" * 40)
    assert changed.generation_root == ctx.generation_root
    other = recipe(ctx=changed)
    assert other["seed_manifest"] != original["seed_manifest"]
    original["create_body"]["HostConfig"]["Mounts"].clear()
    original["seed_manifest"].clear()
    assert recipe(ctx=ctx)["create_body"]["HostConfig"]["Mounts"]
    assert recipe(ctx=ctx)["seed_manifest"]


@pytest.mark.parametrize("role", [None, False, "original", "../reference", "reference ", 1, []])
def test_invalid_roles_refuse_without_material_lookup(monkeypatch, role):
    monkeypatch.setattr(
        seed.fixtures, "original_fixture_inputs", lambda *_a, **_k: pytest.fail("lookup")
    )
    monkeypatch.setattr(
        seed.auxiliary, "auxiliary_fixture_inputs", lambda *_a, **_k: pytest.fail("lookup")
    )
    with pytest.raises(seed.LabSeedRecipeError):
        seed.seed_helper_recipe(context(), lab_binding=context().journal_binding(), role=role)


@pytest.mark.parametrize("binding", [None, {}, False, "lab"])
def test_missing_or_invalid_binding_refuses(binding):
    with pytest.raises(seed.LabSeedRecipeError):
        seed.seed_helper_recipe(context(), lab_binding=binding, role="working")


@pytest.mark.parametrize(
    "field,value",
    [
        ("vm_uuid", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        ("engine_id", "other-engine"),
        ("source_commit", "f" * 40),
    ],
)
def test_full_context_drift_cannot_reuse_same_namespace_binding(field, value):
    ctx = context()
    other = replace(ctx, **{field: value})
    assert other.generation_root == ctx.generation_root
    with pytest.raises(seed.LabSeedRecipeError):
        seed.seed_helper_recipe(other, lab_binding=ctx.journal_binding(), role="reference")


def test_generation_callback_binding_drift_refuses_and_diagnostics_stay_private(monkeypatch):
    ctx = context()
    binding = ctx.journal_binding()
    original = seed.fixtures.original_fixture_inputs

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        binding["context"]["engine_id"] = "private-engine-canary"
        return result

    monkeypatch.setattr(seed.fixtures, "original_fixture_inputs", changed)
    with pytest.raises(seed.LabSeedRecipeError) as caught:
        seed.seed_helper_recipe(ctx, lab_binding=binding, role="working")
    assert "private-engine-canary" not in str(caught.value)


def test_candidate_image_drift_refuses_even_though_helper_uses_original_pin():
    ctx = context()
    other = replace(
        ctx,
        images=tuple(
            (role, "sha256:" + "f" * 64 if role == "candidate" else pin) for role, pin in ctx.images
        ),
    )
    assert other.generation_root == ctx.generation_root
    assert dict(other.images)["original"] == dict(ctx.images)["original"]
    with pytest.raises(seed.LabSeedRecipeError):
        seed.seed_helper_recipe(other, lab_binding=ctx.journal_binding(), role="working")


@pytest.mark.parametrize("data", [b"", b"x" * (seed.MAX_SEED_BYTES + 1), bytearray(b"x"), "x"])
def test_seed_bytes_are_strictly_bounded(monkeypatch, data):
    monkeypatch.setattr(
        seed.fixtures, "original_fixture_inputs", lambda *_a, **_k: {seed.fixtures.SEED: data}
    )
    with pytest.raises(seed.LabSeedRecipeError):
        recipe()


def test_no_caller_commands_paths_payloads_or_admission_overrides():
    for key, value in (
        ("python", "/other"),
        ("path", "/other"),
        ("payload", b"x"),
        ("execution_admission", True),
    ):
        with pytest.raises(TypeError):
            seed.seed_helper_recipe(
                context(), lab_binding=context().journal_binding(), role="working", **{key: value}
            )


def test_recipe_performs_no_io_process_or_environment_lookup(monkeypatch):
    ctx = context()
    binding = ctx.journal_binding()

    def forbidden(*_a, **_k):
        pytest.fail("Pure recipe crossed an I/O boundary")

    for owner, name in (
        (builtins, "open"),
        (os, "open"),
        (os, "getenv"),
        (socket, "socket"),
        (subprocess, "run"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    assert (
        seed.seed_helper_recipe(ctx, lab_binding=binding, role="working")["execution_admission"]
        is False
    )


def test_script_reports_are_fixed_bounded_and_not_acceptance():
    namespace = definitions()
    for key in ("SUCCESS", "FAILURE"):
        value = namespace[key]
        assert value.endswith("\n") and len(value.encode()) < 256
        parsed = json.loads(value)
        assert parsed["execution_admission"] is False and parsed["compatibility_verified"] is False
        assert context().digest() not in value
    calls = {
        node.func.attr
        for node in ast.walk(ast.parse(recipe()["create_body"]["Entrypoint"][-1]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls.intersection(
        {"chmod", "chown", "unlink", "remove", "rename", "replace", "mkdir", "rmdir"}
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../escape"),
        ("name", "/data/x"),
        ("size", True),
        ("size", 4097),
        ("data", "!invalid!"),
        ("sha256", "0" * 64),
    ],
)
def test_script_payload_refusal_precedes_filesystem_access(field, value):
    namespace = definitions()
    namespace["PAYLOAD"][field] = value
    namespace["os"] = SimpleNamespace(open=lambda *_a, **_k: pytest.fail("unexpected open"))
    with pytest.raises(Exception):
        namespace["seed"]()


@pytest.fixture
def volume(tmp_path, request):
    if not sys.platform.startswith("linux"):
        pytest.skip("Requires actual temporary Linux directory FDs and filesystem calls")
    role = getattr(request, "param", "working")
    value = recipe(role)
    namespace = definitions(value)
    root = tmp_path / "container-root"
    root.mkdir(mode=0o700)
    data = root / "data"
    data.mkdir(mode=0o700)
    outside = tmp_path / "outside-canary"
    outside.write_bytes(b"unchanged")
    state = SimpleNamespace(
        root=root,
        data=data,
        outside=outside,
        namespace=namespace,
        recipe=value,
        opened=[],
        uid=0,
        gid=0,
        uid_paths={},
        gid_paths={},
    )
    proxy = SimpleNamespace(**vars(os))

    def fd_path(fd):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))

    def mapped(info, path):
        path = Path(path)
        if path != root and root not in path.parents:
            return info
        fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
        fields["st_uid"] = state.uid_paths.get(str(path), state.uid)
        fields["st_gid"] = state.gid_paths.get(str(path), state.gid)
        return SimpleNamespace(**fields)

    def opened(path, flags, mode=0o777, *, dir_fd=None):
        assert path in {"/", "data", namespace["PAYLOAD"]["name"]}
        assert (path == "/") is (dir_fd is None)
        fd = os.open(root if path == "/" else path, flags, mode, dir_fd=dir_fd)
        state.opened.append(fd)
        return fd

    def inspected(path, *, dir_fd=None, follow_symlinks=True):
        assert path in {"/", "data", namespace["PAYLOAD"]["name"]}
        target = root if path == "/" else fd_path(dir_fd) / path
        return mapped(
            os.stat(root if path == "/" else path, dir_fd=dir_fd, follow_symlinks=follow_symlinks),
            target,
        )

    proxy.open = opened
    proxy.stat = inspected
    proxy.fstat = lambda fd: mapped(os.fstat(fd), fd_path(fd))
    proxy.geteuid = proxy.getegid = lambda: 0
    namespace["os"] = proxy
    namespace["sys"] = SimpleNamespace(
        platform="linux", argv=["-c"], stdout=io.StringIO(), stderr=io.StringIO()
    )
    state.proxy = proxy
    state.fd_path = fd_path
    state.name = namespace["PAYLOAD"]["name"]
    state.expected = base64.b64decode(namespace["PAYLOAD"]["data"])
    return state


def run(value):
    return value.namespace["main"]()


def refused(value):
    assert run(value) == 78
    assert value.namespace["sys"].stdout.getvalue() == ""
    assert value.namespace["sys"].stderr.getvalue() == value.namespace["FAILURE"]
    assert value.outside.read_bytes() == b"unchanged"


@pytest.mark.parametrize("volume", ["working", "reference"], indirect=True)
def test_linux_exact_seed_with_private_leaf_and_no_permission_repair(volume):
    before = volume.data.stat()
    assert run(volume) == 0
    target = volume.data / volume.name
    assert list(volume.data.iterdir()) == [target]
    assert target.read_bytes() == volume.expected
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and target.stat().st_nlink == 1
    after = volume.data.stat()
    assert (before.st_uid, before.st_gid, before.st_mode) == (
        after.st_uid,
        after.st_gid,
        after.st_mode,
    )
    assert volume.namespace["sys"].stdout.getvalue() == volume.namespace["SUCCESS"]
    assert volume.namespace["sys"].stderr.getvalue() == ""
    for fd in set(volume.opened):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "kind", ["regular", "hidden", "symlink", "broken", "directory", "fifo", "matching"]
)
def test_linux_nonempty_input_is_never_read_or_adopted(volume, kind):
    name = volume.name if kind == "matching" else ("ordinary" if kind == "regular" else ".other")
    target = volume.data / name
    if kind in {"regular", "hidden", "matching"}:
        target.write_bytes(volume.expected if kind == "matching" else b"private data canary")
    elif kind in {"symlink", "broken"}:
        target.symlink_to(volume.outside if kind == "symlink" else volume.root / "missing")
    elif kind == "directory":
        target.mkdir(mode=0o700)
    else:
        os.mkfifo(target, 0o600)
    previous = target.lstat()
    refused(volume)
    assert target.lstat().st_ino == previous.st_ino
    assert list(volume.data.iterdir()) == [target]


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "owner", "writable", "readonly"])
def test_linux_volume_prerequisites_refuse_without_repairs(volume, kind):
    if kind in {"missing", "symlink", "fifo"}:
        volume.data.rename(volume.root / "retained-data")
        if kind == "symlink":
            volume.data.symlink_to(volume.root / "retained-data", target_is_directory=True)
        elif kind == "fifo":
            os.mkfifo(volume.data, 0o600)
    elif kind == "owner":
        volume.uid_paths[str(volume.data)] = 1000
    elif kind == "writable":
        volume.data.chmod(0o770)
    else:
        volume.proxy.fstatvfs = lambda _fd: SimpleNamespace(f_flag=os.ST_RDONLY)
    refused(volume)
    if kind == "missing":
        assert not volume.data.exists()
    elif kind not in {"fifo", "symlink"}:
        assert not (volume.data / volume.name).exists()


@pytest.mark.parametrize("phase", ["file", "directory"])
def test_linux_fsync_failure_retains_bytes_and_never_reports_success(volume, phase):
    original = volume.proxy.fsync

    def failing(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) is (phase == "file"):
            raise OSError("private sync canary")
        return original(fd)

    volume.proxy.fsync = failing
    refused(volume)
    assert (volume.data / volume.name).read_bytes() == volume.expected


def test_linux_partial_writes_are_completed(volume):
    original = volume.proxy.write
    calls = []

    def partial(fd, data):
        calls.append(True)
        return original(fd, data[:7])

    volume.proxy.write = partial
    assert run(volume) == 0 and len(calls) > 1
    assert (volume.data / volume.name).read_bytes() == volume.expected


@pytest.mark.parametrize("failure", ["zero", "exception", "interrupt"])
def test_linux_interrupted_writes_leave_evidence_and_no_automatic_retry(volume, failure):
    original = volume.proxy.write
    calls = []

    def partial(fd, data):
        if not calls:
            calls.append(True)
            return original(fd, data[:7])
        if failure == "zero":
            return 0
        if failure == "interrupt":
            raise KeyboardInterrupt
        raise OSError("private write canary")

    volume.proxy.write = partial
    refused(volume)
    target = volume.data / volume.name
    identity = target.stat().st_ino
    assert target.read_bytes() == volume.expected[:7]
    volume.namespace["sys"].stderr = io.StringIO()
    refused(volume)
    assert target.stat().st_ino == identity and target.read_bytes() == volume.expected[:7]


@pytest.mark.parametrize("kind", ["regular", "symlink", "fifo"])
def test_linux_creation_race_is_exclusive_and_no_follow(volume, kind):
    original = volume.proxy.open
    fired = []

    def opened(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and not fired:
            fired.append(True)
            target = volume.data / volume.name
            if kind == "regular":
                target.write_bytes(b"concurrent bytes")
            elif kind == "symlink":
                target.symlink_to(volume.outside)
            else:
                os.mkfifo(target, 0o600)
        return original(path, flags, *args, **kwargs)

    volume.proxy.open = opened
    refused(volume)
    assert fired
    if kind == "regular":
        assert (volume.data / volume.name).read_bytes() == b"concurrent bytes"


@pytest.mark.parametrize("change", ["volume", "root_mode", "file_owner", "hardlink", "extra"])
def test_linux_identity_and_membership_drift_after_write_refuses(volume, change):
    original = volume.proxy.write
    fired = []

    def write(fd, data):
        result = original(fd, data)
        if not fired:
            fired.append(True)
            if change == "volume":
                volume.data.rename(volume.root / "retained-volume")
                volume.data.mkdir(mode=0o700)
            elif change == "root_mode":
                volume.root.chmod(0o750)
            elif change == "file_owner":
                volume.uid_paths[str(volume.data / volume.name)] = 1000
            elif change == "hardlink":
                os.link(volume.data / volume.name, volume.root / "retained-link")
            else:
                (volume.data / "unexpected").write_bytes(b"retained")
        return result

    volume.proxy.write = write
    refused(volume)
    assert fired


@pytest.mark.parametrize("kind", ["replacement", "fifo", "symlink", "edited"])
def test_linux_readback_leaf_change_never_reads_untrusted_content(volume, kind):
    original = volume.proxy.fsync
    original_open = volume.proxy.open
    reads = []
    fired = []

    def synced(fd):
        original(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not fired:
            fired.append(True)
            target = volume.data / volume.name
            if kind == "edited":
                target.write_bytes(b"different bytes")
            else:
                data = target.read_bytes()
                target.rename(volume.root / "retained-file")
                if kind == "replacement":
                    target.write_bytes(data)
                    target.chmod(0o600)
                elif kind == "fifo":
                    os.mkfifo(target, 0o600)
                else:
                    target.symlink_to(volume.outside)

    def opened(path, flags, *args, **kwargs):
        if path == volume.name and not flags & os.O_CREAT:
            reads.append(True)
        return original_open(path, flags, *args, **kwargs)

    volume.proxy.fsync = synced
    volume.proxy.open = opened
    refused(volume)
    assert fired and reads == []


def test_linux_replacement_between_read_stat_and_open_refuses(volume):
    original = volume.proxy.open
    fired = []

    def opened(path, flags, *args, **kwargs):
        if path == volume.name and not flags & os.O_CREAT and not fired:
            fired.append(True)
            target = volume.data / volume.name
            data = target.read_bytes()
            target.rename(volume.root / "retained-file")
            target.write_bytes(data)
            target.chmod(0o600)
        return original(path, flags, *args, **kwargs)

    volume.proxy.open = opened
    refused(volume)
    assert fired


@pytest.mark.parametrize("change", ["growth", "same_size_edit"])
def test_linux_read_race_is_bounded_and_exact(volume, change):
    original = volume.proxy.read
    fired = []

    def read(fd, bound):
        data = original(fd, bound)
        if not fired:
            fired.append(True)
            path = volume.data / volume.name
            if change == "growth":
                with path.open("ab") as stream:
                    stream.write(b"extra")
            else:
                path.write_bytes(b"x" * len(volume.expected))
        return data

    volume.proxy.read = read
    refused(volume)
    assert fired


def test_linux_close_failure_attempts_all_fds_before_any_success_report(volume):
    original = volume.proxy.close
    closed, failed = [], []

    def close(fd):
        original(fd)
        closed.append(fd)
        if not failed:
            failed.append(True)
            raise OSError("private close canary")

    volume.proxy.close = close
    refused(volume)
    assert failed and set(closed) == set(volume.opened)
    for fd in set(volume.opened):
        with pytest.raises(OSError):
            os.fstat(fd)
    assert (volume.data / volume.name).read_bytes() == volume.expected


@pytest.mark.parametrize("phase", ["write", "short_write", "flush"])
def test_linux_report_failure_is_not_success_even_if_bytes_were_printed(volume, phase):
    class BrokenOutput(io.StringIO):
        def write(self, value):
            if phase == "write":
                raise OSError("private output canary")
            count = super().write(value)
            return count - 1 if phase == "short_write" else count

        def flush(self):
            if phase == "flush":
                raise OSError("private flush canary")
            return super().flush()

    volume.namespace["sys"].stdout = BrokenOutput()
    assert run(volume) == 78
    assert volume.namespace["sys"].stderr.getvalue() == volume.namespace["FAILURE"]
    assert (volume.data / volume.name).read_bytes() == volume.expected


def test_linux_unexpected_argv_is_not_a_payload_or_path_override(volume):
    volume.namespace["sys"].argv.append("/outside")
    refused(volume)
    assert list(volume.data.iterdir()) == [] and volume.opened == []
