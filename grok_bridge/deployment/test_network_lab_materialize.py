"""Fixed inputs and isolated Linux FD/flock/mkdir/write/fsync failure tests.

The temporary Linux fixture maps only the fixed logical filesystem tree into a
private temp directory, simulates root/mount/guest observations there, and uses
actual descriptor-relative filesystem operations and locks. It is not a live VM,
admission, mount-classification or power-loss experiment. Windows skips mechanics.
"""

import copy
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import network_lab_materialize as material
import pytest
import test_network_lab_auxiliary_store as store_tests
from test_network_lab_auxiliary_state import context

linux_storage_fixture = store_tests.linux_files


def inputs(ctx=None):
    ctx = ctx or context()
    binding = ctx.journal_binding()
    return {
        **material.fixtures.original_fixture_inputs(ctx, lab_binding=binding),
        **material.auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding),
    }


@pytest.fixture
def no_io(monkeypatch):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Unexpected host entry")

    monkeypatch.setattr(material, "_Installer", forbidden)
    return calls


@pytest.mark.parametrize("binding", [None, {}, False, "lab"])
def test_invalid_binding_precedes_all_host_io(no_io, binding):
    with pytest.raises(material.LabMaterializationError):
        material.materialize(context(), lab_binding=binding)
    assert no_io == []


@pytest.mark.parametrize("changed", ["vm_uuid", "engine_id", "source_commit", "images"])
def test_same_namespace_full_context_drift_refuses_before_io(no_io, changed):
    ctx = context()
    values = {
        "vm_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "engine_id": "different-engine",
        "source_commit": "f" * 40,
        "images": tuple(
            (role, "sha256:" + "f" * 64 if role == "original" else pin) for role, pin in ctx.images
        ),
    }
    other = replace(ctx, **{changed: values[changed]})
    assert other.generation_root == ctx.generation_root
    with pytest.raises(material.LabMaterializationError):
        material.materialize(other, lab_binding=ctx.journal_binding())
    assert no_io == []


@pytest.mark.parametrize("change", ["extra", "missing", "nonbytes", "oversized", "collision"])
def test_generated_contract_is_closed_before_io(no_io, monkeypatch, change):
    original = material.fixtures.original_fixture_inputs

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        key = material.fixtures.COMPOSE
        if change == "extra":
            result["../outside"] = b"no"
        elif change == "missing":
            del result[key]
        elif change == "nonbytes":
            result[key] = bytearray(b"not immutable bytes")
        elif change == "oversized":
            result[key] = b"x" * (material.MAX_FILE_BYTES + 1)
        else:
            result[material.auxiliary.REFERENCE_SEED] = b"collision"
        return result

    monkeypatch.setattr(material.fixtures, "original_fixture_inputs", changed)
    with pytest.raises(material.LabMaterializationError):
        material.materialize(context(), lab_binding=context().journal_binding())
    assert no_io == []


def test_intent_and_reserved_completion_count_against_preflight_bound(no_io, monkeypatch):
    data = inputs()
    size = material.MAX_TOTAL_BYTES - material.MAX_RECORD_BYTES + 1
    for index, key in enumerate(data):
        data[key] = b"x" * (size // len(data) + (index < size % len(data)))
    working = set(
        material.fixtures.original_fixture_inputs(
            context(), lab_binding=context().journal_binding()
        )
    )
    monkeypatch.setattr(
        material.fixtures,
        "original_fixture_inputs",
        lambda *_a, **_kw: {k: v for k, v in data.items() if k in working},
    )
    monkeypatch.setattr(
        material.auxiliary,
        "auxiliary_fixture_inputs",
        lambda *_a, **_kw: {k: v for k, v in data.items() if k not in working},
    )
    with pytest.raises(material.LabMaterializationError, match="reserve"):
        material.materialize(context(), lab_binding=context().journal_binding())
    assert no_io == []


def test_binding_mutation_during_generation_refuses_before_io(no_io, monkeypatch):
    ctx = context()
    binding = ctx.journal_binding()
    original = material.auxiliary.auxiliary_fixture_inputs

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        binding["context"]["source_commit"] = "f" * 40
        return result

    monkeypatch.setattr(material.auxiliary, "auxiliary_fixture_inputs", changed)
    with pytest.raises(material.LabMaterializationError):
        material.materialize(ctx, lab_binding=binding)
    assert no_io == []


def test_no_public_content_path_or_admission_override(no_io):
    with pytest.raises(TypeError):
        material.materialize(
            context(),
            lab_binding=context().journal_binding(),
            path="/other",
            files={},
            execution_admission=True,
        )
    assert no_io == []


def test_pure_intent_record_bound():
    with pytest.raises(material.LabMaterializationError, match="bound"):
        material._record({"value": "x" * material.MAX_RECORD_BYTES})


@pytest.mark.parametrize(
    "platform,uid,machine",
    [("win32", 0, "x86_64"), ("linux", 1000, "x86_64"), ("linux", 0, "aarch64")],
)
def test_unsupported_host_refuses_before_lock_or_filesystem(monkeypatch, platform, uid, machine):
    monkeypatch.setattr(material.session, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        material.session,
        "os",
        SimpleNamespace(geteuid=lambda: uid, uname=lambda: SimpleNamespace(machine=machine)),
    )
    monkeypatch.setattr(material.session, "_OperationLock", lambda: pytest.fail("Lock touched"))
    with pytest.raises(material.LabMaterializationError):
        material.materialize(context(), lab_binding=context().journal_binding())


@pytest.fixture
def guest(linux_storage_fixture, monkeypatch):
    files = linux_storage_fixture
    # The shared fixture supplies actual temp FD/mount observation scaffolding.
    # Move its auxiliary lock outside the generation so this installer sees empty.
    (files.root / material.AUXILIARY_LOCK).rename(files.anchor / "retained-auxiliary-lock")
    run = files.anchor / "run"
    run.mkdir(mode=0o700)
    for path in (material.session.LOCK, material.storage.LOCK):
        target = run / Path(path).name
        target.write_bytes(b"externally provisioned\n")
        target.chmod(0o600)
    files.run = run
    files.guest = {
        "architecture": "x86_64",
        "uuid": {"value": files.ctx.vm_uuid, "file": [1, 2], "parents": []},
        "boot": {"value": "11111111-2222-3333-4444-555555555555", "file": [3, 4], "parents": []},
    }
    original_mounts = material.storage._mounts

    def mounts():
        # Match the real parser's JSON-compatible fields; the reused store
        # fixture uses sets because that component never hashes whole bindings.
        return {
            key: {
                **value,
                "options": sorted(value["options"]),
                "super_options": sorted(value["super_options"]),
            }
            for key, value in original_mounts().items()
        }

    monkeypatch.setattr(material.storage, "_mounts", mounts)
    monkeypatch.setattr(material, "os", files.proxy)
    monkeypatch.setattr(material.session, "os", files.proxy)
    monkeypatch.setattr(material.session, "_guest", lambda: copy.deepcopy(files.guest))

    def forbidden(*_args, **_kwargs):
        pytest.fail("No Docker, collector, observation session or journal operation is permitted")

    monkeypatch.setattr(material.storage, "collect", forbidden)
    monkeypatch.setattr(material.storage, "_engine_get", forbidden)
    monkeypatch.setattr(material.session, "observation_session", forbidden)
    files.outside = files.anchor / "outside-canary"
    files.outside.write_bytes(b"outside unchanged")
    files.outside_before = files.outside.stat()
    return files


def install(files):
    return material.materialize(files.ctx, lab_binding=files.binding)


def physical(files, descriptor):
    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))


def test_linux_exact_materials_private_tree_and_non_authorizing_receipt(guest):
    original_binding = copy.deepcopy(guest.binding)
    result = install(guest)
    expected = inputs(guest.ctx)
    actual_files = {
        p.relative_to(guest.root).as_posix(): p for p in guest.root.rglob("*") if p.is_file()
    }
    actual_dirs = {
        p.relative_to(guest.root).as_posix() for p in guest.root.rglob("*") if p.is_dir()
    }
    assert actual_dirs == set(material.DIRECTORIES)
    assert set(actual_files) == set(expected) | {
        material.INTENT,
        material.COMPLETE,
        material.AUXILIARY_LOCK,
    }
    for path, data in expected.items():
        assert actual_files[path].read_bytes() == data
    assert actual_files[material.AUXILIARY_LOCK].read_bytes() == b""
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in actual_files.values())
    assert all(stat.S_IMODE((guest.root / p).stat().st_mode) == 0o700 for p in actual_dirs)
    assert not list((guest.root / "runtime/nginx-logs").iterdir())
    assert not list((guest.root / "runtime/llm-proxy2-logs").iterdir())
    intent = json.loads(actual_files[material.INTENT].read_bytes())
    completion = json.loads(actual_files[material.COMPLETE].read_bytes())
    assert set(intent["files"]) == set(expected) | {material.AUXILIARY_LOCK}
    assert material.COMPLETE not in completion["observed_tree"]["files"]
    assert "completed_steps" not in completion and "execution_admission" not in completion
    assert completion["kind"] == "completion_marker_only"
    assert result["execution_admission"] is False and result["scope"] == "file_materialization_only"
    assert len(result["observed_tree"]["files"]) == 12
    assert len(result["observed_tree"]["directories"]) == 10  # Includes preexisting generation.
    assert (
        result["completion_sha256"]
        == hashlib.sha256(actual_files[material.COMPLETE].read_bytes()).hexdigest()
    )
    public = json.dumps(result)
    assert guest.ctx.vm_uuid not in public and guest.ctx.engine_id not in public
    assert str(guest.ctx.generation_root) not in public
    assert guest.binding == original_binding
    assert guest.outside.read_bytes() == b"outside unchanged"
    assert guest.outside.stat().st_mtime_ns == guest.outside_before.st_mtime_ns
    result["observed_tree"]["files"].clear()
    assert len(list(guest.root.rglob("*.yml"))) == 3


def test_linux_durable_intent_precedes_every_child_creation(guest, monkeypatch):
    events = []
    real_open, real_mkdir, real_fsync = guest.proxy.open, guest.proxy.mkdir, guest.proxy.fsync

    def opened(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            events.append(("create", path))
        return real_open(path, flags, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        events.append(("mkdir", path))
        return real_mkdir(path, *args, **kwargs)

    def fsync(fd):
        real_fsync(fd)
        events.append(("fsync", physical(guest, fd)))

    monkeypatch.setattr(guest.proxy, "open", opened)
    monkeypatch.setattr(guest.proxy, "mkdir", mkdir)
    monkeypatch.setattr(guest.proxy, "fsync", fsync)
    install(guest)
    intent_created = events.index(("create", material.INTENT))
    intent_synced = events.index(("fsync", guest.root / material.INTENT))
    root_synced = events.index(("fsync", guest.root))
    first_child = events.index(("create", material.AUXILIARY_LOCK))
    assert intent_created < intent_synced < root_synced < first_child
    assert events.index(("create", material.COMPLETE)) > max(
        i for i, event in enumerate(events) if event[0] == "mkdir"
    )


@pytest.mark.parametrize("kind", ["regular", "hidden", "symlink", "broken", "directory", "fifo"])
def test_linux_nonempty_generation_never_adopted_or_cleaned(guest, monkeypatch, kind):
    target = guest.root / (".unknown" if kind == "hidden" else "unknown")
    if kind in {"regular", "hidden"}:
        target.write_bytes(b"retained")
    elif kind in {"symlink", "broken"}:
        target.symlink_to(guest.outside if kind == "symlink" else guest.anchor / "missing")
    elif kind == "directory":
        target.mkdir(mode=0o700)
    else:
        os.mkfifo(target, 0o600)
    before = target.lstat()
    with pytest.raises(material.LabMaterializationError, match="empty"):
        install(guest)
    assert target.lstat().st_ino == before.st_ino
    assert set(p.name for p in guest.root.iterdir()) == {target.name}
    assert guest.outside.read_bytes() == b"outside unchanged"


@pytest.mark.parametrize("kind", ["success", "partial"])
def test_linux_own_prior_output_never_resumed(guest, monkeypatch, kind):
    if kind == "partial":
        monkeypatch.setattr(guest.proxy, "write", lambda *_: 0)
        with pytest.raises(material.LabMaterializationError):
            install(guest)
    else:
        install(guest)
    retained = {p: p.stat().st_ino for p in guest.root.rglob("*")}
    with pytest.raises(material.LabMaterializationError, match="empty"):
        install(guest)
    assert retained == {p: p.stat().st_ino for p in guest.root.rglob("*")}


@pytest.mark.parametrize("kind", ["missing", "symlink", "mode", "overlay", "readonly", "subtree"])
def test_linux_generation_prerequisites_are_not_provisioned_or_repaired(guest, kind):
    if kind in {"missing", "symlink"}:
        original = guest.root.with_name("retained-generation")
        guest.root.rename(original)
        if kind == "symlink":
            guest.root.symlink_to(original, target_is_directory=True)
    elif kind == "mode":
        guest.root.chmod(0o750)
    elif kind == "overlay":
        guest.filesystem = "overlay"
    elif kind == "readonly":
        guest.options = {"ro"}
    else:
        guest.mount_root = "/subtree"
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    if kind == "missing":
        assert not guest.root.exists()
    else:
        assert not (guest.root / material.INTENT).exists()


@pytest.mark.parametrize("role", ["operation", "storage"])
@pytest.mark.parametrize("kind", ["missing", "symlink", "hardlink", "mode"])
def test_linux_host_lock_prerequisites_are_private_and_preexisting(guest, role, kind):
    name = material.session.LOCK if role == "operation" else material.storage.LOCK
    path = guest.run / Path(name).name
    if kind in {"missing", "symlink"}:
        retained = guest.run / (role + "-retained")
        path.rename(retained)
        if kind == "symlink":
            path.symlink_to(retained)
    elif kind == "hardlink":
        os.link(path, guest.run / (role + "-linked"))
    else:
        path.chmod(0o640)
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    assert list(guest.root.iterdir()) == []


def test_linux_storage_lock_is_validated_without_acquiring_it(guest):
    import fcntl

    descriptor = os.open(guest.run / Path(material.storage.LOCK).name, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert install(guest)["execution_admission"] is False
    finally:
        os.close(descriptor)


def test_linux_existing_operation_lock_prevents_nested_staging(guest):
    owner = material.session._OperationLock()
    try:
        with pytest.raises(material.LabMaterializationError):
            install(guest)
        owner.verify()
        assert list(guest.root.iterdir()) == []
    finally:
        owner.close()


def test_linux_different_host_lock_names_cannot_hide_same_observed_inode(guest, monkeypatch):
    original = guest.proxy.stat
    operation_name = Path(material.session.LOCK).name
    storage_name = Path(material.storage.LOCK).name

    def aliased(path, **kwargs):
        # Model a same-inode file bind: separate names are insufficient even
        # when nlink remains one. No actual bind mount or host mutation occurs.
        return original(operation_name if path == storage_name else path, **kwargs)

    monkeypatch.setattr(guest.proxy, "stat", aliased)
    with pytest.raises(material.LabMaterializationError, match="distinct"):
        install(guest)
    assert list(guest.root.iterdir()) == []


def test_linux_wrong_guest_refuses_before_any_generation_write(guest):
    guest.guest["uuid"]["value"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with pytest.raises(material.LabMaterializationError, match="guest"):
        install(guest)
    assert list(guest.root.iterdir()) == []


@pytest.mark.parametrize(
    "change",
    ["uuid", "boot", "binding", "descriptor", "pid", "thread", "operation", "storage", "parent"],
)
def test_linux_mid_install_binding_drift_retains_intent_and_refuses_children(
    guest, monkeypatch, change
):
    real_sync = guest.proxy.fsync
    fired = []

    def sync(fd):
        real_sync(fd)
        if not fired and physical(guest, fd) == guest.root / material.INTENT:
            fired.append(True)
            if change in {"uuid", "boot"}:
                guest.guest[change]["value"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            elif change == "binding":
                guest.binding["context"]["source_commit"] = "f" * 40
            elif change == "descriptor":
                object.__setattr__(guest.ctx, "engine_id", "changed-engine")
            elif change == "pid":
                monkeypatch.setattr(guest.proxy, "getpid", lambda: -1)
            elif change == "thread":
                monkeypatch.setattr(material, "threading", SimpleNamespace(get_ident=lambda: -1))
            elif change == "parent":
                guest.root.parent.chmod(0o750)
            else:
                name = material.session.LOCK if change == "operation" else material.storage.LOCK
                path = guest.run / Path(name).name
                path.rename(guest.run / (change + "-retained"))
                path.write_bytes(b"replacement")
                path.chmod(0o600)

    monkeypatch.setattr(guest.proxy, "fsync", sync)
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    assert fired and set(p.name for p in guest.root.iterdir()) == {material.INTENT}


@pytest.mark.parametrize("change", ["mount", "replacement", "mode", "extra_entry"])
def test_linux_child_identity_or_membership_drift_is_retained(guest, monkeypatch, change):
    original = material._Installer.mkdir
    fired = []

    def mkdir(installer, relative):
        original(installer, relative)
        if relative == "configuration/nginx" and not fired:
            fired.append(True)
            path = guest.root / relative
            if change == "mount":
                monkeypatch.setattr(
                    material.storage,
                    "_fd_mount_id",
                    lambda fd: 99 if physical(guest, fd) == path else guest.mount_id,
                )
            elif change == "replacement":
                path.rename(path.with_name("retained-nginx"))
                path.mkdir(mode=0o700)
            elif change == "mode":
                path.chmod(0o750)
            else:
                (path / "unexpected").write_bytes(b"retained")

    monkeypatch.setattr(material._Installer, "mkdir", mkdir)
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    assert fired and not (guest.root / material.COMPLETE).exists()


@pytest.mark.parametrize(
    "failure",
    [
        "intent_file",
        "intent_directory",
        "auxiliary",
        "generated",
        "material_directory",
        "completion_file",
        "completion_directory",
    ],
)
def test_linux_fsync_boundaries_retain_output_without_success_receipt(guest, monkeypatch, failure):
    real_sync = guest.proxy.fsync
    fired = []

    def sync(fd):
        path = physical(guest, fd)
        complete = (guest.root / material.COMPLETE).exists()
        matched = {
            "intent_file": path == guest.root / material.INTENT,
            "intent_directory": path == guest.root
            and not (guest.root / material.AUXILIARY_LOCK).exists(),
            "auxiliary": path == guest.root / material.AUXILIARY_LOCK,
            "generated": path == guest.root / material.fixtures.COMPOSE,
            "material_directory": path == guest.root / "runtime/nginx-logs" and not complete,
            "completion_file": path == guest.root / material.COMPLETE,
            "completion_directory": path == guest.root and complete,
        }[failure]
        if matched and not fired:
            fired.append(True)
            raise OSError("private filesystem failure canary")
        return real_sync(fd)

    monkeypatch.setattr(guest.proxy, "fsync", sync)
    before = copy.deepcopy(guest.binding)
    error = (
        material.LabMaterializationUncertain
        if failure.startswith("completion")
        else material.LabMaterializationError
    )
    with pytest.raises(error) as caught:
        install(guest)
    assert fired and "canary" not in str(caught.value) and guest.binding == before
    assert (guest.root / material.INTENT).exists()
    if failure.startswith("completion"):
        marker = json.loads((guest.root / material.COMPLETE).read_bytes())
        assert marker["kind"] == "completion_marker_only" and "completed_steps" not in marker
    with pytest.raises(material.LabMaterializationError, match="empty"):
        install(guest)


def test_linux_partial_writes_complete(guest, monkeypatch):
    real_write = guest.proxy.write
    counts = []

    def partial(fd, data):
        count = real_write(fd, data[:113])
        counts.append(count)
        return count

    monkeypatch.setattr(guest.proxy, "write", partial)
    install(guest)
    assert len(counts) > 12
    assert (guest.root / material.fixtures.COMPOSE).read_bytes() == inputs()[
        material.fixtures.COMPOSE
    ]


def test_linux_zero_write_preserves_empty_intent(guest, monkeypatch):
    monkeypatch.setattr(guest.proxy, "write", lambda *_: 0)
    with pytest.raises(material.LabMaterializationError, match="advance"):
        install(guest)
    assert (guest.root / material.INTENT).read_bytes() == b""
    assert set(p.name for p in guest.root.iterdir()) == {material.INTENT}


def test_linux_creation_race_does_not_overwrite_or_adopt_leaf(guest, monkeypatch):
    real_open = guest.proxy.open
    fired = []

    def opened(path, flags, *args, **kwargs):
        if path == material.AUXILIARY_LOCK and flags & os.O_CREAT and not fired:
            fired.append(True)
            (guest.root / material.AUXILIARY_LOCK).write_bytes(b"concurrent creator")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(guest.proxy, "open", opened)
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    assert fired and (guest.root / material.AUXILIARY_LOCK).read_bytes() == b"concurrent creator"
    assert not (guest.root / "configuration").exists()


def test_linux_interrupted_completion_write_is_uncertain_and_retained(guest, monkeypatch):
    original = guest.proxy.write

    def write(fd, data):
        if physical(guest, fd) == guest.root / material.COMPLETE:
            return 0
        return original(fd, data)

    monkeypatch.setattr(guest.proxy, "write", write)
    with pytest.raises(material.LabMaterializationUncertain):
        install(guest)
    assert (guest.root / material.COMPLETE).read_bytes() == b""
    assert (guest.root / material.fixtures.COMPOSE).read_bytes() == inputs()[
        material.fixtures.COMPOSE
    ]


def test_linux_directory_creation_race_refuses_without_adoption(guest, monkeypatch):
    original = guest.proxy.mkdir
    fired = []

    def mkdir(name, *args, **kwargs):
        if name == "configuration" and not fired:
            fired.append(True)
            original(name, *args, **kwargs)
            (guest.root / name / "concurrent-entry").write_bytes(b"retained")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(guest.proxy, "mkdir", mkdir)
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    assert fired and (guest.root / "configuration/concurrent-entry").read_bytes() == b"retained"
    assert not (guest.root / "configuration/nginx").exists()


@pytest.mark.parametrize("kind", ["fifo", "symlink", "hardlink", "edited"])
def test_linux_retained_leaf_drift_refuses_before_read_open(guest, monkeypatch, kind):
    original = material._Installer.readback
    real_open = guest.proxy.open
    reads = []
    fired = []

    def opened(path, flags, *args, **kwargs):
        if path == material.INTENT and not flags & os.O_CREAT:
            reads.append(path)
        return real_open(path, flags, *args, **kwargs)

    def readback(installer, relative):
        if relative == material.INTENT and not fired:
            fired.append(True)
            path = guest.root / material.INTENT
            if kind == "hardlink":
                os.link(path, guest.root / "retained-hardlink")
            elif kind == "edited":
                path.write_bytes(b"changed")
            else:
                path.rename(guest.root / "retained-intent")
                if kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.symlink_to(guest.outside)
        return original(installer, relative)

    monkeypatch.setattr(guest.proxy, "open", opened)
    monkeypatch.setattr(material._Installer, "readback", readback)
    with pytest.raises(material.LabMaterializationError):
        install(guest)
    assert fired and reads == [] and guest.outside.read_bytes() == b"outside unchanged"


def test_linux_replacement_between_lstat_and_open_refuses(guest, monkeypatch):
    real_open = guest.proxy.open
    fired = []

    def opened(path, flags, *args, **kwargs):
        if path == material.INTENT and not flags & os.O_CREAT and not fired:
            fired.append(True)
            target = guest.root / material.INTENT
            data = target.read_bytes()
            target.rename(guest.root / "retained-intent")
            target.write_bytes(data)
            target.chmod(0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(guest.proxy, "open", opened)
    with pytest.raises(material.LabMaterializationError, match="opened leaf"):
        install(guest)
    assert fired and not (guest.root / material.AUXILIARY_LOCK).exists()


def test_linux_read_growth_is_bounded_and_retained(guest, monkeypatch):
    real_read = guest.proxy.read
    fired = []

    def read(fd, size):
        data = real_read(fd, size)
        if not fired and physical(guest, fd) == guest.root / material.INTENT:
            fired.append(True)
            with (guest.root / material.INTENT).open("ab") as stream:
                stream.write(b"extra")
        return data

    monkeypatch.setattr(guest.proxy, "read", read)
    with pytest.raises(material.LabMaterializationError, match="bound|readback"):
        install(guest)
    assert fired and not (guest.root / material.AUXILIARY_LOCK).exists()


def test_linux_final_readback_failure_never_returns_a_receipt(guest, monkeypatch):
    original = material._Installer.readback

    def readback(installer, relative):
        if relative == material.COMPLETE:
            raise OSError("private final-read canary")
        return original(installer, relative)

    monkeypatch.setattr(material._Installer, "readback", readback)
    with pytest.raises(material.LabMaterializationUncertain) as caught:
        install(guest)
    assert "canary" not in str(caught.value) and (guest.root / material.COMPLETE).exists()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_linux_failure_always_releases_operation_lock_without_cleanup(guest, monkeypatch, failure):
    import fcntl

    def write(*_):
        raise failure("private interruption canary")

    monkeypatch.setattr(guest.proxy, "write", write)
    expected = material.LabMaterializationError if failure is RuntimeError else failure
    with pytest.raises(expected):
        install(guest)
    assert (guest.root / material.INTENT).exists()
    fd = os.open(guest.run / Path(material.session.LOCK).name, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_linux_close_failure_still_attempts_all_resources_and_releases_lock(guest, monkeypatch):
    import fcntl

    real_close = guest.proxy.close
    failure = []
    opened = []
    writers = set()
    real_open = guest.proxy.open

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        if args[1] & os.O_CREAT:
            writers.add(fd)
        return fd

    def close(fd):
        target = physical(guest, fd)
        real_close(fd)
        if not failure and fd in writers and target == guest.root / material.COMPLETE:
            # Raise after the actual close, as a close error has uncertain fd state.
            failure.append(True)
            raise OSError("private close canary")

    monkeypatch.setattr(guest.proxy, "open", tracked_open)
    monkeypatch.setattr(guest.proxy, "close", close)
    with pytest.raises(material.LabMaterializationUncertain):
        install(guest)
    assert failure
    for fd in set(opened):
        with pytest.raises(OSError):
            os.fstat(fd)
    fd = os.open(guest.run / Path(material.session.LOCK).name, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert (guest.root / material.COMPLETE).exists()
