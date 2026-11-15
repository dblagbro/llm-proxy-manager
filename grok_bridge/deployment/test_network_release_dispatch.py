"""Production dispatch tests using retained synthetic transaction fixtures only."""

import copy
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import network_lab_context as lab
import pytest
import release as r
import test_release as fixtures

_REAL_CAPACITY = r.capacity
_REAL_SNAPSHOT = r.fresh_snapshot


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    # Reuse the existing command prohibition and explicit Windows fcntl substitute.
    fixtures.no_real_host_commands.__wrapped__(monkeypatch)


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    return fixtures.transaction.__wrapped__(tmp_path, monkeypatch)


class NoHost:
    def __getattribute__(self, name):
        raise AssertionError("Refused dispatch reached a host action")


@pytest.mark.parametrize("entry", [r.deploy, r._deploy_transaction])
@pytest.mark.parametrize(
    "missing",
    [
        "source_merged",
        "required_checks_passed",
        "grok_release_gate_satisfied",
        "browser_acceptance_passed",
        "private_recovery_passed",
        "network_plan_reviewed",
        "network_recovery_acceptance_passed",
        "network_outsider_denial_passed",
        "multi_network_nginx_validation_passed",
        "operator_deployment_approved",
    ],
)
def test_every_production_gate_precedes_dependency_construction_and_io(
    entry, missing, tmp_path, monkeypatch
):
    evidence = fixtures.approval()
    evidence.pop(missing)
    state = {"phase": "prepared_not_deployed"}
    before = copy.deepcopy(state)
    built = []
    monkeypatch.setattr(r, "_ProductionDeployment", lambda *_: built.append(True))
    with pytest.raises(r.Refused, match=missing):
        entry(tmp_path, state, NoHost(), evidence)
    assert built == [] and state == before and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [r.deploy, r._deploy_transaction])
@pytest.mark.parametrize(
    "kind",
    ["header", "nested", "project", "data", "files", "observer", "lab-evidence", "generation"],
)
def test_lab_inputs_refuse_before_dependency_or_host_actions(entry, kind, tmp_path, monkeypatch):
    state = {"phase": "prepared_not_deployed"}
    evidence, folder = fixtures.approval(), tmp_path
    if kind == "header":
        state["mode"] = "lab"
    elif kind == "nested":
        state["network_transaction"] = {"schema": 2}
    elif kind == "project":
        state["project"] = "codex-grok-nettest-retained"
    elif kind == "data":
        state["candidate_data"] = str(lab.ROOT / "retained" / "candidate-data")
    elif kind == "files":
        state["files"] = {"compose": str(lab.ROOT / "retained" / "compose.yml")}
    elif kind in {"observer", "lab-evidence"}:
        target = state if kind == "observer" else evidence
        target.update(schema="grok-network-lab-observation-session-v1", execution_admission=False)
    else:
        folder = PurePosixPath(lab.ROOT / "retained")
    before = copy.deepcopy(state)
    built = []
    monkeypatch.setattr(r, "_ProductionDeployment", lambda *_: built.append(True))
    with pytest.raises(r.Refused, match="Lab"):
        entry(folder, state, NoHost(), evidence)
    assert built == [] and state == before and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [r.deploy, r._deploy_transaction])
@pytest.mark.parametrize(
    "argument",
    ["lab_context", "dependencies", "adapter", "capacity", "candidate_image", "project_root"],
)
def test_dispatch_has_no_caller_dependency_or_context_override(entry, argument, tmp_path):
    with pytest.raises(TypeError):
        entry(tmp_path, {}, NoHost(), fixtures.approval(), **{argument: object()})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [r.deploy, r._deploy_transaction])
def test_one_gate_call_and_intents_are_durable_before_mutations(transaction, monkeypatch, entry):
    folder, state, host = transaction
    events = []
    real_gate, real_stop, real_connect = r.gate, host.stop, host.network_host.connect

    def gate(evidence):
        assert host.calls == []
        events.append("gate")
        real_gate(evidence)

    def connect(*args):
        saved = json.loads((folder / "state.private.json").read_bytes())
        assert saved["phase"] == "attaching_consumers" and saved["network_intent"] is True
        assert saved["stop_intent"] is False
        events.append("attach")
        return real_connect(*args)

    def stop(identity, allow_unclean=False):
        saved = json.loads((folder / "state.private.json").read_bytes())
        assert saved["phase"] == "quiescing_original" and saved["stop_intent"] is True
        assert saved["network_transaction"]["phase"] == "attached"
        events.append("stop")
        return real_stop(identity, allow_unclean)

    def capacity(path):
        assert path == folder and state["deployed"] is False
        events.append("capacity")

    monkeypatch.setattr(r, "gate", gate)
    monkeypatch.setattr(host.network_host, "connect", connect)
    monkeypatch.setattr(host, "stop", stop)
    monkeypatch.setattr(r, "capacity", capacity)
    assert entry(folder, state, host, fixtures.approval()) is state
    assert events == ["gate", "attach", "attach", "stop", "capacity"]
    assert state["phase"] == "deployed_local_checks_passed"
    assert state["provider_acceptance"] is False and state["public_browser_acceptance"] is False


def test_ambiguous_candidate_is_recorded_before_candidate_stop_and_original_restore(
    transaction, monkeypatch
):
    folder, state, host = transaction
    host.up_timeout = True
    events = []
    original_record, original_stop, original_rollback = r.record_candidate, host.stop, r.rollback

    def record(*args):
        original_record(*args)
        saved = r.load_state(folder)
        assert saved["candidate_id"] == fixtures.CANDIDATE_ID
        assert saved["network_transaction"]["bridge_id"] == fixtures.CANDIDATE_ID
        events.append("candidate-recorded")

    def stop(identity, allow_unclean=False):
        if identity == fixtures.CANDIDATE_ID:
            assert events == ["candidate-recorded", "rollback"]
            events.append("candidate-stop")
        return original_stop(identity, allow_unclean)

    def rollback(*args):
        assert state["phase"] == "deployment_failed"
        events.append("rollback")
        return original_rollback(*args)

    monkeypatch.setattr(r, "record_candidate", record)
    monkeypatch.setattr(host, "stop", stop)
    monkeypatch.setattr(r, "rollback", rollback)
    with pytest.raises(TimeoutError):
        r.deploy(folder, state, host, fixtures.approval())
    assert events == ["candidate-recorded", "rollback", "candidate-stop"]
    assert state["phase"] == "rolled_back" and host.current["Image"] == r.ORIGINAL


def test_pre_stop_failure_delegates_network_recovery_without_original_restart(
    transaction, monkeypatch
):
    folder, state, host = transaction
    host.network_host.behavior["connect-llm-proxy2"] = "before"
    calls = []
    original = r.rollback

    def rollback(*args):
        saved = r.load_state(folder)
        assert saved["phase"] == "deployment_failed" and saved["stop_intent"] is False
        calls.append("rollback")
        return original(*args)

    monkeypatch.setattr(r, "rollback", rollback)
    with pytest.raises(r.network_transaction.TransactionError):
        r.deploy(folder, state, host, fixtures.approval())
    assert calls == ["rollback"] and state["phase"] == "pre_stop_failed_network_recovered"
    assert not any(
        call[0] in {"stop", "up", "recover-original", "reload-nginx"} for call in host.calls
    )


def test_snapshot_uses_original_peak_check_before_archive_or_candidate_creation(
    transaction, monkeypatch
):
    folder, state, host = transaction
    checks = []

    def size(args):
        assert args[:2] == ["du", "-sb"] and Path(args[2]) == Path(state["original_data"])
        return "100\tretained"

    def capacity(path, peak=0):
        assert not host.current["State"]["Running"] and r.load_state(folder)["stop_intent"]
        checks.append((path, peak))
        raise r.Refused("Insufficient capacity reserve")

    monkeypatch.setattr(host, "run", size, raising=False)
    monkeypatch.setattr(r, "fresh_snapshot", _REAL_SNAPSHOT)
    monkeypatch.setattr(r, "capacity", capacity)
    with pytest.raises(r.Refused, match="capacity reserve"):
        r.deploy(folder, state, host, fixtures.approval())
    assert checks == [(folder, 200 + r.GIB)]
    assert not (folder / "data.private.tar").exists() and not Path(state["candidate_data"]).exists()
    assert host.current["Id"] == fixtures.ORIGINAL_ID and host.current["State"]["Running"]


@pytest.mark.parametrize("free,accepted", [(64 * r.GIB - 1, False), (64 * r.GIB, True)])
def test_fixed_dispatch_preserves_production_capacity_reserve(
    tmp_path, monkeypatch, free, accepted
):
    monkeypatch.setattr(r, "capacity", _REAL_CAPACITY)
    monkeypatch.setattr(
        r.shutil, "disk_usage", lambda _: SimpleNamespace(total=100 * r.GIB, free=free)
    )
    dependencies = r._ProductionDeployment(tmp_path, {}, NoHost())
    if accepted:
        dependencies.capacity()
    else:
        with pytest.raises(r.Refused, match="capacity reserve"):
            dependencies.capacity()
