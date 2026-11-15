"""Pure reported-history tests, not Docker, persistence or runtime acceptance."""

import builtins
import copy
import json
import os
import socket
import subprocess
import time
from dataclasses import replace

import network_lab_auxiliary_state as auxiliary
import network_lab_seed_state as states
import pytest
from test_network_lab_auxiliary_state import context


def initial(ctx=None):
    ctx = ctx or context()
    return states.initial_state(ctx, lab_binding=ctx.journal_binding(), transaction_id="9" * 32)


def continuity(role="working"):
    return {
        "scope": "supplied_consistency_only",
        "engine_id": context().engine_id,
        "guest_boot_sha256": "a" * 64,
        "controller_process_sha256": "b" * 64,
        "daemon_process_sha256": "c" * 64,
        "socket_peer_sha256": "d" * 64,
        "operation_lock_sha256": "e" * 64,
        "backing_directory_sha256": ("f" if role == "working" else "0") * 64,
        "backing_mount_sha256": "1" * 64,
    }


def runtime(created):
    return {
        "status": "created" if created else "exited",
        "running": False,
        "paused": False,
        "restarting": False,
        "dead": False,
        "oom_killed": False,
        "pid": 0,
        "restart_count": 0,
        "exit_code": 0,
        "error": "",
        "started_at": auxiliary.ZERO_TIME if created else "2026-11-15T07:00:00.123456789Z",
        "finished_at": auxiliary.ZERO_TIME if created else "2026-11-15T07:00:01Z",
    }


def events(role="working"):
    plan = initial()["roles"][role]
    identifier = ("2" if role == "working" else "3") * 64
    create = ("4" if role == "working" else "5") * 32
    start = ("6" if role == "working" else "7") * 32
    checksum = "8" * 64
    records = [
        {
            "image": plan["image"],
            "image_inspection_sha256": checksum,
            "volume_name": plan["volume_name"],
            "volume_inspection_sha256": checksum,
        },
        {
            "operation_id": create,
            "helper_name": plan["helper_name"],
            "create_body_sha256": plan["create_body_sha256"],
        },
        {
            "operation_id": create,
            "http_status": 201,
            "returned_id": identifier,
            "inspected_id": identifier,
            "helper_name": plan["helper_name"],
            "image": plan["image"],
            "configuration_sha256": checksum,
            "mounts_sha256": checksum,
            "inspection_sha256": checksum,
            "runtime": runtime(True),
            "warnings": [],
        },
        {"operation_id": start, "target_id": identifier},
        {"operation_id": start, "target_id": identifier, "http_status": 204},
        {
            "id": identifier,
            "start_operation_id": start,
            "helper_name": plan["helper_name"],
            "image": plan["image"],
            "configuration_sha256": checksum,
            "mounts_sha256": checksum,
            "inspection_sha256": checksum,
            "runtime": runtime(False),
        },
        {
            "id": identifier,
            "start_operation_id": start,
            "volume_name": plan["volume_name"],
            "manifest": plan["seed_manifest"],
            "seed_inspection_sha256": checksum,
        },
    ]
    return [
        {
            "role": role,
            "phase": phase,
            "record": {**copy.deepcopy(record), "continuity": continuity(role)},
        }
        for phase, record in zip(states.PHASES[1:], records)
    ]


def append(prior, event):
    result = copy.deepcopy(prior)
    result["events"].append(copy.deepcopy(event))
    result["predecessor_sha256"] = auxiliary._digest(prior)
    return result


def history():
    result = [initial()]
    for event in (*events(), *events("reference")):
        result.append(append(result[-1], event))
    return result


def validate(value, previous=None, ctx=None):
    ctx = ctx or context()
    return states.validate_state(
        ctx, lab_binding=ctx.journal_binding(), state=value, previous=previous
    )


def diagnostic(role="working", code="observer_failed", observed_id=None):
    return {
        "role": role,
        "phase": "diagnostic",
        "record": {"code": code, "evidence_sha256": "a" * 64, "observed_id": observed_id},
    }


def test_both_complete_histories_have_exact_predecessors_and_no_authority():
    chain = history()
    for previous, current in zip(chain, chain[1:]):
        result = validate(current, previous)
        assert result == current and result is not current
        assert result["execution_admission"] is False and result["compatibility_verified"] is False
    assert len(chain[-1]["events"]) == 14
    assert chain[-1]["schema"] != auxiliary.SCHEMA
    assert all(
        plan["volume_identity"] == "no_immutable_docker_id" for plan in chain[-1]["roles"].values()
    )
    assert validate(auxiliary._encoded(chain[-1])) == chain[-1]


def test_fast_exit_needs_no_observed_running_phase_and_no_clock_order_claim():
    current = history()[6]
    terminal = current["events"][-1]["record"]["runtime"]
    terminal["started_at"] = "2099-12-31T23:59:59.999999999Z"
    terminal["finished_at"] = "2000-01-01T00:00:00Z"
    assert not any(event["phase"] == "running_reported" for event in current["events"])
    assert validate(current) == current
    assert current["execution_admission"] is False


@pytest.mark.parametrize("phase", states.PHASES[2:])
def test_normal_phase_cannot_skip_its_immediate_predecessor(phase):
    sequence = events()
    index = states.PHASES.index(phase)
    current = append(history()[index - 2], sequence[index - 1])
    with pytest.raises(states.LabSeedStateError):
        validate(current)


@pytest.mark.parametrize("phase_index", range(1, 8))
def test_normal_phase_cannot_repeat_or_retry(phase_index):
    previous = history()[phase_index]
    with pytest.raises(states.LabSeedStateError):
        validate(append(previous, previous["events"][-1]), previous)


@pytest.mark.parametrize(
    "field,value",
    [
        ("http_status", 200),
        ("http_status", 201.0),
        ("http_status", True),
        ("warnings", ["private warning canary"]),
        ("warnings", None),
        ("returned_id", "2" * 12),
        ("inspected_id", "a" * 64),
        ("helper_name", "unattributable-name"),
        ("operation_id", "a" * 32),
    ],
)
def test_create_report_requires_exact_success_and_id(field, value):
    current = history()[3]
    current["events"][-1]["record"][field] = value
    with pytest.raises(states.LabSeedStateError) as caught:
        validate(current)
    assert "private warning canary" not in str(caught.value)


@pytest.mark.parametrize("status", [304, 200, 202, 204.0, "204", True])
def test_start_response_does_not_accept_already_running_or_coerced_success(status):
    current = history()[5]
    current["events"][-1]["record"]["http_status"] = status
    with pytest.raises(states.LabSeedStateError):
        validate(current)


@pytest.mark.parametrize("index,key", [(4, "target_id"), (5, "target_id"), (6, "id"), (7, "id")])
def test_every_post_create_report_is_bound_to_the_recorded_helper(index, key):
    current = history()[index]
    current["events"][-1]["record"][key] = "a" * 64
    with pytest.raises(states.LabSeedStateError):
        validate(current)


def test_roles_cannot_share_helper_id_or_operation_id():
    for kind in ("helper", "operation"):
        current = history()[10 if kind == "helper" else 9]
        record = current["events"][-1]["record"]
        if kind == "helper":
            record["returned_id"] = record["inspected_id"] = "2" * 64
        else:
            record["operation_id"] = "4" * 32
        with pytest.raises(states.LabSeedStateError):
            validate(current)


@pytest.mark.parametrize("field", sorted(states.CONTINUITY_HASHES))
def test_missing_or_changed_continuity_component_refuses(field):
    missing = history()[1]
    del missing["events"][0]["record"]["continuity"][field]
    changed = history()[3]
    changed["events"][-1]["record"]["continuity"][field] = "9" * 64
    for current in (missing, changed):
        with pytest.raises(states.LabSeedStateError):
            validate(current)


@pytest.mark.parametrize("phase_index", range(1, 8))
def test_continuity_drift_refuses_in_every_normal_phase(phase_index):
    # Reference prerequisites must also match the already reported shared lab
    # controller/daemon bindings; this does not prove those reports were observed.
    current = history()[7 + phase_index]
    current["events"][-1]["record"]["continuity"]["daemon_process_sha256"] = "9" * 64
    with pytest.raises(states.LabSeedStateError):
        validate(current)


def test_role_backing_directories_cannot_report_the_same_identity():
    current = history()[8]
    current["events"][-1]["record"]["continuity"]["backing_directory_sha256"] = "f" * 64
    with pytest.raises(states.LabSeedStateError):
        validate(current)


@pytest.mark.parametrize(
    "field,value",
    [
        ("running", True),
        ("running", 0),
        ("paused", True),
        ("restarting", True),
        ("dead", True),
        ("oom_killed", True),
        ("pid", 1),
        ("pid", False),
        ("restart_count", 1),
        ("restart_count", 0.0),
        ("exit_code", 78),
        ("error", "private daemon canary"),
        ("status", "running"),
    ],
)
def test_terminal_runtime_is_strict_and_unsuccessful_reports_need_diagnostics(field, value):
    current = history()[6]
    current["events"][-1]["record"]["runtime"][field] = value
    with pytest.raises(states.LabSeedStateError) as caught:
        validate(current)
    assert "private daemon canary" not in str(caught.value)


@pytest.mark.parametrize(
    "stamp",
    [
        auxiliary.ZERO_TIME,
        "0001-01-01T00:00:00.0Z",
        "0001-01-01T00:00:00.000000Z",
        "0001-01-01T00:00:00.000000000Z",
        "2026-02-30T00:00:00Z",
        "2026-01-01",
        "2026-01-01T00:00:00.0000000000Z",
        None,
    ],
)
def test_terminal_times_reject_invalid_and_semantically_zero_values(stamp):
    for field in ("started_at", "finished_at"):
        current = history()[6]
        current["events"][-1]["record"]["runtime"][field] = stamp
        with pytest.raises(states.LabSeedStateError):
            validate(current)


@pytest.mark.parametrize("digits", range(1, 10))
def test_terminal_fraction_precision_is_validated_without_python_version_dependency(digits):
    current = history()[6]
    stamp = "2026-11-15T07:00:00." + "1" * digits + "Z"
    current["events"][-1]["record"]["runtime"]["started_at"] = stamp
    assert validate(current) == current


def test_nonzero_nanosecond_is_not_confused_with_python_microsecond_truncation():
    current = history()[6]
    current["events"][-1]["record"]["runtime"]["started_at"] = "0001-01-01T00:00:00.000000001Z"
    assert validate(current) == current


@pytest.mark.parametrize(
    "field,value",
    [
        ("started_at", "2026-11-15T07:00:00Z"),
        ("status", "exited"),
        ("pid", 12),
    ],
)
def test_created_report_must_describe_never_started_helper(field, value):
    current = history()[3]
    current["events"][-1]["record"]["runtime"][field] = value
    with pytest.raises(states.LabSeedStateError):
        validate(current)


@pytest.mark.parametrize("field", ["configuration_sha256", "mounts_sha256", "image", "helper_name"])
def test_terminal_report_cannot_rebase_created_configuration(field):
    current = history()[6]
    current["events"][-1]["record"][field] = "a" * 64
    with pytest.raises(states.LabSeedStateError):
        validate(current)


@pytest.mark.parametrize(
    "change",
    [
        "missing_inspection",
        "missing_manifest",
        "extra_leaf",
        "other_role",
        "size_type",
        "volume",
        "report_only",
    ],
)
def test_exit_zero_or_report_line_cannot_replace_exact_seed_reinspection(change):
    current = history()[7]
    record = current["events"][-1]["record"]
    if change == "missing_inspection":
        del record["seed_inspection_sha256"]
    elif change == "missing_manifest":
        del record["manifest"]
    elif change == "extra_leaf":
        record["manifest"].append(copy.deepcopy(record["manifest"][0]))
    elif change == "other_role":
        record["manifest"] = initial()["roles"]["reference"]["seed_manifest"]
    elif change == "size_type":
        record["manifest"][0]["size"] = float(record["manifest"][0]["size"])
    elif change == "volume":
        record["volume_name"] = initial()["roles"]["reference"]["volume_name"]
    else:
        del record["manifest"]
        record["report"] = {
            "status": "script_completed",
            "execution_admission": False,
            "compatibility_verified": False,
        }
    with pytest.raises(states.LabSeedStateError):
        validate(current)
    assert validate(history()[6])["execution_admission"] is False


@pytest.mark.parametrize(
    "index,code",
    [(2, "create_ambiguous"), (4, "start_ambiguous"), (5, "helper_failed"), (7, "continuity_lost")],
)
def test_diagnostics_retain_facts_and_only_allow_further_diagnostics(index, code):
    prior = history()[index]
    closed = append(prior, diagnostic(code=code))
    assert validate(closed, prior) == closed
    assert closed["events"][:-1] == prior["events"]
    further = append(closed, diagnostic("reference"))
    assert validate(further, closed) == further
    next_event = events()[index] if index < 7 else events("reference")[0]
    with pytest.raises(states.LabSeedStateError):
        validate(append(closed, next_event), closed)
    assert closed["execution_admission"] is False


def test_unexplained_ambiguity_and_arbitrary_diagnostic_text_refuse():
    for event in (
        diagnostic(code="create_ambiguous"),
        diagnostic(code="start_ambiguous"),
        diagnostic(code="private exception canary"),
    ):
        with pytest.raises(states.LabSeedStateError) as caught:
            validate(append(initial(), event))
        assert "private exception canary" not in str(caught.value)


@pytest.mark.parametrize("change", ["skipped_append", "stale_digest", "old_fact", "transaction"])
def test_predecessor_bound_validation_cannot_rebase_history(change):
    prior, current = history()[3:5]
    if change == "skipped_append":
        current = history()[5]
        current["predecessor_sha256"] = auxiliary._digest(prior)
    elif change == "stale_digest":
        current["predecessor_sha256"] = "a" * 64
    elif change == "old_fact":
        current["events"][0]["record"]["volume_inspection_sha256"] = "a" * 64
    else:
        current["transaction_id"] = "a" * 32
    # Standalone consistency deliberately cannot establish actual prior disk history.
    assert validate(current) == current
    with pytest.raises(states.LabSeedStateError):
        validate(current, prior)


def test_valid_binding_cannot_rewrite_frozen_recipe_or_assert_authority():
    changes = [
        ("execution_admission", True),
        ("execution_admission", 0),
        ("compatibility_verified", True),
        ("schema", auxiliary.SCHEMA),
        ("scope", "admitted"),
    ]
    for key, value in changes:
        current = initial()
        current[key] = value
        with pytest.raises(states.LabSeedStateError):
            validate(current)
    current = initial()
    current["roles"]["working"]["create_body_sha256"] = "a" * 64
    with pytest.raises(states.LabSeedStateError):
        validate(current)
    current = initial()
    current["roles"]["working"]["volume_id"] = "a" * 64
    with pytest.raises(states.LabSeedStateError):
        validate(current)


def test_full_context_drift_with_same_names_refuses():
    ctx = context()
    image_drift = replace(
        ctx,
        images=tuple(
            (role, "sha256:" + "f" * 64 if role == "original" else pin) for role, pin in ctx.images
        ),
    )
    for changed in (
        replace(ctx, source_commit="f" * 40),
        replace(ctx, engine_id="other-engine"),
        image_drift,
    ):
        assert changed.generation_root == ctx.generation_root
        with pytest.raises(states.LabSeedStateError):
            validate(history()[-1], ctx=changed)


def test_detached_inputs_outputs_and_json_forms():
    current, prior = history()[4], history()[3]
    before = copy.deepcopy((current, prior))
    result = validate(current, prior)
    assert (current, prior) == before
    result["events"][0]["record"]["continuity"]["engine_id"] = "changed"
    assert (current, prior) == before
    assert validate(json.dumps(current).encode()) == current


def test_closed_shapes_duplicate_keys_nonfinite_and_byte_event_bounds():
    raw = auxiliary._encoded(initial())
    duplicate = raw.replace(b'"events":[]', b'"events":[],"events":[]')
    nested = raw.replace(
        b'"volume_identity":"no_immutable_docker_id"',
        b'"volume_identity":"no_immutable_docker_id","volume_identity":"x"',
        1,
    )
    for value in (
        duplicate,
        nested,
        b'{"events":[NaN]}',
        b"x" * (auxiliary.MAX_RECORD_BYTES + 1),
        {**initial(), "override": True},
    ):
        with pytest.raises(states.LabSeedStateError):
            validate(value)
    current = history()[-1]
    while len(current["events"]) <= states.MAX_EVENTS:
        current = append(current, diagnostic())
    with pytest.raises(states.LabSeedStateError):
        validate(current)


def test_no_io_clock_permissions_persistence_or_runtime_operations(monkeypatch):
    ctx = context()
    current = history()[-1]

    def forbidden(*_args, **_kwargs):
        pytest.fail("Pure state contract crossed an I/O or clock boundary")

    for owner, name in (
        (builtins, "open"),
        (os, "open"),
        (os, "getenv"),
        (socket, "socket"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (time, "time"),
        (time, "monotonic"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    result = states.validate_state(ctx, lab_binding=ctx.journal_binding(), state=current)
    assert result["execution_admission"] is False and result["compatibility_verified"] is False
