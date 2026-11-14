"""Synthetic guest/collector tests and real Linux temporary FD/flock tests.

No live kernel guest-identity or Docker/storage collection is invoked. Linux
mechanics use private temporary files with explicitly simulated root ownership,
kernel mount observations and an anchored parent chain. Windows runs pure/fake
session tests only; it provides no flock or no-follow filesystem evidence.
"""

import copy
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import network_lab_capacity as capacity
import network_lab_context as lab
import network_lab_session as session
import pytest
from test_network_lab_context import record

GIB = 1024**3
LINUX = sys.platform.startswith("linux")
_REAL_LOCK = session._OperationLock
_REAL_PLATFORM = session._platform


def context():
    return lab.from_record(record())


def peaks():
    return {"evidence": GIB, "docker": 2 * GIB}


@pytest.fixture
def simulated(monkeypatch):
    ctx = context()
    state = SimpleNamespace(
        context=ctx,
        binding=ctx.journal_binding(),
        calls=[],
        locks=[],
        collected=[],
        guest={
            "architecture": "x86_64",
            "uuid": {"value": ctx.vm_uuid, "file": [1, 2], "parents": []},
            "boot": {
                "value": "11111111-2222-3333-4444-555555555555",
                "file": [1, 3],
                "parents": [],
            },
        },
        storage_binding="a" * 64,
        available=30 * GIB,
        collector_hook=lambda *_: None,
        guest_hook=lambda: None,
        lock_hook=lambda: None,
    )

    class Lock:
        def __init__(self):
            state.calls.append("lock-acquire")
            self.held = True
            state.locks.append(self)

        def verify(self):
            assert self.held
            state.calls.append("lock-verify")
            state.lock_hook()

        def close(self):
            state.calls.append("lock-close")
            self.held = False

        def binding_sha256(self):
            assert self.held
            return "d" * 64

    def guest():
        state.calls.append("guest")
        state.guest_hook()
        return copy.deepcopy(state.guest)

    def collect(ctx, *, peaks):
        assert state.locks[-1].held
        state.calls.append("collect")
        state.collected.append(copy.deepcopy(peaks))
        state.collector_hook(ctx, peaks)
        observations = {
            role: {
                "filesystem_id": "dev:8:1:fsid:a",
                "total_bytes": 60 * GIB,
                "available_bytes": state.available,
            }
            for role in capacity.ROLES
        }
        return {
            "schema": session.storage.SCHEMA,
            "scope": "read_only_storage_observation_and_capacity_arithmetic",
            "context_sha256": ctx.digest(),
            "bindings_sha256": state.storage_binding,
            "capacity_assessment": capacity.assess(ctx, observations, peaks=peaks),
            "snapshot_lock_released_on_return": True,
            "execution_admission": False,
        }

    monkeypatch.setattr(session, "_platform", lambda: state.calls.append("platform"))
    monkeypatch.setattr(session, "_OperationLock", Lock)
    monkeypatch.setattr(session, "_guest", guest)
    monkeypatch.setattr(session.storage, "collect", collect)
    return state


def manager(state, **overrides):
    values = {"lab_binding": state.binding, "peaks": peaks(), **overrides}
    return session.observation_session(state.context, **values)


def test_session_holds_lock_across_fresh_observations_and_releases_after_body(simulated):
    with manager(simulated) as current:
        assert simulated.locks[-1].held and len(simulated.collected) == 1
        first = current.refresh(peaks=peaks())
        simulated.available -= GIB
        second = current.refresh(peaks={"evidence": 2 * GIB, "docker": 3 * GIB})
        assert first["execution_admission"] is False
        assert first["operation_lock_held_at_observation"] is True
        assert first["operation_lock_binding_sha256"] == "d" * 64
        assert first["guest_binding_sha256"] == second["guest_binding_sha256"]
        assert (
            first["storage_observation"]["bindings_sha256"]
            == second["storage_observation"]["bindings_sha256"]
        )
        assert (
            first["storage_observation"]["capacity_assessment"]
            != second["storage_observation"]["capacity_assessment"]
        )
        first["storage_observation"]["bindings_sha256"] = "changed-output"
        assert current.refresh(peaks=peaks())["storage_observation"]["bindings_sha256"] == "a" * 64
    assert not simulated.locks[-1].held
    public = json.dumps(second)
    for secret in (
        simulated.context.vm_uuid,
        simulated.context.vm_name,
        simulated.context.engine_id,
        str(simulated.context.generation_root),
        simulated.guest["boot"]["value"],
    ):
        assert secret not in public


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        True,
        {"evidence": 0, "docker": 0},
        {"evidence": True, "docker": 1},
        {"evidence": 1.0, "docker": 1},
        {"evidence": -1, "docker": 1},
        {"evidence": 1, "docker": 2**64},
    ],
)
def test_invalid_initial_peaks_have_zero_observation_io(simulated, value):
    with pytest.raises(session.LabSessionError):
        manager(simulated, peaks=value)
    assert simulated.calls == []


@pytest.mark.parametrize("binding", [None, {}, False, "not-a-binding"])
def test_missing_or_malformed_binding_has_zero_observation_io(simulated, binding):
    with pytest.raises(session.LabSessionError):
        manager(simulated, lab_binding=binding)
    assert simulated.calls == []


@pytest.mark.parametrize("field", ["vm_uuid", "vm_name", "engine_id", "source_commit", "images"])
def test_full_context_change_in_same_namespace_refuses_before_io(simulated, field):
    values = {
        "vm_uuid": "12345678-1234-1234-1234-123456789abd",
        "vm_name": "codex-grok-lab-other",
        "engine_id": "other-engine",
        "source_commit": "f" * 40,
        "images": tuple(
            (role, "sha256:" + "7" * 64 if role == "nginx" else image)
            for role, image in simulated.context.images
        ),
    }
    altered = replace(simulated.context, **{field: values[field]})
    with pytest.raises(session.LabSessionError):
        session.observation_session(altered, lab_binding=simulated.binding, peaks=peaks())
    assert simulated.calls == []


def test_initial_and_refresh_peaks_are_detached_from_caller_mutation(simulated):
    initial = peaks()
    scope = manager(simulated, peaks=initial)
    initial["docker"] = 999 * GIB
    with scope as current:
        upcoming = peaks()
        simulated.collector_hook = lambda *_: upcoming.update(docker=999 * GIB)
        current.refresh(peaks=upcoming)
    assert simulated.collected == [peaks(), peaks()]


@pytest.mark.parametrize(
    "change", ["source-context", "source-binding", "internal-context", "internal-binding"]
)
def test_context_or_binding_drift_before_entry_has_zero_observation_io(simulated, change):
    scope = manager(simulated)
    if change == "source-context":
        object.__setattr__(simulated.context, "engine_id", "changed-engine")
    elif change == "source-binding":
        simulated.binding["context"]["engine_id"] = "changed-engine"
    elif change == "internal-context":
        object.__setattr__(scope._context, "engine_id", "changed-engine")
    else:
        scope._binding["context"]["engine_id"] = "changed-engine"
    with pytest.raises(session.LabSessionError):
        scope.__enter__()
    assert simulated.calls == []


@pytest.mark.parametrize(
    "change", ["context", "binding", "guest-uuid", "guest-boot", "guest-path", "lock"]
)
def test_drift_before_refresh_refuses_before_collector(simulated, change):
    with manager(simulated) as current:
        before = len(simulated.collected)
        if change == "context":
            object.__setattr__(simulated.context, "engine_id", "changed-engine")
        elif change == "binding":
            simulated.binding["context_sha256"] = "f" * 64
        elif change == "guest-uuid":
            simulated.guest["uuid"]["value"] = "different"
        elif change == "guest-boot":
            simulated.guest["boot"]["value"] = "different"
        elif change == "guest-path":
            simulated.guest["uuid"]["file"] = [1, 999]
        else:
            simulated.lock_hook = lambda: (_ for _ in ()).throw(OSError("private-canary"))
        with pytest.raises(session.LabSessionError) as caught:
            current.refresh(peaks=peaks())
        assert len(simulated.collected) == before and "private-canary" not in str(caught.value)
    assert not simulated.locks[-1].held


@pytest.mark.parametrize("change", ["context", "binding", "guest", "lock", "storage-binding"])
def test_drift_inside_collector_never_returns_a_fresh_record(simulated, change):
    with manager(simulated) as current:

        def change_during(*_):
            if change == "context":
                object.__setattr__(simulated.context, "engine_id", "changed-engine")
            elif change == "binding":
                simulated.binding["context_sha256"] = "f" * 64
            elif change == "guest":
                simulated.guest["boot"]["value"] = "different"
            elif change == "lock":
                simulated.lock_hook = lambda: (_ for _ in ()).throw(OSError("private-canary"))
            else:
                simulated.storage_binding = "b" * 64

        simulated.collector_hook = change_during
        with pytest.raises(session.LabSessionError):
            current.refresh(peaks=peaks())
        count = len(simulated.collected)
        with pytest.raises(session.LabSessionError):
            current.refresh(peaks=peaks())
        assert len(simulated.collected) == count
    assert not simulated.locks[-1].held


def test_invalid_refresh_peaks_do_not_observe_guest_lock_or_storage(simulated):
    with manager(simulated) as current:
        simulated.calls.clear()
        with pytest.raises(session.LabSessionError):
            current.refresh(peaks={"evidence": 1})
        assert simulated.calls == []


@pytest.mark.parametrize("fault", [KeyboardInterrupt, SystemExit, RuntimeError])
@pytest.mark.parametrize("where", ["body", "collector"])
def test_baseexception_always_releases_lock(simulated, fault, where):
    def fail(*_):
        raise fault("private-canary")

    if where == "collector":
        simulated.collector_hook = fail
    expected = session.LabSessionError if where == "collector" and fault is RuntimeError else fault
    with pytest.raises(expected):
        with manager(simulated):
            if where == "body":
                fail()
    assert simulated.locks and not simulated.locks[-1].held


def test_wrong_observed_uuid_refuses_without_collector_and_releases(simulated):
    simulated.guest["uuid"]["value"] = "12345678-1234-1234-1234-123456789abd"
    with pytest.raises(session.LabSessionError, match="VM identity"):
        with manager(simulated):
            pytest.fail("Wrong VM yielded")
    assert not simulated.collected and not simulated.locks[-1].held


def test_reentry_reuse_and_use_after_exit_are_refused_without_more_collection(simulated):
    scope = manager(simulated)
    with scope as current:
        with pytest.raises(session.LabSessionError, match="reused"):
            scope.__enter__()
    count = len(simulated.collected)
    for call in (lambda: current.refresh(peaks=peaks()), scope.__enter__):
        with pytest.raises(session.LabSessionError):
            call()
    assert len(simulated.collected) == count


def test_concurrent_entry_cannot_close_first_controllers_lock(simulated):
    scope = manager(simulated)
    entered, finish = threading.Event(), threading.Event()
    errors = []

    def owner():
        try:
            with scope:
                entered.set()
                assert finish.wait(timeout=2)
        except BaseException as error:
            errors.append(type(error).__name__)

    worker = threading.Thread(target=owner)
    worker.start()
    try:
        assert entered.wait(timeout=2)
        count = len(simulated.collected)
        with pytest.raises(session.LabSessionError, match="reused"):
            scope.__enter__()
        assert simulated.locks[-1].held and len(simulated.collected) == count
    finally:
        finish.set()
        worker.join(timeout=2)
    assert not worker.is_alive() and errors == [] and not simulated.locks[-1].held


def test_cross_thread_refresh_refuses_without_observations(simulated):
    errors = []
    with manager(simulated) as current:
        count = len(simulated.collected)

        def foreign():
            try:
                current.refresh(peaks=peaks())
            except session.LabSessionError:
                errors.append("refused")

        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive() and errors == ["refused"]
        assert len(simulated.collected) == count


@pytest.mark.parametrize("exception", [None, KeyboardInterrupt])
def test_foreign_thread_exit_cannot_release_owners_operation_lock(simulated, exception):
    errors = []
    with manager(simulated) as current:
        before = list(simulated.calls)

        def foreign():
            try:
                current.__exit__(exception, None, None)
            except session.LabSessionError:
                errors.append("refused")

        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive() and errors == ["refused"]
        assert simulated.calls == before and simulated.locks[-1].held
        current.refresh(peaks=peaks())
    assert not simulated.locks[-1].held


def test_exit_detects_guest_change_and_releases_lock_without_collection(simulated):
    with pytest.raises(session.LabSessionError, match="guest identity"):
        with manager(simulated):
            count = len(simulated.collected)
            simulated.guest["boot"]["value"] = "changed"
    assert not simulated.locks[-1].held and len(simulated.collected) == count


@pytest.mark.parametrize(
    "field", ["extra", "schema", "context", "scope", "authority", "nested-context", "nested-scope"]
)
def test_unexpected_collector_receipt_refuses_and_poisoned_session_cannot_rebase(
    simulated, monkeypatch, field
):
    actual = session.storage.collect
    with manager(simulated) as current:

        def changed(*args, **kwargs):
            result = actual(*args, **kwargs)
            if field == "extra":
                result["private-path"] = "private-canary"
            elif field == "schema":
                result["schema"] = "unknown"
            elif field == "context":
                result["context_sha256"] = "f" * 64
            elif field == "scope":
                result["scope"] = "execution_admission"
            elif field == "authority":
                result["execution_admission"] = True
            elif field == "nested-context":
                result["capacity_assessment"]["context_sha256"] = "f" * 64
            else:
                result["capacity_assessment"]["scope"] = "admitted"
            return result

        monkeypatch.setattr(session.storage, "collect", changed)
        with pytest.raises(session.LabSessionError) as caught:
            current.refresh(peaks=peaks())
        assert "private-canary" not in str(caught.value)
        count = len(simulated.collected)
        monkeypatch.setattr(session.storage, "collect", actual)
        with pytest.raises(session.LabSessionError):
            current.refresh(peaks=peaks())
        assert len(simulated.collected) == count


@pytest.mark.parametrize("stage", ["before-entry", "refresh", "inside-collector"])
def test_process_change_refuses_before_further_collection(simulated, monkeypatch, stage):
    scope = manager(simulated)
    actual_pid = os.getpid()
    changed = SimpleNamespace(**vars(os))
    changed.getpid = lambda: actual_pid + 1
    if stage == "before-entry":
        monkeypatch.setattr(session, "os", changed)
        with pytest.raises(session.LabSessionError):
            scope.__enter__()
        assert simulated.calls == []
    else:
        with scope as current:
            count = len(simulated.collected)
            if stage == "refresh":
                monkeypatch.setattr(session, "os", changed)
            else:
                simulated.collector_hook = lambda *_: monkeypatch.setattr(session, "os", changed)
            with pytest.raises(session.LabSessionError):
                current.refresh(peaks=peaks())
            assert len(simulated.collected) == count + (stage == "inside-collector")
            # Return to the real owning process before normal context-manager exit.
            monkeypatch.setattr(session, "os", os)
        assert not simulated.locks[-1].held


def test_operation_lock_must_differ_from_snapshot_lock(simulated, monkeypatch):
    monkeypatch.setattr(session, "LOCK", session.storage.LOCK)
    with pytest.raises(session.LabSessionError):
        with manager(simulated):
            pytest.fail("Shared lock yielded")
    assert simulated.calls == []


@pytest.mark.parametrize(
    "platform,uid,machine",
    [("win32", 0, "x86_64"), ("linux", 1000, "x86_64"), ("linux", 0, "aarch64")],
)
def test_real_platform_guard_refuses_unsupported_observation_host(
    monkeypatch, platform, uid, machine
):
    monkeypatch.setattr(session, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        session,
        "os",
        SimpleNamespace(geteuid=lambda: uid, uname=lambda: SimpleNamespace(machine=machine)),
    )
    with pytest.raises(session.LabSessionError):
        _REAL_PLATFORM()


@pytest.fixture
def linux_files(tmp_path, monkeypatch):
    if not LINUX:
        pytest.skip("Linux-only real directory descriptors, no-follow and flock")
    root = tmp_path / "observed"
    root.mkdir(mode=0o700)
    state = SimpleNamespace(
        root=root, uid=0, mount_id=17, filesystem="sysfs", mount_root="/", read_hook=None
    )
    lock = root / Path(session.LOCK).name
    lock.write_bytes(b"retained-lock")
    lock.chmod(0o600)

    def mapped_info(info):
        inodes = {(p.lstat().st_dev, p.lstat().st_ino) for p in root.iterdir()}
        if (info.st_dev, info.st_ino) not in inodes:
            return info
        values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
        values["st_uid"] = state.uid
        return SimpleNamespace(**values)

    proxy = SimpleNamespace(**vars(os))
    proxy.stat = lambda *a, **k: mapped_info(os.stat(*a, **k))
    proxy.fstat = lambda descriptor: mapped_info(os.fstat(descriptor))

    class Chain:
        def __init__(self, path, **_):
            assert path in {
                "/run",
                str(Path(session.DMI_UUID).parent),
                str(Path(session.BOOT_ID).parent),
            }
            self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.expected = session.storage._metadata(os.fstat(self.fd))
            self.bindings = [
                {
                    "metadata": self.expected,
                    "mount": {
                        "id": state.mount_id,
                        "fstype": state.filesystem,
                        "root": state.mount_root,
                    },
                }
            ]

        def verify(self):
            if (
                session.storage._metadata(os.fstat(self.fd)) != self.expected
                or session.storage._metadata(root.stat()) != self.expected
            ):
                raise session.LabSessionError("Simulated parent changed")

        def close(self):
            os.close(self.fd)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(session, "os", proxy)
    monkeypatch.setattr(session.storage, "os", proxy)
    monkeypatch.setattr(session.storage, "_DirectoryChain", Chain)
    monkeypatch.setattr(session.storage, "_fd_mount_id", lambda _: state.mount_id)
    return state


def test_linux_real_flock_excludes_independent_open_until_close(linux_files):
    import fcntl

    lock_path = linux_files.root / Path(session.LOCK).name
    before = lock_path.read_bytes(), lock_path.stat().st_mtime_ns
    held = _REAL_LOCK()
    other = os.open(lock_path, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(session.LabSessionError, match="busy"):
            _REAL_LOCK()
        held.close()
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (lock_path.read_bytes(), lock_path.stat().st_mtime_ns) == before
    finally:
        held.close()
        os.close(other)


@pytest.mark.parametrize("change", ["replace", "hardlink", "mode", "owner", "parent-mode"])
def test_linux_held_lock_drift_refuses(linux_files, change):
    path = linux_files.root / Path(session.LOCK).name
    held = _REAL_LOCK()
    try:
        if change == "replace":
            path.rename(linux_files.root / "retained-old-lock")
            path.write_bytes(b"replacement")
            path.chmod(0o600)
        elif change == "hardlink":
            os.link(path, linux_files.root / "another-link")
        elif change == "mode":
            path.chmod(0o644)
        elif change == "owner":
            linux_files.uid = 1000
        else:
            linux_files.root.chmod(0o750)
        with pytest.raises((session.LabSessionError, session.storage.LabStorageError)):
            held.verify()
    finally:
        held.close()


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "directory"])
def test_linux_lock_admission_refuses_nonregular_or_absent_without_creation(linux_files, kind):
    path = linux_files.root / Path(session.LOCK).name
    path.rename(linux_files.root / "retained-original")
    if kind == "symlink":
        path.symlink_to(linux_files.root / "retained-original")
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    before = set(linux_files.root.iterdir())
    with pytest.raises((session.LabSessionError, session.storage.LabStorageError, OSError)):
        _REAL_LOCK()
    assert set(linux_files.root.iterdir()) == before


@pytest.mark.parametrize(
    "which,filesystem", [(session.DMI_UUID, "sysfs"), (session.BOOT_ID, "proc")]
)
def test_linux_kernel_uuid_reads_are_bounded_and_normalized(linux_files, which, filesystem):
    linux_files.filesystem = filesystem
    path = linux_files.root / Path(which).name
    path.write_bytes(context().vm_uuid.upper().encode() + b"\n")
    path.chmod(0o400)
    value = session._kernel_uuid(which, filesystem)
    assert value["value"] == context().vm_uuid
    assert path.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * 65,
        b"invalid-private-canary",
        b"00000000-0000-0000-0000-000000000000\n",
        b"12345678-1234-1234-1234-123456789abc\n\n",
        b"\xff",
    ],
)
def test_linux_kernel_uuid_refuses_invalid_or_oversized_input(linux_files, payload):
    path = linux_files.root / Path(session.DMI_UUID).name
    path.write_bytes(payload)
    path.chmod(0o400)
    with pytest.raises((session.LabSessionError, UnicodeError)):
        session._kernel_uuid(session.DMI_UUID, "sysfs")


@pytest.mark.parametrize("change", ["symlink", "writable", "owner", "mount-type", "mount-root"])
def test_linux_kernel_file_and_mount_refusals(linux_files, change):
    path = linux_files.root / Path(session.DMI_UUID).name
    path.write_bytes(context().vm_uuid.encode())
    path.chmod(0o400)
    if change == "symlink":
        path.rename(linux_files.root / "retained-uuid")
        path.symlink_to(linux_files.root / "retained-uuid")
    elif change == "writable":
        path.chmod(0o666)
    elif change == "owner":
        linux_files.uid = 1000
    elif change == "mount-type":
        linux_files.filesystem = "ext4"
    else:
        linux_files.mount_root = "/subtree"
    with pytest.raises(session.LabSessionError):
        session._kernel_uuid(session.DMI_UUID, "sysfs")


def test_linux_after_fork_refusal_closes_child_fds_without_unlocking_parent(
    simulated, linux_files, monkeypatch
):
    import fcntl

    monkeypatch.setattr(session, "_OperationLock", _REAL_LOCK)
    # Storage measurements remain synthetic; the contender below checks real flock.
    monkeypatch.setattr(
        session.storage,
        "collect",
        lambda ctx, *, peaks: {
            "schema": session.storage.SCHEMA,
            "scope": "read_only_storage_observation_and_capacity_arithmetic",
            "context_sha256": ctx.digest(),
            "bindings_sha256": "a" * 64,
            "capacity_assessment": capacity.assess(
                ctx,
                {
                    role: {
                        "filesystem_id": "dev:8:1:fsid:a",
                        "total_bytes": 60 * GIB,
                        "available_bytes": 30 * GIB,
                    }
                    for role in capacity.ROLES
                },
                peaks=peaks,
            ),
            "snapshot_lock_released_on_return": True,
            "execution_admission": False,
        },
    )
    with manager(simulated) as current:
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            result = b"failed"
            try:
                try:
                    current.refresh(peaks=peaks())
                except session.LabSessionError:
                    try:
                        current.__exit__(None, None, None)
                    except session.LabSessionError:
                        if current._lock.fd is None:
                            result = b"refused"
                os.write(write_fd, result)
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            assert os.read(read_fd, 16) == b"refused"
        finally:
            os.close(read_fd)
            os.waitpid(child, 0)
        contender = os.open(linux_files.root / Path(session.LOCK).name, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
