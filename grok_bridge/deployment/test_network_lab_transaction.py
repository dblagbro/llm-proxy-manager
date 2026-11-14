"""Lab journal/adapter consistency with simulated commands; no runtime execution."""

import copy
import json
from dataclasses import replace

import network_docker as adapter
import network_lab_context as lab
import network_policy as policy
import network_transaction as tx
import pytest
from test_network_docker import FakeRunner, sha
from test_network_lab_context import record
from test_network_transaction import Crash, FakeHost


def context():
    return lab.from_record(record())


def bound_host(ctx, runner=None, *, enabled=True, bridge_id=None):
    runner = runner or FakeRunner(ctx.resource_map(), ctx.project)
    host = adapter.DockerHost(
        runner,
        enable_mutations=enabled,
        transaction_id="a" * 32,
        allowed_consumers={role: sha(role) for role in policy.CONSUMERS},
        resource_map=ctx.resource_map(),
        compose_project=ctx.project,
        allowed_bridge_id=bridge_id,
    )
    return runner, host


def begin(ctx=None, *, enabled=True, save=None):
    ctx = ctx or context()
    runner, host = bound_host(ctx, enabled=enabled)
    journal = []
    save = save or (lambda value: journal.append(copy.deepcopy(value)))
    engine = tx.NetworkTransaction.begin(host, save, "a" * 32, lab_context=ctx)
    return ctx, runner, host, journal, engine


def mutations(runner):
    return [
        args
        for args, _ in runner.calls
        if args[1:3] in (["network", "create"], ["network", "connect"], ["network", "disconnect"])
    ]


@pytest.mark.parametrize("scenario", sorted(lab.SCENARIOS))
def test_context_survives_all_checkpoints_and_cold_rollback(scenario):
    ctx, runner, _, journal, engine = begin(replace(context(), scenario=scenario))
    before = copy.deepcopy(runner.containers)
    engine.attach_consumers()
    _, cold = bound_host(ctx, runner)
    resumed = tx.NetworkTransaction(
        cold, lambda s: journal.append(copy.deepcopy(s)), journal[-1], lab_context=ctx
    )
    resumed.rollback()
    assert resumed.state["phase"] == "rolled_back"
    assert runner.network["Containers"] == {}
    assert runner.containers == before
    assert all(s["schema"] == 2 and s["mode"] == "lab" for s in journal)
    for state in journal:
        ctx.verify_journal_binding(state["lab_context"])
    wire = mutations(runner)
    assert len([a for a in wire if a[2] == "create"]) == 1
    assert len([a for a in wire if a[2] == "connect"]) == 2
    assert len([a for a in wire if a[2] == "disconnect"]) == 2
    assert all("rm" not in a and "prune" not in a and "exec" not in a for a, _ in runner.calls)
    assert "synthetic-secret" not in json.dumps(journal)


def test_schema_modes_are_disjoint_before_any_runner_or_save_call():
    ctx, runner, host, journal, _ = begin()
    lab_state = journal[-1]
    production = FakeHost()
    prod_engine = tx.NetworkTransaction.begin(production, production.save, "b" * 32)
    prior_calls = len(runner.calls)
    saves = []
    for state, supplied in ((lab_state, None), (prod_engine.state, ctx)):
        with pytest.raises(tx.TransactionError):
            tx.NetworkTransaction(host, saves.append, state, lab_context=supplied)
    assert len(runner.calls) == prior_calls and saves == []
    assert set(prod_engine.state) == {
        "schema",
        "transaction_id",
        "phase",
        "network",
        "consumers",
        "bridge_id",
    }
    assert prod_engine.state["schema"] == 1
    tx.NetworkTransaction(production, production.save, prod_engine.state).rollback()


@pytest.mark.parametrize(
    "field,value",
    [
        ("vm_uuid", "12345678-1234-1234-1234-123456789abd"),
        ("vm_name", "codex-grok-lab-other"),
        ("engine_id", "other-engine"),
        ("source_commit", "c" * 40),
        (
            "images",
            (
                ("original", "sha256:" + "4" * 64),
                ("candidate", "sha256:" + "2" * 64),
                ("nginx", "sha256:" + "3" * 64),
            ),
        ),
        ("scenario", "configuration-conflict"),
        ("context_id", "c" * 32),
    ],
)
def test_changed_full_context_refuses_even_when_names_match(field, value):
    ctx, _, _, journal, _ = begin()
    foreign = replace(ctx, **{field: value})
    runner, host = bound_host(foreign)
    saves = []
    with pytest.raises(tx.TransactionError, match="context"):
        tx.NetworkTransaction(host, saves.append, journal[-1], lab_context=foreign)
    assert runner.calls == [] and saves == []


@pytest.mark.parametrize("field", ["schema", "mode", "lab_context", "digest", "extra"])
def test_tampered_journal_refuses_without_io(field):
    ctx, _, _, journal, _ = begin()
    state = copy.deepcopy(journal[-1])
    if field == "digest":
        state["lab_context"]["context_sha256"] = "f" * 64
    elif field == "lab_context":
        state.pop(field)
    else:
        state[field] = True
    runner, host = bound_host(ctx)
    saves = []
    with pytest.raises(tx.TransactionError):
        tx.NetworkTransaction(host, saves.append, state, lab_context=ctx)
    assert runner.calls == [] and saves == []


@pytest.mark.parametrize("field", ["project", "resources", "transaction_id", "allowed", "enabled"])
def test_bad_initial_adapter_binding_refuses_before_read_or_save(field):
    ctx = context()
    runner, host = bound_host(ctx)
    if field == "project":
        host.project = "docker"
    elif field == "resources":
        host.resources["nginx"] = "nginx"
    elif field == "transaction_id":
        host.transaction_id = "b" * 32
    elif field == "allowed":
        host.allowed = {}
    else:
        host.enabled = 1
    saves = []
    with pytest.raises(tx.TransactionError):
        tx.NetworkTransaction.begin(host, saves.append, "a" * 32, lab_context=ctx)
    assert runner.calls == [] and saves == []


def test_supplied_consumer_ids_must_match_observations_before_first_save():
    ctx = context()
    runner, host = bound_host(ctx)
    host.allowed["nginx"] = sha("replacement")
    saves = []
    with pytest.raises(tx.TransactionError):
        tx.NetworkTransaction.begin(host, saves.append, "a" * 32, lab_context=ctx)
    assert saves == [] and mutations(runner) == []


def test_initial_bridge_binding_refuses_before_any_read_or_save():
    ctx = context()
    runner, host = bound_host(ctx, bridge_id=sha(policy.BRIDGE))
    saves = []
    with pytest.raises(tx.TransactionError, match="bridge"):
        tx.NetworkTransaction.begin(host, saves.append, "a" * 32, lab_context=ctx)
    assert runner.calls == [] and saves == []


@pytest.mark.parametrize("bad", [{}, "lab", True, object()])
def test_invalid_context_never_invokes_adapter(bad):
    runner, host = bound_host(context())
    saves = []
    with pytest.raises(tx.TransactionError):
        tx.NetworkTransaction.begin(host, saves.append, "a" * 32, lab_context=bad)
    assert runner.calls == [] and saves == []


def test_lab_requires_exact_context_and_exact_adapter_types():
    class Foreign(lab.LabContext):
        def to_record(self):
            raise AssertionError("Subclass method must not execute")

    ctx = context()
    wrong = Foreign(**{name: getattr(ctx, name) for name in ctx.__dataclass_fields__})
    runner, host = bound_host(ctx)
    saves = []
    with pytest.raises(tx.TransactionError):
        tx.NetworkTransaction.begin(host, saves.append, "a" * 32, lab_context=wrong)
    with pytest.raises(tx.TransactionError):
        tx.NetworkTransaction.begin(FakeHost(), saves.append, "a" * 32, lab_context=ctx)
    assert runner.calls == [] and saves == []


@pytest.mark.parametrize("field", ["project", "resources", "transaction_id", "allowed", "enabled"])
def test_active_adapter_drift_is_rejected_before_next_host_call(field):
    _, runner, host, journal, engine = begin()
    if field == "resources":
        host.resources["nginx"] = "nginx"
    elif field == "allowed":
        host.allowed["nginx"] = sha("replacement")
    else:
        setattr(
            host, field, {"project": "docker", "transaction_id": "b" * 32, "enabled": False}[field]
        )
    calls, saved = len(runner.calls), len(journal)
    with pytest.raises(tx.TransactionError):
        engine.attach_consumers()
    assert len(runner.calls) == calls and len(journal) == saved


def test_save_callback_namespace_drift_stops_before_mutation():
    ctx = context()
    runner, host = bound_host(ctx)
    journal = []

    def save(state):
        journal.append(copy.deepcopy(state))
        if state["network"]["phase"] == "creating":
            host.resources["nginx"] = "nginx"

    engine = tx.NetworkTransaction.begin(host, save, "a" * 32, lab_context=ctx)
    with pytest.raises(tx.TransactionError, match="namespace"):
        engine.attach_consumers()
    assert journal[-1]["network"]["phase"] == "creating"
    assert mutations(runner) == []


@pytest.mark.parametrize("failure", ["create-after", "connect-after", "disconnect-after"])
def test_ambiguous_transport_effects_reconcile_with_binding_retained(failure):
    ctx, runner, _, journal, engine = begin()
    runner.mutation_error = failure
    engine.attach_consumers()
    engine.rollback()
    assert engine.state["phase"] == "rolled_back" and runner.network["Containers"] == {}
    assert all(s["lab_context"] == ctx.journal_binding() for s in journal)
    assert len(mutations(runner)) == 5


@pytest.mark.parametrize("cut", ["owned", "attached"])
def test_cold_resume_after_effect_before_receipt_preserves_binding(cut):
    ctx = context()
    runner, host = bound_host(ctx)
    journal = []
    crashed = False

    def save(state):
        nonlocal crashed
        should_crash = (
            state["network"]["phase"] == "owned"
            if cut == "owned"
            else state["consumers"]["nginx"]["phase"] == "attached"
        )
        if should_crash and not crashed:
            crashed = True
            raise Crash()
        journal.append(copy.deepcopy(state))

    engine = tx.NetworkTransaction.begin(host, save, "a" * 32, lab_context=ctx)
    with pytest.raises(Crash):
        engine.attach_consumers()
    assert runner.network is not None
    if cut == "attached":
        assert sha("nginx") in runner.network["Containers"]
        assert journal[-1]["consumers"]["nginx"]["phase"] == "connecting"
    _, cold = bound_host(ctx, runner)
    resumed = tx.NetworkTransaction(cold, save, journal[-1], lab_context=ctx)
    resumed.attach_consumers()
    resumed.rollback()
    assert resumed.state["phase"] == "rolled_back"
    assert len(mutations(runner)) == 5
    assert all(s["lab_context"] == ctx.journal_binding() for s in journal)


def test_readonly_adapter_is_never_escalated():
    _, runner, host, _, engine = begin(enabled=False)
    with pytest.raises(tx.TransactionError):
        engine.attach_consumers()
    assert host.enabled is False and mutations(runner) == []


def test_readonly_capability_flip_is_rejected_before_runner():
    _, runner, host, _, engine = begin(enabled=False)
    host.enabled = True
    calls = len(runner.calls)
    with pytest.raises(tx.TransactionError, match="capability"):
        engine.attach_consumers()
    assert len(runner.calls) == calls


def test_bridge_identity_is_saved_before_adapter_rebinding():
    ctx, runner, host, journal, engine = begin()
    engine.attach_consumers()
    engine.record_bridge_id(sha(policy.BRIDGE))
    assert journal[-1]["bridge_id"] == sha(policy.BRIDGE) and host.bridge_id is None
    count = len(runner.calls)
    with pytest.raises(tx.TransactionError, match="bridge"):
        engine.rollback()
    assert len(runner.calls) == count
    _, rebound = bound_host(ctx, runner, bridge_id=sha(policy.BRIDGE))
    resumed = tx.NetworkTransaction(
        rebound, lambda s: journal.append(copy.deepcopy(s)), journal[-1], lab_context=ctx
    )
    resumed.rollback()
    assert resumed.state["phase"] == "rolled_back"


def test_journal_return_values_are_detached_and_cannot_change_context():
    ctx, _, _, _, engine = begin()
    state = engine.state
    state["lab_context"]["context"]["engine_id"] = "different"
    assert engine.state["lab_context"] == ctx.journal_binding()
    with pytest.raises(tx.TransactionError):
        engine._checkpoint(lambda s: s["lab_context"]["context"].update(engine_id="different"))


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("operation", ["attach", "rollback", "checkpoint"])
def test_lost_context_never_falls_back_to_production(attached, operation):
    _, runner, _, journal, engine = begin()
    if attached:
        engine.attach_consumers()
    calls, saved = len(runner.calls), len(journal)
    callbacks = []
    engine._lab_context = None
    action = {
        "attach": engine.attach_consumers,
        "rollback": engine.rollback,
        "checkpoint": lambda: engine._checkpoint(lambda state: callbacks.append(state)),
    }[operation]
    with pytest.raises(tx.TransactionError, match="context"):
        action()
    assert len(runner.calls) == calls and len(journal) == saved and callbacks == []
