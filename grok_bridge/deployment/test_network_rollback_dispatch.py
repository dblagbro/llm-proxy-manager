"""Production recovery dispatch using isolated synthetic runtime fixtures only."""

import copy
from pathlib import Path, PurePosixPath

import network_lab_context as lab
import pytest
import release as r
import test_release as fixtures


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    # Existing fixture also declares the Windows-only fcntl substitute explicitly.
    fixtures.no_real_host_commands.__wrapped__(monkeypatch)


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    return fixtures.transaction.__wrapped__(tmp_path, monkeypatch)


def no_deploy_gate(*_, **__):
    raise AssertionError("Recovery called the deployment release gate")


class NoHost:
    def __getattribute__(self, name):
        raise AssertionError("Refused rollback reached a host action")


@pytest.mark.parametrize("entry", [r.rollback, r._rollback_transaction])
@pytest.mark.parametrize(
    "stage", ["pre-stop", "restart-original", "legacy", "deployed", "missing-runtime"]
)
def test_recovery_is_available_without_deployment_approval(transaction, monkeypatch, entry, stage):
    folder, state, host = transaction
    if stage in {"deployed", "missing-runtime"}:
        r.deploy(folder, state, host, fixtures.approval())
        if stage == "missing-runtime":
            host.current = None
            host.network_host.network_value["members"].pop(fixtures.CANDIDATE_ID)
    elif stage == "pre-stop":
        state.update(network_intent=True, phase="attaching_consumers")
        r.save(folder, state)
        r.network_engine(folder, state, host, mutate=True).attach_consumers()
    else:
        if stage == "legacy":
            for key in (
                "network_contract",
                "network_transaction",
                "network_intent",
                "original_network_projection",
            ):
                state.pop(key)
        state.update(stop_intent=True, phase="quiescing_original")
        host.current["State"]["Running"] = False
    # These would fail deployment, but neither is a prerequisite for recovery.
    for evidence in (
        {},
        {**fixtures.approval(), "source_merged": False, "operator_deployment_approved": False},
    ):
        with pytest.raises(r.Refused):
            r.gate(evidence)
    monkeypatch.setattr(r, "gate", no_deploy_gate)
    before = len(host.calls)
    assert entry(folder, state, host) is None
    assert state["phase"] == ("network_only_rolled_back" if stage == "pre-stop" else "rolled_back")
    assert host.current["Image"] == r.ORIGINAL and host.current["State"]["Running"]
    if stage == "pre-stop":
        assert host.current["Id"] == fixtures.ORIGINAL_ID
        assert not any(
            call[0] in {"stop", "up", "recover-original", "reload-nginx"}
            for call in host.calls[before:]
        )
    if stage in {"restart-original", "legacy"}:
        assert state["rollback_id"] == state["original_id"] == fixtures.ORIGINAL_ID
        assert not any(call[0] == "up" for call in host.calls[before:])
    if stage == "legacy":
        assert not host.network_host.actions


@pytest.mark.parametrize("entry", [r.rollback, r._rollback_transaction])
@pytest.mark.parametrize(
    "kind", ["header", "binding", "nested", "project", "data", "files", "observer", "generation"]
)
def test_lab_recovery_inputs_refuse_before_dependencies_or_io(entry, kind, tmp_path, monkeypatch):
    state = {"phase": "deployment_failed"}
    folder = tmp_path
    if kind == "header":
        state["mode"] = "lab"
    elif kind == "binding":
        state["lab_binding"] = {"context_sha256": "a" * 64}
    elif kind == "nested":
        state["network_transaction"] = {"schema": 2}
    elif kind == "project":
        state["project"] = "codex-grok-nettest-retained"
    elif kind == "data":
        state["original_data"] = str(lab.ROOT / "retained" / "original-data")
    elif kind == "files":
        state["files"] = {"compose": str(lab.ROOT / "retained" / "compose.yml")}
    elif kind == "observer":
        state.update(schema="grok-network-lab-observation-session-v1", execution_admission=False)
    else:
        folder = PurePosixPath(lab.ROOT / "retained")
    built, before = [], copy.deepcopy(state)
    monkeypatch.setattr(r, "gate", no_deploy_gate)
    monkeypatch.setattr(r, "_ProductionDeployment", lambda *_: built.append(True))
    with pytest.raises(r.Refused, match="Lab"):
        entry(folder, state, NoHost())
    assert state == before and built == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [r.rollback, r._rollback_transaction])
@pytest.mark.parametrize(
    "argument", ["evidence", "lab_context", "dependencies", "adapter", "original_image"]
)
def test_rollback_accepts_no_approval_or_dispatch_override(entry, argument, tmp_path, monkeypatch):
    monkeypatch.setattr(r, "gate", no_deploy_gate)
    with pytest.raises(TypeError):
        entry(tmp_path, {}, NoHost(), **{argument: object()})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [r.rollback, r._rollback_transaction])
def test_cold_pre_stop_ambiguous_attachment_recovers_without_original_actions(
    transaction, monkeypatch, entry
):
    folder, state, host = transaction
    state.update(network_intent=True, phase="attaching_consumers")
    r.save(folder, state)
    host.network_host.behavior["connect-nginx"] = "crash"
    with pytest.raises(fixtures.network_fixture.Crash):
        r.network_engine(folder, state, host, mutate=True).attach_consumers()
    retained = r.load_state(folder)
    assert retained["network_transaction"]["consumers"]["nginx"]["phase"] == "connecting"
    host.network_host.behavior.clear()
    monkeypatch.setattr(r, "gate", no_deploy_gate)
    before = len(host.calls)
    entry(folder, retained, host)
    assert retained["phase"] == "network_only_rolled_back"
    assert host.current["Id"] == fixtures.ORIGINAL_ID and host.current["State"]["Running"]
    assert not any(
        call[0] in {"stop", "up", "recover-original", "reload-nginx"}
        for call in host.calls[before:]
    )
    assert host.network_host.network_value["members"] == {}


@pytest.mark.parametrize("remove_candidate", [False, True])
def test_ambiguous_candidate_binding_is_durable_before_network_audit_and_stop(
    transaction, monkeypatch, remove_candidate
):
    folder, state, host = transaction
    r.deploy(folder, state, host, fixtures.approval())
    state["network_transaction"]["bridge_id"] = None
    if remove_candidate:
        state.pop("candidate_id")
    r.save(folder, state)
    retained = r.load_state(folder)
    events = []
    original_audit, original_stop, original_reload = r.audit_network, host.stop, host.reload_nginx

    def audit(*args):
        disk = r.load_state(folder)
        assert (
            disk["candidate_id"]
            == disk["network_transaction"]["bridge_id"]
            == fixtures.CANDIDATE_ID
        )
        events.append("audit-bound-candidate")
        return original_audit(*args)

    def stop(identity, allow_unclean=False):
        assert identity == fixtures.CANDIDATE_ID and events
        events.append("stop-candidate")
        return original_stop(identity, allow_unclean)

    def reload(nginx):
        disk = r.load_state(folder)
        assert disk["phase"] == "original_runtime_recovered"
        assert disk["rollback_id"] == host.current["Id"] == fixtures.ROLLBACK_ID
        assert host.current["Image"] == r.ORIGINAL and host.current["State"]["Running"]
        events.append("reload-recovered-original")
        return original_reload(nginx)

    monkeypatch.setattr(r, "gate", no_deploy_gate)
    monkeypatch.setattr(r, "audit_network", audit)
    monkeypatch.setattr(host, "stop", stop)
    monkeypatch.setattr(host, "reload_nginx", reload)
    r.rollback(folder, retained, host)
    assert (
        events.index("audit-bound-candidate")
        < events.index("stop-candidate")
        < events.index("reload-recovered-original")
    )
    assert retained["phase"] == "rolled_back" and host.network_host.network_value["members"] == {}


def test_reload_failure_retains_recovered_identity_for_new_controller(transaction, monkeypatch):
    folder, state, host = transaction
    r.deploy(folder, state, host, fixtures.approval())
    monkeypatch.setattr(r, "gate", no_deploy_gate)

    def refused_reload(*_):
        saved = r.load_state(folder)
        assert saved["phase"] == "original_runtime_recovered"
        assert saved["rollback_id"] == host.current["Id"] == fixtures.ROLLBACK_ID
        raise r.Refused("synthetic reload refusal")

    monkeypatch.setattr(host, "reload_nginx", refused_reload)
    with pytest.raises(r.Refused, match="reload refusal"):
        r._rollback_transaction(folder, state, host)
    retained = r.load_state(folder)
    assert retained["phase"] == "original_runtime_recovered"
    assert set(host.network_host.network_value["members"].values()) == {"nginx", "llm-proxy2"}
    monkeypatch.setattr(host, "reload_nginx", lambda *_: None)
    r.rollback(folder, retained, host)
    assert retained["phase"] == "rolled_back"


def test_configuration_conflict_is_saved_and_refused_after_original_recovery_without_detach(
    transaction, monkeypatch
):
    folder, state, host = transaction
    r.deploy(folder, state, host, fixtures.approval())
    path = Path(state["files"]["compose"])
    path.write_bytes(b"concurrent owner edit retained")
    saves = []
    original_save = r.save

    def save(*args, **kwargs):
        original_save(*args, **kwargs)
        saves.append(r.load_state(folder)["phase"])

    def forbidden(*_, **__):
        raise AssertionError("Conflicted recovery reloaded or detached consumers")

    monkeypatch.setattr(r, "gate", no_deploy_gate)
    monkeypatch.setattr(r, "save", save)
    monkeypatch.setattr(host, "reload_nginx", forbidden)
    monkeypatch.setattr(host.network_host, "disconnect", forbidden)
    with pytest.raises(r.Refused, match="configuration conflict"):
        r._rollback_transaction(folder, state, host)
    retained = r.load_state(folder)
    assert path.read_bytes() == b"concurrent owner edit retained"
    assert retained["configuration_conflicts"] == [str(path)] and retained["deployed"] is False
    assert saves[-2:] == [
        "original_runtime_recovered",
        "original_runtime_recovered_config_conflict",
    ]
    assert host.current["Image"] == r.ORIGINAL and host.current["State"]["Running"]
    assert host.current["Mounts"][0]["Source"] == state["original_data"]
    assert set(host.network_host.network_value["members"].values()) == {"nginx", "llm-proxy2"}


@pytest.mark.parametrize("contract", [None, 2, True, "1"])
def test_malformed_network_journal_cannot_turn_into_legacy_recovery(
    transaction, monkeypatch, contract
):
    folder, state, host = transaction
    if contract is None:
        state.pop("network_contract")
    else:
        state["network_contract"] = contract
    monkeypatch.setattr(r, "gate", no_deploy_gate)
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused, match="network preparation"):
        r._rollback_transaction(folder, state, host)
    assert state == before and not host.calls and not host.network_host.actions
