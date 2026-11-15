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


@pytest.fixture
def metadata_observer(simulated, monkeypatch):
    state = SimpleNamespace(calls=0, hook=lambda: None)
    summary = {
        "config_sha256": "a" * 64,
        "environment_entry_count": 8,
        "declared_volume_count": 0,
        "healthcheck_present": False,
        "onbuild_count": 0,
        "entrypoint_count": 0,
        "command_count": 1,
        "args_escaped": True,
    }
    state.receipt = {
        "schema": session.image_metadata.SCHEMA,
        "scope": session.image_metadata.SCOPE,
        "context_sha256": simulated.context.digest(),
        "engine_socket_peer_sha256": "e" * 64,
        "metadata_before_sha256": "f" * 64,
        "metadata_after_sha256": "f" * 64,
        "images": {role: copy.deepcopy(summary) for role, _ in simulated.context.images},
        "execution_admission": False,
        "compatibility_verified": False,
        "material_safety_verified": False,
    }

    def collect(ctx, *, lab_binding):
        assert simulated.locks[-1].held
        ctx.verify_journal_binding(lab_binding)
        simulated.calls.append("image-metadata")
        state.calls += 1
        state.hook()
        return copy.deepcopy(state.receipt)

    monkeypatch.setattr(session.image_metadata, "collect", collect)
    return state


def test_image_metadata_has_independent_binding_and_guarded_surrounding_storage(
    simulated, metadata_observer, bootstrap_observer
):
    with manager(simulated) as current:
        current.observe_bootstrap_engine(peaks=peaks())
        bootstrap_binding = current._bootstrap_binding
        simulated.calls.clear()
        result = current.observe_pinned_image_metadata(peaks=peaks())
        assert [item for item in simulated.calls if item in {"collect", "image-metadata"}] == [
            "collect",
            "image-metadata",
            "collect",
        ]
        assert current._bootstrap_binding == bootstrap_binding
        assert current._metadata_binding != bootstrap_binding
        assert result["image_metadata_observation"] == metadata_observer.receipt
        assert result["operation_lock_held_at_observation"] is True
        assert (
            result["storage_before"]["bindings_sha256"]
            == result["storage_after"]["bindings_sha256"]
        )
        assert all(
            result[key] is False
            for key in (
                "execution_admission",
                "compatibility_verified",
                "material_safety_verified",
            )
        )
        result["image_metadata_observation"]["images"]["original"]["environment_entry_count"] = 99
        assert current.observe_pinned_image_metadata(peaks=peaks())[
            "image_metadata_observation"
        ] == (metadata_observer.receipt)
    assert not simulated.locks[-1].held


@pytest.mark.parametrize(
    "bad", [None, {"evidence": True, "docker": 1}, {"evidence": 0, "docker": 0}]
)
def test_image_metadata_invalid_peaks_precede_all_io(simulated, metadata_observer, bad):
    with manager(simulated) as current:
        before = list(simulated.calls)
        with pytest.raises(session.LabSessionError):
            current.observe_pinned_image_metadata(peaks=bad)
        assert simulated.calls == before and metadata_observer.calls == 0
        assert current._failed is False


def test_image_metadata_peaks_are_detached_before_observer_callback(simulated, metadata_observer):
    upcoming = peaks()
    expected = dict(upcoming)
    with manager(simulated) as current:
        metadata_observer.hook = lambda: upcoming.update(evidence=0, docker=0)
        current.observe_pinned_image_metadata(peaks=upcoming)
    assert simulated.collected[-2:] == [expected, expected]


@pytest.mark.parametrize("change", ["binding", "guest", "storage", "lock"])
def test_image_metadata_observed_drift_latches_failure_before_further_collection(
    simulated, metadata_observer, change
):
    with manager(simulated) as current:
        initial_count = len(simulated.collected)

        def changed():
            if change == "binding":
                simulated.binding["context"]["source_commit"] = "f" * 40
            elif change == "guest":
                simulated.guest["boot"]["value"] = "99999999-2222-3333-4444-555555555555"
            elif change == "storage":
                simulated.storage_binding = "e" * 64
            else:
                simulated.locks[-1].binding_sha256 = lambda: "e" * 64

        metadata_observer.hook = changed
        with pytest.raises(session.LabSessionError):
            current.observe_pinned_image_metadata(peaks=peaks())
        assert current._failed and current._metadata_binding is None
        assert len(simulated.collected) == initial_count + (2 if change == "storage" else 1)
        previous = len(simulated.collected)
        with pytest.raises(session.LabSessionError):
            current.observe_pinned_image_metadata(peaks=peaks())
        assert len(simulated.collected) == previous and metadata_observer.calls == 1


@pytest.mark.parametrize(
    "operation",
    ["refresh", "observe_bootstrap_engine", "observe_pinned_image_metadata", "__enter__"],
)
def test_image_metadata_refuses_nested_observation_entry(simulated, metadata_observer, operation):
    with manager(simulated) as current:

        def nested():
            with pytest.raises(session.LabSessionError):
                if operation == "__enter__":
                    current.__enter__()
                else:
                    getattr(current, operation)(peaks=peaks())

        metadata_observer.hook = nested
        assert current.observe_pinned_image_metadata(peaks=peaks())["execution_admission"] is False
        assert metadata_observer.calls == 1 and len(simulated.collected) == 3


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_image_metadata_failure_latches_and_owner_always_closes(
    simulated, metadata_observer, failure
):
    def fail():
        raise failure("private-image-diagnostic")

    metadata_observer.hook = fail
    expected = session.LabSessionError if failure is RuntimeError else failure
    with pytest.raises(expected) as caught:
        with manager(simulated) as current:
            current.observe_pinned_image_metadata(peaks=peaks())
    assert current._failed and not simulated.locks[-1].held
    if failure is RuntimeError:
        assert "private-image" not in str(caught.value)
    with pytest.raises(session.LabSessionError):
        current.observe_pinned_image_metadata(peaks=peaks())
    assert metadata_observer.calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "other"),
        ("scope", "admitted"),
        ("context_sha256", "f" * 64),
        ("execution_admission", True),
        ("compatibility_verified", True),
        ("material_safety_verified", 0),
        ("metadata_after_sha256", "a" * 64),
        ("engine_socket_peer_sha256", "bad"),
        ("images", {}),
    ],
)
def test_image_metadata_malformed_receipt_never_advances_binding(
    simulated, metadata_observer, field, value
):
    with manager(simulated) as current:
        metadata_observer.receipt[field] = value
        with pytest.raises(session.LabSessionError):
            current.observe_pinned_image_metadata(peaks=peaks())
        assert current._failed and current._metadata_binding is None
        assert len(simulated.collected) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment_entry_count", False),
        ("command_count", 1.0),
        ("onbuild_count", 257),
        ("healthcheck_present", 1),
        ("args_escaped", 1),
        ("config_sha256", "invalid"),
    ],
)
def test_image_metadata_nested_summary_types_refuse(simulated, metadata_observer, field, value):
    with manager(simulated) as current:
        metadata_observer.receipt["images"]["original"][field] = value
        with pytest.raises(session.LabSessionError):
            current.observe_pinned_image_metadata(peaks=peaks())
        assert current._failed and current._metadata_binding is None


@pytest.mark.parametrize("change", ["engine", "snapshot", "summary"])
def test_repeated_image_binding_cannot_rebase_or_hide_summary_tampering(
    simulated, metadata_observer, change
):
    with manager(simulated) as current:
        current.observe_pinned_image_metadata(peaks=peaks())
        retained = current._metadata_binding
        if change == "engine":
            metadata_observer.receipt["engine_socket_peer_sha256"] = "b" * 64
        elif change == "snapshot":
            metadata_observer.receipt.update(
                metadata_before_sha256="b" * 64, metadata_after_sha256="b" * 64
            )
        else:
            metadata_observer.receipt["images"]["original"]["environment_entry_count"] += 1
        with pytest.raises(session.LabSessionError, match="binding changed"):
            current.observe_pinned_image_metadata(peaks=peaks())
        assert current._failed and current._metadata_binding == retained


def test_image_metadata_cross_thread_fork_and_use_after_exit_refuse_before_io(
    simulated, metadata_observer, monkeypatch
):
    with manager(simulated) as current:
        failures = []
        before = list(simulated.calls)

        def foreign():
            try:
                current.observe_pinned_image_metadata(peaks=peaks())
            except session.LabSessionError:
                failures.append(True)

        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=5)
        assert failures == [True] and simulated.calls == before
        real_pid = session.os.getpid
        monkeypatch.setattr(session.os, "getpid", lambda: current._creator_pid + 1)
        with pytest.raises(session.LabSessionError, match="process"):
            current.observe_pinned_image_metadata(peaks=peaks())
        monkeypatch.setattr(session.os, "getpid", real_pid)
        assert simulated.calls == before and metadata_observer.calls == 0
    before = list(simulated.calls)
    with pytest.raises(session.LabSessionError):
        current.observe_pinned_image_metadata(peaks=peaks())
    assert simulated.calls == before and metadata_observer.calls == 0


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


@pytest.fixture
def bootstrap_observer(simulated, monkeypatch):
    state = SimpleNamespace(calls=0, hook=lambda: None)
    state.receipt = {
        "schema": session.bootstrap.SCHEMA,
        "scope": session.bootstrap.SCOPE,
        "context_sha256": simulated.context.digest(),
        "engine_socket_peer_sha256": "b" * 64,
        "inventory_before_sha256": "c" * 64,
        "inventory_after_sha256": "c" * 64,
        "observed_counts": {"containers": 0, "volumes": 0, "networks": 3, "endpoints": 0},
        "required_image_inspections": 3,
        "unrelated_images_observed": False,
        "execution_admission": False,
    }

    def collect(ctx, *, lab_binding):
        assert simulated.locks[-1].held
        ctx.verify_journal_binding(lab_binding)
        simulated.calls.append("bootstrap")
        state.calls += 1
        state.hook()
        return copy.deepcopy(state.receipt)

    monkeypatch.setattr(session.bootstrap, "collect", collect)
    return state


def test_bootstrap_has_guarded_storage_before_and_after_and_detached_result(
    simulated, bootstrap_observer
):
    with manager(simulated) as current:
        simulated.calls.clear()
        result = current.observe_bootstrap_engine(peaks=peaks())
        work = [item for item in simulated.calls if item in {"collect", "bootstrap"}]
        assert work == ["collect", "bootstrap", "collect"]
        assert result["execution_admission"] is False
        assert result["operation_lock_held_at_observation"] is True
        assert (
            result["storage_before"]["bindings_sha256"]
            == result["storage_after"]["bindings_sha256"]
        )
        assert result["bootstrap_observation"] == bootstrap_observer.receipt
        result["bootstrap_observation"]["execution_admission"] = True
        assert current.observe_bootstrap_engine(peaks=peaks())["execution_admission"] is False
    assert not simulated.locks[-1].held


@pytest.mark.parametrize(
    "bad", [None, {}, {"evidence": 0, "docker": 0}, {"evidence": False, "docker": 1}]
)
def test_bootstrap_invalid_peaks_precede_any_collection(simulated, bootstrap_observer, bad):
    with manager(simulated) as current:
        before = list(simulated.calls)
        with pytest.raises(session.LabSessionError):
            current.observe_bootstrap_engine(peaks=bad)
        assert simulated.calls == before and bootstrap_observer.calls == 0
        assert current.refresh(peaks=peaks())["execution_admission"] is False


def test_bootstrap_peaks_are_detached_before_callbacks(simulated, bootstrap_observer):
    upcoming = peaks()
    with manager(simulated) as current:
        bootstrap_observer.hook = lambda: upcoming.update(evidence=0, docker=0)
        current.observe_bootstrap_engine(peaks=upcoming)
        assert simulated.collected[-2:] == [peaks(), peaks()]


@pytest.mark.parametrize("change", ["binding", "guest", "storage", "lock", "pid"])
def test_bootstrap_drift_latches_failure_before_more_collection(
    simulated, bootstrap_observer, monkeypatch, change
):
    with manager(simulated) as current:
        before_count = len(simulated.collected)
        original_pid = session.os.getpid

        def changed():
            if change == "binding":
                simulated.binding["context"]["source_commit"] = "f" * 40
            elif change == "guest":
                simulated.guest["boot"]["value"] = "99999999-2222-3333-4444-555555555555"
            elif change == "storage":
                simulated.storage_binding = "e" * 64
            elif change == "lock":
                simulated.locks[-1].binding_sha256 = lambda: "e" * 64
            else:
                monkeypatch.setattr(session.os, "getpid", lambda: current._creator_pid + 1)

        bootstrap_observer.hook = changed
        with pytest.raises(session.LabSessionError):
            current.observe_bootstrap_engine(peaks=peaks())
        monkeypatch.setattr(session.os, "getpid", original_pid)
        assert current._failed is True
        assert len(simulated.collected) == before_count + (2 if change == "storage" else 1)
        previous = len(simulated.collected)
        with pytest.raises(session.LabSessionError):
            current.refresh(peaks=peaks())
        assert len(simulated.collected) == previous and bootstrap_observer.calls == 1


@pytest.mark.parametrize("operation", ["refresh", "observe_bootstrap_engine", "__enter__"])
def test_bootstrap_refuses_nested_observation_entry(simulated, bootstrap_observer, operation):
    with manager(simulated) as current:

        def nested():
            with pytest.raises(session.LabSessionError):
                if operation == "__enter__":
                    current.__enter__()
                else:
                    getattr(current, operation)(peaks=peaks())

        bootstrap_observer.hook = nested
        assert current.observe_bootstrap_engine(peaks=peaks())["execution_admission"] is False
        assert bootstrap_observer.calls == 1 and len(simulated.collected) == 3


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_bootstrap_collection_failure_latches_and_owner_always_closes(
    simulated, bootstrap_observer, failure
):
    def fail():
        raise failure("private-engine-diagnostic")

    bootstrap_observer.hook = fail
    expected = session.LabSessionError if failure is RuntimeError else failure
    with pytest.raises(expected) as caught:
        with manager(simulated) as current:
            current.observe_bootstrap_engine(peaks=peaks())
    assert current._failed and not simulated.locks[-1].held
    if failure is RuntimeError:
        assert "private-engine" not in str(caught.value)
    with pytest.raises(session.LabSessionError):
        current.observe_bootstrap_engine(peaks=peaks())
    assert bootstrap_observer.calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "other"),
        ("scope", "admitted"),
        ("context_sha256", "f" * 64),
        ("execution_admission", True),
        ("execution_admission", 0),
        ("unrelated_images_observed", True),
        ("required_image_inspections", 3.0),
        ("engine_socket_peer_sha256", "bad"),
        ("inventory_after_sha256", "f" * 64),
        ("observed_counts", {"containers": False, "volumes": 0, "networks": 3, "endpoints": 0}),
    ],
)
def test_bootstrap_receipt_consistency_refusal_is_latched(
    simulated, bootstrap_observer, field, value
):
    with manager(simulated) as current:
        bootstrap_observer.receipt[field] = value
        with pytest.raises(session.LabSessionError, match="receipt"):
            current.observe_bootstrap_engine(peaks=peaks())
        assert current._failed and len(simulated.collected) == 2


def test_bootstrap_repeated_binding_drift_refuses(simulated, bootstrap_observer):
    with manager(simulated) as current:
        current.observe_bootstrap_engine(peaks=peaks())
        bootstrap_observer.receipt["engine_socket_peer_sha256"] = "f" * 64
        with pytest.raises(session.LabSessionError, match="binding changed"):
            current.observe_bootstrap_engine(peaks=peaks())
        assert current._failed


def test_bootstrap_cross_thread_and_forked_pid_refuse_before_io(
    simulated, bootstrap_observer, monkeypatch
):
    with manager(simulated) as current:
        failures = []
        before = list(simulated.calls)

        def foreign():
            try:
                current.observe_bootstrap_engine(peaks=peaks())
            except session.LabSessionError:
                failures.append(True)

        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=5)
        assert failures == [True] and simulated.calls == before
        real_pid = session.os.getpid
        monkeypatch.setattr(session.os, "getpid", lambda: current._creator_pid + 1)
        with pytest.raises(session.LabSessionError, match="process"):
            current.observe_bootstrap_engine(peaks=peaks())
        monkeypatch.setattr(session.os, "getpid", real_pid)
        assert simulated.calls == before and bootstrap_observer.calls == 0


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
