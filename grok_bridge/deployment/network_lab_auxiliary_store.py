"""Bounded separate auxiliary-file persistence; no runtime or ownership authority.

The exact generation and a distinct private auxiliary lock must already exist on
writable ext4/xfs. Future controllers acquire operation before auxiliary. This
module neither provisions paths nor writes through the read-only observation
session. Fixed no-follow, descriptor-relative leaves and held ancestry/mount
checks coordinate cooperating writers; compare-then-replace is NOT atomic content
CAS against an uncooperative/root writer.

Initial publication uses Linux RENAME_NOREPLACE with no overwrite fallback.
Updates validate actual stored history and one canonical appended event, separately
from raw-byte/file-identity conflict checks. Failed staging is retained. An
unacknowledged publication returns no new receipt and is never rolled back or
retried here. Cold reads report content consistency only, not completion of a
previous save's synchronization sequence, power-loss-tested recovery, attributable
Docker creation or restored volume continuity. Existing recovery gates are untouched.
"""

import copy
import ctypes
import hashlib
import os
import stat
import sys

import network_lab_auxiliary_state as auxiliary
import network_lab_capacity as capacity
import network_lab_storage as storage

JOURNAL = "auxiliary-state.lab.private.json"
PENDING = ".auxiliary-state.lab.private.pending"
LOCK = ".auxiliary-state.lab.lock"
OPPOSITE = "auxiliary-state.private.json"
RECEIPT_SCHEMA = "grok-network-lab-auxiliary-file-receipt-v1"
_READ_STEPS = ["bounded_readback"]
_SAVE_STEPS = [
    "pending_file_fsync",
    "pending_readback",
    "pending_directory_fsync",
    "publication_directory_fsync",
    "final_readback",
]


class LabAuxiliaryStoreError(RuntimeError):
    """Sanitized filesystem/record refusal without private paths or OS details."""


class LabAuxiliaryCommitUncertain(LabAuxiliaryStoreError):
    """Publication was attempted but no complete acknowledgment can be returned."""


def _require(condition, message):
    if not condition:
        raise LabAuxiliaryStoreError(message)


def _platform():
    _require(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        "Auxiliary persistence requires root on Linux",
    )


def _identity(info):
    return tuple(
        getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _private(info):
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == 0
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600,
        "Auxiliary leaf must be private, root-owned, regular and single-link",
    )


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _bind(context, binding):
    ctx = capacity._context(context)
    detached = auxiliary._snapshot(binding)
    ctx.verify_journal_binding(detached)
    return ctx, detached


def _expected(value, ctx, transaction):
    if value is None:
        return None
    receipt = auxiliary._snapshot(value)
    _require(
        capacity._fields(
            receipt,
            {
                "schema",
                "scope",
                "context_sha256",
                "transaction_id",
                "raw_sha256",
                "file_identity_sha256",
                "canonical_state_sha256",
                "operation",
                "completed_steps",
                "execution_admission",
            },
        ),
        "Invalid auxiliary file receipt fields",
    )
    operation = receipt["operation"]
    _require(
        receipt["schema"] == RECEIPT_SCHEMA
        and receipt["scope"] == "file_consistency_only"
        and receipt["context_sha256"] == ctx.digest()
        and receipt["transaction_id"] == transaction
        and operation in ("load", "save")
        and receipt["execution_admission"] is False
        and capacity._exact(
            receipt["completed_steps"], _READ_STEPS if operation == "load" else _SAVE_STEPS
        )
        and all(
            auxiliary._hex(receipt[key])
            for key in (
                "raw_sha256",
                "file_identity_sha256",
                "canonical_state_sha256",
            )
        ),
        "Auxiliary file receipt binding or claims differ",
    )
    return receipt


class _Scope:
    """Private per-call auxiliary flock; close-only release remains safe after fork."""

    def __init__(self, context, binding, ctx, retained):
        self.source_context, self.source_binding = context, binding
        self.context, self.binding = ctx, retained
        self.pid = None
        self.chain, self.lock_fd = None, None
        try:
            _platform()
            self.pid = os.getpid()
            self.chain = storage._DirectoryChain(
                str(ctx.generation_root), evidence=True, storage=True
            )
            self.absent(OPPOSITE)
            self.absent(PENDING)
            original = self.entry(LOCK)
            _require(original is not None, "Auxiliary lock must already exist")
            _private(original)
            self.lock_fd = os.open(
                LOCK,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=self.chain.fd,
            )
            _require(
                _identity(os.fstat(self.lock_fd)) == _identity(original),
                "Auxiliary lock changed before acquisition",
            )
            import fcntl

            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LabAuxiliaryStoreError("Auxiliary lock is busy") from None
            self.lock_identity = _identity(original)
            self.guard()
            self.absent(PENDING)
        except BaseException:
            self.close()
            raise

    def entry(self, name):
        try:
            return os.stat(name, dir_fd=self.chain.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def absent(self, name):
        _require(self.entry(name) is None, "Auxiliary leaf already exists; retain it for review")

    def guard(self):
        _require(os.getpid() == self.pid, "Auxiliary store cannot cross processes")
        ctx, binding = _bind(self.source_context, self.source_binding)
        _require(
            ctx == self.context and capacity._exact(binding, self.binding),
            "Auxiliary store context or binding changed",
        )
        self.chain.verify()
        held, current = os.fstat(self.lock_fd), self.entry(LOCK)
        _private(held)
        _require(
            current is not None
            and _identity(held) == self.lock_identity
            and _identity(current) == self.lock_identity,
            "Auxiliary held lock changed",
        )
        self.absent(OPPOSITE)

    def read(self, name):
        self.guard()
        path_info = self.entry(name)
        _require(path_info is not None, "Auxiliary journal leaf is absent")
        _private(path_info)
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.chain.fd
        )
        try:
            before = os.fstat(fd)
            _private(before)
            _require(
                _identity(before) == _identity(path_info),
                "Auxiliary journal leaf changed before opening",
            )
            _require(before.st_size <= auxiliary.MAX_RECORD_BYTES, "Auxiliary file exceeds bound")
            chunks, count = [], 0
            while True:
                chunk = os.read(fd, min(16384, auxiliary.MAX_RECORD_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                _require(count <= auxiliary.MAX_RECORD_BYTES, "Auxiliary read exceeds bound")
            after, current = os.fstat(fd), self.entry(name)
            _private(after)
            _require(
                current is not None
                and _identity(before) == _identity(after)
                and _identity(current) == _identity(after)
                and count == after.st_size,
                "Auxiliary file changed during read",
            )
            data = b"".join(chunks)
        finally:
            os.close(fd)
        self.guard()
        return data, after

    def close(self):
        descriptor, chain = self.lock_fd, self.chain
        self.lock_fd, self.chain = None, None
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            if chain is not None:
                chain.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _receipt(ctx, state, data, info, operation):
    return {
        "state": copy.deepcopy(state),
        "receipt": {
            "schema": RECEIPT_SCHEMA,
            "scope": "file_consistency_only",
            "context_sha256": ctx.digest(),
            "transaction_id": state["transaction_id"],
            "raw_sha256": _hash(data),
            "file_identity_sha256": capacity._digest(_identity(info)),
            "canonical_state_sha256": auxiliary._digest(state),
            "operation": operation,
            "completed_steps": list(_READ_STEPS if operation == "load" else _SAVE_STEPS),
            "execution_admission": False,
        },
    }


def _matches(receipt, state, data, info):
    _require(
        receipt["raw_sha256"] == _hash(data)
        and receipt["file_identity_sha256"] == capacity._digest(_identity(info))
        and receipt["canonical_state_sha256"] == auxiliary._digest(state)
        and receipt["transaction_id"] == state["transaction_id"],
        "Auxiliary raw bytes, identity or stored record conflict",
    )


def _rename_noreplace(directory):
    """Use the libc Linux interface; missing/unsupported support never falls back."""
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renameat2
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(directory, PENDING.encode("ascii"), directory, JOURNAL.encode("ascii"), 1)
    if result != 0:
        raise OSError(ctypes.get_errno(), "Auxiliary no-clobber publication failed")


def load(context, *, lab_binding):
    """Read historical consistency only; no previous fsync/continuity acknowledgment."""
    try:
        ctx, binding = _bind(context, lab_binding)
        with _Scope(context, lab_binding, ctx, binding) as scope:
            data, info = scope.read(JOURNAL)
            state = auxiliary.validate_state(ctx, lab_binding=binding, state=data)
            scope.guard()
            scope.absent(PENDING)
            return _receipt(ctx, state, data, info, "load")
    except LabAuxiliaryStoreError:
        raise
    except Exception:  # noqa: BLE001 -- filesystem/record diagnostics may contain private values.
        raise LabAuxiliaryStoreError("Auxiliary load refused; retain private evidence") from None


def save(context, *, lab_binding, state, expected=None):
    """Publish one planned initial state or one appended event; never mutate inputs.

    A supplied receipt is a consistency condition, not a capability. No advanced
    receipt is returned if publication or its subsequent acknowledgment fails.
    Interrupted pending files remain untouched for separately reviewed recovery.
    """
    publication_attempted = False
    try:
        ctx, binding = _bind(context, lab_binding)
        desired = auxiliary.validate_state(ctx, lab_binding=binding, state=state)
        prior = _expected(expected, ctx, desired["transaction_id"])
        if prior is None:
            _require(
                not desired["events"] and desired["predecessor_sha256"] is None,
                "Initial auxiliary publication requires the empty planned state",
            )
        data = auxiliary._encoded(desired) + b"\n"
        _require(len(data) <= auxiliary.MAX_RECORD_BYTES, "Auxiliary write exceeds bound")

        def inputs_guard():
            _require(
                capacity._exact(auxiliary._snapshot(state), desired)
                and capacity._exact(_expected(expected, ctx, desired["transaction_id"]), prior),
                "Auxiliary store caller inputs changed",
            )

        inputs_guard()
        with _Scope(context, lab_binding, ctx, binding) as scope:
            previous = previous_info = None
            if prior is None:
                scope.absent(JOURNAL)
            else:
                previous, previous_info = scope.read(JOURNAL)
                old = auxiliary.validate_state(ctx, lab_binding=binding, state=previous)
                _matches(prior, old, previous, previous_info)
                auxiliary.validate_state(ctx, lab_binding=binding, state=desired, previous=old)
            inputs_guard()
            scope.guard()
            scope.absent(PENDING)
            descriptor = os.open(
                PENDING,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=scope.chain.fd,
            )
            try:
                _private(os.fstat(descriptor))
                offset = 0
                while offset < len(data):
                    count = os.write(descriptor, data[offset:])
                    _require(
                        type(count) is int and 0 < count <= len(data) - offset,
                        "Auxiliary staging write did not advance",
                    )
                    offset += count
                os.fsync(descriptor)
                staged_info = os.fstat(descriptor)
                _private(staged_info)
            finally:
                os.close(descriptor)
            staged, checked_info = scope.read(PENDING)
            _require(
                staged == data and _identity(checked_info) == _identity(staged_info),
                "Auxiliary pending file changed",
            )
            os.fsync(scope.chain.fd)
            scope.guard()
            inputs_guard()
            if prior is None:
                scope.absent(JOURNAL)
            else:
                current, current_info = scope.read(JOURNAL)
                _require(
                    current == previous and _identity(current_info) == _identity(previous_info),
                    "Auxiliary current file changed before publication",
                )
            staged, checked_info = scope.read(PENDING)
            _require(
                staged == data and _identity(checked_info) == _identity(staged_info),
                "Auxiliary pending file changed before publication",
            )
            scope.guard()
            inputs_guard()
            publication_attempted = True
            if prior is None:
                _rename_noreplace(scope.chain.fd)
            else:
                os.replace(PENDING, JOURNAL, src_dir_fd=scope.chain.fd, dst_dir_fd=scope.chain.fd)
            os.fsync(scope.chain.fd)
            final, final_info = scope.read(JOURNAL)
            # Rename may change ctime. All other recorded identity/content fields
            # must still describe the exact staged inode; final receipts bind ctime.
            _require(
                final == data and _identity(final_info)[:-1] == _identity(staged_info)[:-1],
                "Auxiliary published file changed before acknowledgment",
            )
            auxiliary.validate_state(ctx, lab_binding=binding, state=final)
            scope.guard()
            scope.absent(PENDING)
            inputs_guard()
            return _receipt(ctx, desired, final, final_info, "save")
    except Exception as error:  # noqa: BLE001 -- callers must never receive raw OS/record errors.
        if publication_attempted:
            raise LabAuxiliaryCommitUncertain(
                "Auxiliary publication is unacknowledged; retain evidence for reconciliation"
            ) from None
        if isinstance(error, LabAuxiliaryStoreError):
            raise
        raise LabAuxiliaryStoreError("Auxiliary save refused; retain private evidence") from None
