"""Supplied in-memory histories only; no daemon, filesystem or execution evidence."""

import builtins
import copy
import hashlib
import json
import os
import socket
import subprocess
from dataclasses import replace
from pathlib import Path

import network_lab_auxiliary as recipes
import network_lab_auxiliary_state as auxiliary
import network_lab_context as lab
import network_policy as policy
import pytest
from test_network_lab_context import record


def context():
    return lab.from_record(record())


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def initial(ctx=None):
    ctx = ctx or context()
    return auxiliary.initial_state(ctx, lab_binding=ctx.journal_binding(), transaction_id="9" * 32)


def validate(state, previous=None, ctx=None):
    ctx = ctx or context()
    return auxiliary.validate_state(
        ctx, lab_binding=ctx.journal_binding(), state=state, previous=previous
    )


def append(state, resource, phase, values):
    result = copy.deepcopy(state)
    result["predecessor_sha256"] = digest(state)
    result["events"].append({"resource": resource, "phase": phase, "record": copy.deepcopy(values)})
    return result


def runtime(status):
    return {
        "status": status,
        "running": status == "running",
        "exit_code": 0,
        "restart_count": 0,
        "started_at": (auxiliary.ZERO_TIME if status == "created" else "2026-11-15T01:00:00.123Z"),
    }


def creation(state, role, operation, identifier):
    plan = state["resources"][role]
    return {
        "operation_id": operation * 32,
        "returned_id": identifier * 64,
        "inspected_id": identifier * 64,
        "name": plan["name"],
        "image": plan["image"],
        "configuration_sha256": "a" * 64,
        "mounts_sha256": "b" * 64,
        "networks_sha256": "c" * 64,
        "inspection_sha256": "d" * 64,
        "default_network_id": "3" * 64 if role == "probe_container" else None,
        "runtime": runtime("created"),
    }


def probe_report(operation, status):
    return {
        "operation_id": operation * 32,
        "id": "2" * 64,
        "configuration_sha256": "a" * 64,
        "mounts_sha256": "b" * 64,
        "networks_sha256": "e" * 64,
        "inspection_sha256": "f" * 64,
        "default_network_id": "3" * 64,
        "runtime": runtime(status),
    }


def history():
    states = [initial()]
    plans = states[0]["resources"]

    def add(role, phase, values):
        states.append(append(states[-1], role, phase, values))

    add(
        "original_image",
        "metadata_recorded",
        {
            "image": plans["original_image"]["image"],
            "inspection_sha256": "1" * 64,
        },
    )
    add(
        "default_network",
        "borrowed_recorded",
        {
            "name": plans["default_network"]["name"],
            "id": "3" * 64,
            "static_configuration_sha256": "4" * 64,
            "inspection_sha256": "5" * 64,
        },
    )
    add(
        "reference_volume",
        "create_intent",
        {
            "operation_id": "a" * 32,
            "absence_observation_sha256": "6" * 64,
        },
    )
    add(
        "reference_volume",
        "creation_reported",
        {
            "operation_id": "a" * 32,
            "returned_name": plans["reference_volume"]["name"],
            "inspection_sha256": "7" * 64,
        },
    )
    add("reference_volume", "seed_intent", {"operation_id": "b" * 32})
    add(
        "reference_volume",
        "seed_recorded",
        {
            "operation_id": "b" * 32,
            "volume_name": plans["reference_volume"]["name"],
            "manifest": plans["reference_volume"]["seed_manifest"],
            "inspection_sha256": "8" * 64,
        },
    )
    add(
        "reference_container",
        "create_intent",
        {
            "operation_id": "c" * 32,
            "absence_observation_sha256": "9" * 64,
        },
    )
    add(
        "reference_container",
        "created_recorded",
        creation(states[-1], "reference_container", "c", "1"),
    )
    add(
        "probe_container",
        "create_intent",
        {
            "operation_id": "d" * 32,
            "absence_observation_sha256": "a" * 64,
        },
    )
    add("probe_container", "created_recorded", creation(states[-1], "probe_container", "d", "2"))
    add("probe_container", "start_intent", {"operation_id": "e" * 32})
    add("probe_container", "running_recorded", probe_report("e", "running"))
    add("probe_container", "stop_intent", {"operation_id": "f" * 32})
    add("probe_container", "stopped_recorded", probe_report("f", "exited"))
    return states


def diagnose(state, resource, code, *, phase="unresolved", observed_id=None):
    return append(
        state,
        resource,
        phase,
        {
            "code": code,
            "evidence_sha256": "0" * 64,
            "observed_id": observed_id,
        },
    )


def test_initial_state_binds_exact_recipes_separate_resources_and_no_authority():
    ctx = context()
    state = initial(ctx)
    inputs = recipes.auxiliary_fixture_inputs(ctx, lab_binding=ctx.journal_binding())
    assert state["materials"] == {
        path: hashlib.sha256(data).hexdigest() for path, data in inputs.items()
    }
    assert state["lab_context"] == ctx.journal_binding()
    assert state["scope"] == "supplied_auxiliary_intents_and_receipts_only"
    assert state["execution_admission"] is False and state["events"] == []
    assert state["predecessor_sha256"] is None
    resources = state["resources"]
    names = {value["name"] for value in resources.values() if "name" in value}
    assert len(names) == 4
    assert {
        resources[role]["name"] for role in ("reference_container", "probe_container")
    }.isdisjoint(ctx.resource_map()[name] for name in policy.MEMBERS)
    assert resources["reference_volume"]["name"] != ctx.project + "-original-data"
    assert resources["default_network"]["classification"] == "borrowed_not_owned"
    assert validate(state) == state


def test_complete_one_step_history_retains_reports_without_promoting_volume_continuity():
    states = history()
    for old, new in zip(states, states[1:]):
        result = validate(new, old)
        assert result == new and result is not new
        assert result["events"][:-1] == old["events"]
        assert result["execution_admission"] is False
        assert result["resources"]["reference_volume"]["continuity"] == "unverified"
        assert result["resources"]["reference_volume"]["identity"] == "no_immutable_docker_id"
    assert states[-1]["events"][7]["record"]["runtime"] == runtime("created")


def test_standalone_retained_validation_is_distinct_from_one_step_predecessor_check():
    final = history()[-1]
    assert validate(json.dumps(final).encode()) == final
    unproven_predecessor = copy.deepcopy(final)
    unproven_predecessor["predecessor_sha256"] = "0" * 64
    assert validate(unproven_predecessor) == unproven_predecessor
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="predecessor"):
        validate(unproven_predecessor, history()[-2])


@pytest.mark.parametrize(
    "change",
    [
        {"vm_uuid": "11111111-2222-3333-4444-555555555555"},
        {"vm_name": "codex-grok-lab-other"},
        {"engine_id": "other-engine"},
        {"source_commit": "f" * 40},
        {
            "images": (
                ("original", "sha256:" + "9" * 64),
                ("candidate", "sha256:" + "8" * 64),
                ("nginx", "sha256:" + "7" * 64),
            )
        },
    ],
)
def test_full_context_drift_refuses_even_when_physical_namespace_is_unchanged(change):
    ctx = context()
    other = replace(ctx, **change)
    assert ctx.project == other.project and ctx.generation_root == other.generation_root
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(history()[-1], ctx=other)


@pytest.mark.parametrize("transaction", [None, True, 1, "", "0" * 32, "A" * 32, "a" * 31, "a" * 33])
def test_invalid_transaction_refuses(transaction):
    ctx = context()
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        auxiliary.initial_state(ctx, lab_binding=ctx.journal_binding(), transaction_id=transaction)


@pytest.mark.parametrize("binding", [None, {}, False, "lab"])
def test_no_implicit_binding_is_minted(binding):
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        auxiliary.initial_state(context(), lab_binding=binding, transaction_id="a" * 32)


def test_invalid_context_and_tampered_frozen_context_refuse():
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        auxiliary.initial_state(
            context().to_record(), lab_binding=context().journal_binding(), transaction_id="a" * 32
        )
    ctx = context()
    object.__setattr__(ctx, "source_commit", True)
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        auxiliary.initial_state(
            ctx, lab_binding=context().journal_binding(), transaction_id="a" * 32
        )


@pytest.mark.parametrize(
    "location,key,value",
    [
        ((), "execution_admission", True),
        ((), "execution_admission", 0),
        ((), "mode", "production"),
        ((), "ownership_verified", True),
        (("resources", "reference_volume"), "volume_id", "a" * 64),
        (("resources", "reference_volume"), "fresh", True),
        (("resources", "reference_volume"), "continuity", "verified"),
        (("resources", "default_network"), "classification", "owned"),
        (("resources", "probe_container"), "name", "unrelated"),
    ],
)
def test_unknown_fields_and_authority_or_recipe_changes_refuse(location, key, value):
    state = initial()
    target = state
    for part in location:
        target = target[part]
    target[key] = value
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(state)


@pytest.mark.parametrize(
    "field", ["static_configuration_sha256", "inspection_sha256", "name", "id"]
)
def test_predecessor_bound_borrowed_network_receipt_cannot_be_rebased(field):
    old, new = history()[2:4]
    new["events"][1]["record"][field] = "9" * 64
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(new, old)


@pytest.mark.parametrize("field", ["inspection_sha256", "configuration_sha256", "mounts_sha256"])
def test_predecessor_bound_container_receipt_cannot_be_rebased(field):
    old, new = history()[8:10]
    new["events"][7]["record"][field] = "0" * 64
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="history"):
        validate(new, old)


def test_matched_id_rewrite_is_not_authorized_by_a_valid_predecessor_digest():
    old, new = history()[8:10]
    new["events"][7]["record"].update(returned_id="8" * 64, inspected_id="8" * 64)
    assert new["predecessor_sha256"] == digest(old)
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="history"):
        validate(new, old)


def test_transaction_and_material_headers_cannot_be_rebased():
    old, new = history()[:2]
    new["transaction_id"] = "7" * 32
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="preparation"):
        validate(new, old)
    state = initial()
    state["materials"][recipes.PROBE_SCRIPT] = "f" * 64
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="preparation"):
        validate(state)


def test_reset_or_multi_step_append_cannot_hide_intermediate_intent_persistence_requirement():
    states = history()
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(initial(), states[4])
    skipped_save = copy.deepcopy(states[4])
    skipped_save["predecessor_sha256"] = digest(states[2])
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="append-only"):
        validate(skipped_save, states[2])


@pytest.mark.parametrize("identifier", ["1" * 64, "3" * 64, "2" * 12, "sha256:" + "2" * 64, True])
def test_container_ids_must_be_full_and_distinct_from_represented_resources(identifier):
    state = history()[10]
    state["events"][-1]["record"].update(returned_id=identifier, inspected_id=identifier)
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(state)


def test_create_response_and_inspection_id_must_match():
    state = history()[8]
    state["events"][-1]["record"]["inspected_id"] = "8" * 64
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="identity"):
        validate(state)


@pytest.mark.parametrize(
    "kind", ["skip", "repeat", "unknown", "operation", "receipt", "image", "network"]
)
def test_missing_intent_dependency_or_reused_phase_refuses(kind):
    state = history()[10]
    if kind == "skip":
        del state["events"][-2]
    elif kind == "repeat":
        state["events"].append(copy.deepcopy(state["events"][-1]))
    elif kind == "unknown":
        state["events"][-1]["phase"] = "adopted"
    elif kind == "operation":
        state["events"][-2]["record"]["operation_id"] = "a" * 32
    elif kind == "receipt":
        state["events"][-1]["record"]["operation_id"] = "b" * 32
    elif kind == "image":
        del state["events"][0]
    else:
        del state["events"][1]
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(state)


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../reference-generation.txt"),
        ("path", "/data/other"),
        ("type", "symlink"),
        ("sha256", "0" * 64),
        ("size", True),
    ],
)
def test_seed_report_matches_exact_installed_relative_manifest(field, value):
    state = history()[6]
    state["events"][-1]["record"]["manifest"][0][field] = value
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="seed"):
        validate(state)


def test_name_reuse_success_cannot_be_upgraded_to_fresh_volume_identity():
    state = history()[4]
    assert validate(state)["resources"]["reference_volume"]["continuity"] == "unverified"
    state["events"][-1]["record"]["freshly_created"] = True
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="fields"):
        validate(state)


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "exited", "exit_code": 78, "started_at": "2026-11-15T01:00:00Z"},
        {"status": "running", "running": True, "started_at": "2026-11-15T01:00:00Z"},
        {"started_at": "2026-11-15T01:00:00Z"},
        {"restart_count": 1},
    ],
)
def test_reference_never_started_invariant_cannot_be_normalized_as_created(updates):
    state = history()[8]
    state["events"][-1]["record"]["runtime"].update(updates)
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(state)


def test_reference_violation_can_be_retained_even_before_a_normal_creation_receipt():
    pending = history()[7]
    failure = diagnose(
        pending, "reference_container", "reference_started", phase="violation", observed_id="1" * 64
    )
    assert validate(failure, pending) == failure
    assert failure["events"][:-1] == pending["events"]
    assert failure["execution_admission"] is False
    followup = diagnose(failure, "reference_container", "observer_failed")
    assert validate(followup, failure) == followup
    reported_success = history()[8]["events"][-1]
    attempt = append(failure, "reference_container", "created_recorded", reported_success["record"])
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="diagnostic"):
        validate(attempt, failure)


@pytest.mark.parametrize(
    "state_index,role,code",
    [
        (3, "reference_volume", "creation_ambiguous"),
        (5, "reference_volume", "seed_ambiguous"),
        (7, "reference_container", "creation_ambiguous"),
        (11, "probe_container", "start_ambiguous"),
        (13, "probe_container", "stop_ambiguous"),
        (6, "reference_volume", "volume_continuity_lost"),
    ],
)
def test_ambiguity_retains_intent_and_cannot_silently_clear_retry_or_adopt(state_index, role, code):
    pending = history()[state_index]
    failure = diagnose(pending, role, code)
    assert validate(failure, pending) == failure
    extra = diagnose(failure, role, "observer_failed")
    assert validate(extra, failure) == extra
    next_valid = history()[state_index + 1]["events"][-1]
    attempt = append(failure, next_valid["resource"], next_valid["phase"], next_valid["record"])
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="diagnostic"):
        validate(attempt, failure)


def test_replacement_diagnostic_preserves_old_id_and_does_not_adopt_observed_id():
    retained = history()[8]
    failed = diagnose(
        retained,
        "reference_container",
        "resource_replaced",
        phase="violation",
        observed_id="8" * 64,
    )
    assert validate(failed, retained) == failed
    assert failed["events"][7]["record"]["returned_id"] == "1" * 64


@pytest.mark.parametrize(
    "role,code,phase,identifier",
    [
        ("reference_volume", "creation_ambiguous", "violation", None),
        ("reference_volume", "observer_failed", "unresolved", "8" * 64),
        ("reference_volume", "private exception contents", "unresolved", None),
        ("probe_container", "start_ambiguous", "unresolved", None),
    ],
)
def test_diagnostic_codes_types_and_pending_operation_are_bounded(role, code, phase, identifier):
    failed = diagnose(history()[3], role, code, phase=phase, observed_id=identifier)
    with pytest.raises(auxiliary.LabAuxiliaryStateError) as caught:
        validate(failed)
    assert "private exception contents" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("running", 1),
        ("restart_count", False),
        ("exit_code", 0.0),
        ("started_at", "2026-99-99T01:00:00Z"),
        ("started_at", auxiliary.ZERO_TIME),
    ],
)
def test_probe_runtime_reports_require_exact_recursive_types_and_coherent_state(field, value):
    state = history()[12]
    state["events"][-1]["record"]["runtime"][field] = value
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(state)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "8" * 64),
        ("configuration_sha256", "8" * 64),
        ("mounts_sha256", "8" * 64),
        ("default_network_id", "8" * 64),
    ],
)
def test_probe_start_stop_cannot_change_retained_identity_or_preparation(field, value):
    state = history()[-1]
    state["events"][-1]["record"][field] = value
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(state)


def test_probe_start_time_cannot_change_between_running_and_stop_receipts():
    state = history()[-1]
    state["events"][-1]["record"]["runtime"]["started_at"] = "2026-11-15T02:00:00Z"
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="start identity"):
        validate(state)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":1,"schema":2}',
        b'{"events":[{"phase":"x","phase":"y"}]}',
        b'{"events":NaN}',
        b'{"events":Infinity}',
        b'{"events":-Infinity}',
        b"\xff",
        b"{",
        b"null",
        b"[]",
        b" " * (auxiliary.MAX_RECORD_BYTES + 1),
    ],
)
def test_malformed_duplicate_nonfinite_or_oversized_serialized_input_refuses(raw):
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(raw)


def test_structure_cycle_string_and_event_bounds_refuse():
    cycle = {}
    cycle["cycle"] = cycle
    long_text = {"private": "private-text" * 1000}
    deep = {}
    for _ in range(14):
        deep = {"nested": deep}
    state = initial()
    for _ in range(auxiliary.MAX_EVENTS + 1):
        state = diagnose(state, "original_image", "observer_failed")
    for malformed in (cycle, long_text, deep, state):
        with pytest.raises(auxiliary.LabAuxiliaryStateError) as caught:
            validate(malformed)
        assert "private-text" not in str(caught.value)


def test_mutable_inputs_and_returned_results_are_detached():
    ctx = context()
    binding = ctx.journal_binding()
    state = auxiliary.initial_state(ctx, lab_binding=binding, transaction_id="a" * 32)
    binding["context"]["vm_name"] = "changed"
    assert state["lab_context"] == ctx.journal_binding()
    previous, current = history()[-2:]
    before = copy.deepcopy((previous, current))
    result = validate(current, previous)
    result["events"][0]["record"]["inspection_sha256"] = "0" * 64
    assert (previous, current) == before
    assert result != current


def test_state_mutation_after_snapshot_is_detached_but_binding_mutation_refuses(monkeypatch):
    ctx = context()
    supplied = initial(ctx)
    expected = copy.deepcopy(supplied)
    original = recipes.auxiliary_fixture_inputs

    def mutate_state(*args, **kwargs):
        supplied["execution_admission"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(recipes, "auxiliary_fixture_inputs", mutate_state)
    assert validate(supplied, ctx=ctx) == expected
    binding = ctx.journal_binding()

    def mutate_binding(*args, **kwargs):
        binding["context"]["source_commit"] = "f" * 40
        return original(*args, **kwargs)

    monkeypatch.setattr(recipes, "auxiliary_fixture_inputs", mutate_binding)
    with pytest.raises(auxiliary.LabAuxiliaryStateError, match="binding changed"):
        auxiliary.initial_state(ctx, lab_binding=binding, transaction_id="a" * 32)


def test_validation_failure_preserves_caller_record_and_predecessor():
    previous, supplied = history()[8:10]
    supplied["predecessor_sha256"] = "0" * 64
    before = copy.deepcopy((previous, supplied))
    with pytest.raises(auxiliary.LabAuxiliaryStateError):
        validate(supplied, previous)
    assert (previous, supplied) == before


def test_generator_and_validator_have_no_io_or_runtime_permission_path(monkeypatch):
    retained = history()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unexpected host I/O")

    for target, name in (
        (builtins, "open"),
        (os, "open"),
        (os, "stat"),
        (os, "urandom"),
        (Path, "read_bytes"),
        (Path, "write_bytes"),
        (Path, "resolve"),
        (socket, "socket"),
        (subprocess, "run"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    assert initial()["execution_admission"] is False
    assert validate(retained[-1], retained[-2])["execution_admission"] is False
    ctx = context()
    with pytest.raises(TypeError):
        auxiliary.validate_state(
            ctx, lab_binding=ctx.journal_binding(), state=retained[-1], execution_admission=True
        )
