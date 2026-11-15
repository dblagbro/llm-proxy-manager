"""Pure refusals plus Linux temporary FD/flock/rename/fsync mechanics.

Linux tests map only the fixed logical root into a private temporary tree and
simulate UID/mount observations there. Actual descriptor-relative operations,
file metadata, flock and renameat2 are used. This is not live guest, filesystem
classification, admission or power-loss evidence. Windows skips Linux mechanics.
"""

import copy
import errno
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import network_lab_auxiliary_state as auxiliary
import network_lab_auxiliary_store as store
import pytest
from test_network_lab_auxiliary_state import context, history, initial

LINUX = sys.platform.startswith("linux")


def supplied_receipt(state=None):
    state = state or initial()
    return {
        "schema": store.RECEIPT_SCHEMA,
        "scope": "file_consistency_only",
        "context_sha256": context().digest(),
        "transaction_id": state["transaction_id"],
        "raw_sha256": "1" * 64,
        "file_identity_sha256": "2" * 64,
        "canonical_state_sha256": auxiliary._digest(state),
        "operation": "load",
        "completed_steps": ["bounded_readback"],
        "execution_admission": False,
    }


@pytest.fixture
def no_io(monkeypatch):
    calls = []

    def forbidden():
        calls.append(True)
        raise AssertionError("Unexpected filesystem entry")

    monkeypatch.setattr(store, "_platform", forbidden)
    return calls


@pytest.mark.parametrize("binding", [None, {}, False, "lab"])
def test_invalid_binding_refuses_before_filesystem_entry(no_io, binding):
    with pytest.raises(store.LabAuxiliaryStoreError):
        store.save(context(), lab_binding=binding, state=initial())
    with pytest.raises(store.LabAuxiliaryStoreError):
        store.load(context(), lab_binding=binding)
    assert no_io == []


def test_full_context_drift_refuses_before_filesystem_entry(no_io):
    ctx = context()
    other = replace(ctx, source_commit="f" * 40)
    with pytest.raises(store.LabAuxiliaryStoreError):
        store.save(other, lab_binding=ctx.journal_binding(), state=initial())
    assert no_io == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "authorized"),
        ("execution_admission", True),
        ("execution_admission", 0),
        ("transaction_id", "0" * 32),
        ("context_sha256", "0" * 64),
        ("raw_sha256", "short"),
        ("file_identity_sha256", False),
        ("canonical_state_sha256", "bad"),
        ("operation", "recovered"),
        ("completed_steps", ["file_fsync"]),
        ("unknown", True),
    ],
)
def test_invalid_expected_receipt_refuses_before_io(no_io, field, value):
    receipt = supplied_receipt()
    receipt[field] = value
    with pytest.raises(store.LabAuxiliaryStoreError):
        store.save(
            context(), lab_binding=context().journal_binding(), state=history()[1], expected=receipt
        )
    assert no_io == []


@pytest.mark.parametrize("state", [None, {}, False, b'{"events":[],"events":[]}', history()[1]])
def test_initial_import_or_malformed_state_refuses_before_io(no_io, state):
    with pytest.raises(store.LabAuxiliaryStoreError):
        store.save(context(), lab_binding=context().journal_binding(), state=state)
    assert no_io == []


@pytest.mark.parametrize("platform,uid", [("win32", 0), ("linux", 1000)])
def test_actual_platform_guard_refuses_unsupported_hosts(monkeypatch, platform, uid):
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(store, "os", SimpleNamespace(geteuid=lambda: uid))
    with pytest.raises(store.LabAuxiliaryStoreError, match="Linux"):
        store.save(context(), lab_binding=context().journal_binding(), state=initial())


def test_no_public_path_lock_or_authority_override_exists(no_io):
    with pytest.raises(TypeError):
        store.save(
            context(),
            lab_binding=context().journal_binding(),
            state=initial(),
            folder="/other",
            execution_admission=True,
        )
    assert no_io == []


@pytest.fixture
def linux_files(tmp_path, monkeypatch):
    if not LINUX:
        pytest.skip("Requires actual Linux directory FDs, flock and renameat2")
    ctx = context()
    anchor = tmp_path / "anchored-kernel-view"
    anchor.mkdir(mode=0o700)
    current = anchor
    for component in ctx.generation_root.parts[1:]:
        current /= component
        current.mkdir(mode=0o700)
    root = current
    lock = root / store.LOCK
    lock.write_bytes(b"preprovisioned lock\n")
    lock.chmod(0o600)
    state = SimpleNamespace(
        ctx=ctx,
        binding=ctx.journal_binding(),
        root=root,
        anchor=anchor,
        uid=0,
        uid_paths={},
        mount_id=23,
        filesystem="ext4",
        mount_root="/",
        options={"rw"},
        super_options={"rw"},
    )

    def fd_path(descriptor):
        return Path(os.readlink(f"/proc/self/fd/{descriptor}"))

    def mapped(info, target):
        target = Path(target)
        if target != anchor and anchor not in target.parents:
            return info
        values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
        values["st_uid"] = state.uid_paths.get(str(target), state.uid)
        return SimpleNamespace(**values)

    proxy = SimpleNamespace(**vars(os))

    def inspect(path, *, dir_fd=None, follow_symlinks=True):
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        target = Path(path)
        if not target.is_absolute():
            assert dir_fd is not None
            target = fd_path(dir_fd) / target
        return mapped(info, target)

    proxy.stat = inspect
    proxy.fstat = lambda descriptor: mapped(os.fstat(descriptor), fd_path(descriptor))
    proxy.geteuid = lambda: 0
    monkeypatch.setattr(store, "os", proxy)
    monkeypatch.setattr(store.storage, "os", proxy)
    monkeypatch.setattr(
        store.storage,
        "_open_root",
        lambda: os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC),
    )
    device = root.stat().st_dev

    def mounts():
        return {
            state.mount_id: {
                "id": state.mount_id,
                "fstype": state.filesystem,
                "root": state.mount_root,
                "target": "/",
                "device": f"{os.major(device)}:{os.minor(device)}",
                "options": set(state.options),
                "super_options": set(state.super_options),
            }
        }

    monkeypatch.setattr(store.storage, "_mounts", mounts)
    monkeypatch.setattr(store.storage, "_fd_mount_id", lambda _descriptor: state.mount_id)
    state.proxy = proxy
    return state


def save(files, state=None, expected=None):
    return store.save(
        files.ctx, lab_binding=files.binding, state=state or initial(), expected=expected
    )


def load(files):
    return store.load(files.ctx, lab_binding=files.binding)


def test_linux_initial_save_load_and_update_keep_receipts_separate(linux_files):
    files = linux_files
    state = initial()
    before = copy.deepcopy(state)
    written = save(files, state)
    assert state == before and written["state"] == before
    assert written["receipt"]["operation"] == "save"
    assert written["receipt"]["completed_steps"] == store._SAVE_STEPS
    assert written["receipt"]["raw_sha256"] != written["receipt"]["canonical_state_sha256"]
    observed = load(files)
    assert observed["state"] == state
    assert observed["receipt"]["operation"] == "load"
    assert observed["receipt"]["completed_steps"] == ["bounded_readback"]
    assert observed["receipt"]["execution_admission"] is False
    expected = copy.deepcopy(observed["receipt"])
    updated = save(files, history()[1], expected)
    assert expected == observed["receipt"]
    assert load(files)["state"] == history()[1] == updated["state"]
    assert not (files.root / store.PENDING).exists()
    assert stat.S_IMODE((files.root / store.JOURNAL).stat().st_mode) == 0o600
    assert (files.root / store.LOCK).read_bytes() == b"preprovisioned lock\n"
    updated["state"]["execution_admission"] = True
    assert load(files)["state"]["execution_admission"] is False


def test_linux_cooperating_flock_excludes_second_open_without_unlocking_owner(linux_files):
    import fcntl

    files = linux_files
    ctx, binding = store._bind(files.ctx, files.binding)
    with store._Scope(files.ctx, files.binding, ctx, binding):
        with pytest.raises(store.LabAuxiliaryStoreError, match="busy"):
            save(files)
        descriptor = os.open(files.root / store.LOCK, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
    assert save(files)["receipt"]["operation"] == "save"


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "broken", "directory", "fifo", "hardlink", "mode", "owner"]
)
def test_linux_lock_refusals_do_not_provision_or_repair(linux_files, kind):
    files = linux_files
    path = files.root / store.LOCK
    if kind in {"missing", "symlink", "broken", "directory", "fifo"}:
        path.rename(files.root / "retained-lock")
        if kind in {"symlink", "broken"}:
            path.symlink_to(files.root / ("retained-lock" if kind == "symlink" else "missing"))
        elif kind == "directory":
            path.mkdir(mode=0o700)
        elif kind == "fifo":
            os.mkfifo(path, 0o600)
    elif kind == "hardlink":
        os.link(path, files.root / "second-lock-name")
    elif kind == "mode":
        path.chmod(0o640)
    else:
        files.uid_paths[str(path)] = 1000
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files)
    assert not (files.root / store.JOURNAL).exists() and not (files.root / store.PENDING).exists()


@pytest.mark.parametrize("name", [store.PENDING, store.OPPOSITE])
@pytest.mark.parametrize("kind", ["regular", "broken", "directory", "fifo"])
def test_linux_existing_pending_or_opposite_leaf_is_never_adopted(linux_files, name, kind):
    files = linux_files
    path = files.root / name
    if kind == "regular":
        path.write_bytes(b"retained evidence")
        path.chmod(0o600)
    elif kind == "broken":
        path.symlink_to(files.root / "missing")
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        os.mkfifo(path, 0o600)
    before = path.lstat()
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files)
    assert path.lstat().st_ino == before.st_ino
    assert not (files.root / store.JOURNAL).exists()


@pytest.mark.parametrize(
    "kind", ["symlink", "broken", "directory", "fifo", "hardlink", "mode", "owner"]
)
def test_linux_unsafe_journal_rejected_before_leaf_open(linux_files, monkeypatch, kind):
    files = linux_files
    save(files)
    path = files.root / store.JOURNAL
    path.rename(files.root / "retained-journal")
    if kind in {"symlink", "broken"}:
        path.symlink_to(files.root / ("retained-journal" if kind == "symlink" else "missing"))
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "hardlink":
        os.link(files.root / "retained-journal", path)
    else:
        path.write_bytes(b"private")
        path.chmod(0o640 if kind == "mode" else 0o600)
        if kind == "owner":
            files.uid_paths[str(path)] = 1000
    opened = []
    original = files.proxy.open

    def inspect(name, *args, **kwargs):
        if name == store.JOURNAL:
            opened.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(files.proxy, "open", inspect)
    with pytest.raises(store.LabAuxiliaryStoreError):
        load(files)
    assert opened == []


def test_linux_replacement_between_leaf_stat_and_open_refuses(linux_files, monkeypatch):
    files = linux_files
    save(files)
    path = files.root / store.JOURNAL
    original = files.proxy.open
    changed = []

    def replace_before_open(name, *args, **kwargs):
        if name == store.JOURNAL and not changed:
            data = path.read_bytes()
            path.rename(files.root / "retained-before-open")
            path.write_bytes(data)
            path.chmod(0o600)
            changed.append(True)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(files.proxy, "open", replace_before_open)
    with pytest.raises(store.LabAuxiliaryStoreError, match="before opening"):
        load(files)
    assert changed == [True]


@pytest.mark.parametrize("change", ["overlay", "nfs", "cifs", "readonly", "subtree", "parent-mode"])
def test_linux_unsupported_mount_or_private_ancestry_has_no_fallback(linux_files, change):
    files = linux_files
    if change in {"overlay", "nfs", "cifs"}:
        files.filesystem = change
    elif change == "readonly":
        files.options = {"ro"}
    elif change == "subtree":
        files.mount_root = "/subtree"
    else:
        files.root.parent.chmod(0o750)
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files)
    assert not (files.root / store.PENDING).exists()


@pytest.mark.parametrize("change", ["mount", "ancestor", "lock", "binding"])
def test_linux_guard_drift_after_staging_retains_pending_without_publication(
    linux_files, monkeypatch, change
):
    files = linux_files
    original = files.proxy.fsync
    calls = []

    def drift(descriptor):
        original(descriptor)
        calls.append(True)
        if len(calls) == 1:
            if change == "mount":
                files.mount_id += 1
            elif change == "ancestor":
                files.root.parent.chmod(0o750)
            elif change == "lock":
                (files.root / store.LOCK).chmod(0o640)
            else:
                files.binding["context"]["source_commit"] = "f" * 40

    monkeypatch.setattr(files.proxy, "fsync", drift)
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files)
    assert (files.root / store.PENDING).exists() and not (files.root / store.JOURNAL).exists()


def test_linux_initial_no_clobber_survives_last_moment_destination_race(linux_files, monkeypatch):
    files = linux_files
    original = store._rename_noreplace

    def race(directory):
        (files.root / store.JOURNAL).write_bytes(b"concurrent target")
        (files.root / store.JOURNAL).chmod(0o600)
        return original(directory)

    monkeypatch.setattr(store, "_rename_noreplace", race)
    monkeypatch.setattr(files.proxy, "replace", lambda *_a, **_k: pytest.fail("Overwrite fallback"))
    with pytest.raises(store.LabAuxiliaryCommitUncertain):
        save(files)
    assert (files.root / store.JOURNAL).read_bytes() == b"concurrent target"
    assert (files.root / store.PENDING).exists()


def test_linux_unsupported_no_clobber_does_not_fall_back(linux_files, monkeypatch):
    def unsupported(_directory):
        raise OSError(errno.ENOSYS, "private kernel diagnostic")

    monkeypatch.setattr(store, "_rename_noreplace", unsupported)
    monkeypatch.setattr(
        linux_files.proxy, "replace", lambda *_a, **_k: pytest.fail("Overwrite fallback")
    )
    with pytest.raises(store.LabAuxiliaryCommitUncertain) as caught:
        save(linux_files)
    assert "private kernel" not in str(caught.value)
    assert (linux_files.root / store.PENDING).exists()
    assert not (linux_files.root / store.JOURNAL).exists()


def test_linux_raw_file_digest_is_distinct_from_canonical_predecessor(linux_files):
    files = linux_files
    original = save(files)
    path = files.root / store.JOURNAL
    path.write_text(json.dumps(original["state"], indent=2) + "\n")
    with pytest.raises(store.LabAuxiliaryStoreError, match="conflict"):
        save(files, history()[1], original["receipt"])
    assert not (files.root / store.PENDING).exists()
    loaded = load(files)
    assert (
        loaded["receipt"]["canonical_state_sha256"] == original["receipt"]["canonical_state_sha256"]
    )
    assert loaded["receipt"]["raw_sha256"] != original["receipt"]["raw_sha256"]
    assert save(files, history()[1], loaded["receipt"])["state"] == history()[1]


def test_linux_same_bytes_replacement_still_conflicts_on_file_identity(linux_files):
    files = linux_files
    original = save(files)
    path = files.root / store.JOURNAL
    data = path.read_bytes()
    path.rename(files.root / "retained-original-inode")
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(store.LabAuxiliaryStoreError, match="conflict"):
        save(files, history()[1], original["receipt"])
    assert not (files.root / store.PENDING).exists()


def test_linux_raw_matching_receipt_cannot_replace_another_full_context(linux_files):
    files = linux_files
    other = replace(files.ctx, source_commit="f" * 40)
    state = initial(other)
    data = auxiliary._encoded(state) + b"\n"
    path = files.root / store.JOURNAL
    path.write_bytes(data)
    path.chmod(0o600)
    # Deliberately forged matching file metadata still cannot bypass stored-state validation.
    info = files.proxy.stat(path, follow_symlinks=False)
    fake = store._receipt(files.ctx, state, data, info, "load")["receipt"]
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files, history()[1], fake)
    assert path.read_bytes() == data and not (files.root / store.PENDING).exists()


def test_linux_actual_stored_state_requires_exactly_one_append(linux_files):
    files = linux_files
    original = save(files)
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files, history()[2], original["receipt"])
    assert load(files)["state"] == original["state"]
    assert not (files.root / store.PENDING).exists()


def test_linux_current_edit_before_publish_is_preserved_with_pending_evidence(
    linux_files, monkeypatch
):
    files = linux_files
    original = save(files)
    real_sync = files.proxy.fsync
    calls = []

    def edit(descriptor):
        real_sync(descriptor)
        calls.append(True)
        if len(calls) == 2:
            (files.root / store.JOURNAL).write_bytes(b"concurrent edit")

    monkeypatch.setattr(files.proxy, "fsync", edit)
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(files, history()[1], original["receipt"])
    assert (files.root / store.JOURNAL).read_bytes() == b"concurrent edit"
    assert (files.root / store.PENDING).exists()


def test_linux_complete_partial_writes_are_handled(linux_files, monkeypatch):
    original = linux_files.proxy.write
    counts = []

    def partial(descriptor, data):
        count = original(descriptor, data[:37])
        counts.append(count)
        return count

    monkeypatch.setattr(linux_files.proxy, "write", partial)
    assert save(linux_files)["state"] == initial()
    assert len(counts) > 1


def test_linux_zero_write_retains_pending_and_refuses_automatic_retry(linux_files, monkeypatch):
    monkeypatch.setattr(linux_files.proxy, "write", lambda *_: 0)
    state = initial()
    before = copy.deepcopy(state)
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(linux_files, state)
    pending = linux_files.root / store.PENDING
    identity = pending.stat().st_ino
    with pytest.raises(store.LabAuxiliaryStoreError):
        save(linux_files, state)
    assert pending.stat().st_ino == identity and pending.read_bytes() == b""
    assert state == before and not (linux_files.root / store.JOURNAL).exists()


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_linux_fsync_failure_never_returns_advanced_receipt(linux_files, monkeypatch, fail_at):
    files = linux_files
    real_sync = files.proxy.fsync
    calls = []

    def fail(descriptor):
        calls.append(True)
        if len(calls) == fail_at:
            raise OSError("private fsync diagnostic")
        return real_sync(descriptor)

    monkeypatch.setattr(files.proxy, "fsync", fail)
    state = initial()
    before = copy.deepcopy(state)
    error = store.LabAuxiliaryCommitUncertain if fail_at == 3 else store.LabAuxiliaryStoreError
    with pytest.raises(error) as caught:
        save(files, state)
    assert "private fsync" not in str(caught.value) and state == before
    if fail_at < 3:
        assert (files.root / store.PENDING).exists() and not (files.root / store.JOURNAL).exists()
    else:
        assert not (files.root / store.PENDING).exists() and (files.root / store.JOURNAL).exists()
        cold = load(files)
        assert cold["receipt"]["operation"] == "load"
        assert cold["receipt"]["completed_steps"] == ["bounded_readback"]


def test_linux_failed_final_readback_is_uncertain_without_writeback(linux_files, monkeypatch):
    original = store._Scope.read

    def fail_final(scope, name):
        if name == store.JOURNAL:
            raise OSError("private readback diagnostic")
        return original(scope, name)

    monkeypatch.setattr(store._Scope, "read", fail_final)
    with pytest.raises(store.LabAuxiliaryCommitUncertain):
        save(linux_files)
    assert (linux_files.root / store.JOURNAL).read_bytes() == auxiliary._encoded(initial()) + b"\n"
    assert not (linux_files.root / store.PENDING).exists()


@pytest.mark.parametrize("failure", ["after_replace", "directory_fsync"])
def test_linux_update_publication_ambiguity_never_advances_caller_receipt(
    linux_files, monkeypatch, failure
):
    files = linux_files
    acknowledged = save(files)
    expected = acknowledged["receipt"]
    next_state = history()[1]
    old_expected, old_state = copy.deepcopy(expected), copy.deepcopy(next_state)
    real_replace, real_sync = files.proxy.replace, files.proxy.fsync
    publications, synchronizations = [], []

    def replace_once(*args, **kwargs):
        publications.append(True)
        real_replace(*args, **kwargs)
        if failure == "after_replace":
            raise OSError("private replacement diagnostic")

    def sync_or_fail(descriptor):
        synchronizations.append(True)
        if failure == "directory_fsync" and len(synchronizations) == 3:
            raise OSError("private directory-sync diagnostic")
        return real_sync(descriptor)

    monkeypatch.setattr(files.proxy, "replace", replace_once)
    monkeypatch.setattr(files.proxy, "fsync", sync_or_fail)
    with pytest.raises(store.LabAuxiliaryCommitUncertain) as caught:
        save(files, next_state, expected)
    assert "private" not in str(caught.value)
    assert publications == [True]  # No internal retry or write-back of the old state.
    assert expected == old_expected and next_state == old_state
    assert (files.root / store.JOURNAL).read_bytes() == auxiliary._encoded(next_state) + b"\n"
    assert not (files.root / store.PENDING).exists()
    cold = load(files)
    assert cold["state"] == next_state
    assert cold["receipt"]["operation"] == "load"
    assert cold["receipt"]["completed_steps"] == ["bounded_readback"]
    assert cold["receipt"]["execution_admission"] is False
    assert expected == old_expected and publications == [True]


def test_linux_read_growth_is_refused(linux_files, monkeypatch):
    files = linux_files
    save(files)
    original = files.proxy.read
    grown = []

    def grow(descriptor, bound):
        data = original(descriptor, bound)
        if not grown:
            with (files.root / store.JOURNAL).open("ab") as stream:
                stream.write(b" ")
            grown.append(True)
        return data

    monkeypatch.setattr(files.proxy, "read", grow)
    with pytest.raises(store.LabAuxiliaryStoreError, match="changed during read"):
        load(files)


def test_linux_oversized_or_duplicate_key_file_is_never_trusted(linux_files):
    path = linux_files.root / store.JOURNAL
    for data in (b"x" * (auxiliary.MAX_RECORD_BYTES + 1), b'{"events":[],"events":[]}'):
        path.write_bytes(data)
        path.chmod(0o600)
        with pytest.raises(store.LabAuxiliaryStoreError):
            load(linux_files)


def test_linux_baseexception_releases_flock_and_retains_partial_staging(linux_files, monkeypatch):
    import fcntl

    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(linux_files.proxy, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        save(linux_files)
    assert (linux_files.root / store.PENDING).exists()
    descriptor = os.open(linux_files.root / store.LOCK, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)
