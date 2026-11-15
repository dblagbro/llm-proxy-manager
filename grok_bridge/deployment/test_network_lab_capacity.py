"""Synthetic capacity/receipt refusals; no filesystem or Docker observation."""

import copy
import json
from dataclasses import FrozenInstanceError, replace

import network_lab_capacity as capacity
import network_lab_context as lab
import pytest
from test_network_lab_context import record

GIB = 1024**3


def context():
    return lab.from_record(record())


def observations(*, shared=True, available=20 * GIB):
    return {
        role: {
            "filesystem_id": "guest-device:8:1" if shared else "guest-device:" + role,
            "total_bytes": 60 * GIB,
            "available_bytes": available,
        }
        for role in capacity.ROLES
    }


def peaks():
    return {"evidence": 2 * GIB, "docker": 3 * GIB}


def test_same_filesystem_aggregates_peaks_and_one_reserve_at_exact_boundary():
    ctx = context()
    supplied = observations(available=15 * GIB)
    result = capacity.assess(ctx, supplied, peaks=peaks())
    group = result["filesystems"][0]
    assert len(result["filesystems"]) == 1
    assert group["roles"] == ["evidence", "docker"]
    assert group["reserve_bytes"] == 10 * GIB
    assert group["peak_bytes"] == 5 * GIB
    assert group["required_bytes"] == group["available_bytes"] == 15 * GIB
    capacity.verify(result, ctx, supplied, peaks=peaks())
    supplied["docker"]["available_bytes"] -= 1
    with pytest.raises(capacity.LabCapacityError, match="Insufficient"):
        capacity.assess(ctx, supplied, peaks=peaks())


@pytest.mark.parametrize("lower_role", capacity.ROLES)
def test_shared_storage_uses_minimum_available_not_sum_or_last_observation(lower_role):
    supplied = observations(available=40 * GIB)
    supplied[lower_role]["available_bytes"] = 15 * GIB
    result = capacity.assess(context(), supplied, peaks=peaks())
    assert result["filesystems"][0]["available_bytes"] == 15 * GIB
    supplied[lower_role]["available_bytes"] -= 1
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=peaks())


@pytest.mark.parametrize("short_role", capacity.ROLES)
def test_distinct_filesystems_each_need_reserve_and_their_own_peak(short_role):
    supplied = observations(shared=False)
    for role in capacity.ROLES:
        supplied[role]["available_bytes"] = capacity.RESERVE_BYTES + peaks()[role]
    result = capacity.assess(context(), supplied, peaks=peaks())
    assert len(result["filesystems"]) == 2
    assert sum(group["reserve_bytes"] for group in result["filesystems"]) == 20 * GIB
    assert [group["peak_bytes"] for group in result["filesystems"]] == [2 * GIB, 3 * GIB]
    supplied[short_role]["available_bytes"] -= 1
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=peaks())


def test_one_explicit_zero_peak_is_allowed_but_separate_storage_still_needs_reserve():
    supplied = observations(shared=False, available=10 * GIB)
    required = {"evidence": 0, "docker": 1}
    supplied["docker"]["available_bytes"] += 1
    result = capacity.assess(context(), supplied, peaks=required)
    assert result["filesystems"][0]["required_bytes"] == 10 * GIB
    supplied["evidence"]["available_bytes"] -= 1
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=required)


def test_conflicting_totals_for_same_filesystem_refuse_even_with_enough_space():
    supplied = observations()
    supplied["docker"]["total_bytes"] += 1
    with pytest.raises(capacity.LabCapacityError, match="totals"):
        capacity.assess(context(), supplied, peaks=peaks())


@pytest.mark.parametrize("field", ["total_bytes", "available_bytes", "peak"])
@pytest.mark.parametrize("bad", [True, False, 1.0, float("nan"), -1, "1", None, 2**63])
def test_byte_counts_require_bounded_nonnegative_exact_integers(field, bad):
    supplied, required = observations(), peaks()
    if field == "peak":
        required["evidence"] = bad
    else:
        supplied["evidence"][field] = bad
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=required)


def test_available_cannot_exceed_total():
    supplied = observations()
    supplied["evidence"]["available_bytes"] = supplied["evidence"]["total_bytes"] + 1
    with pytest.raises(capacity.LabCapacityError, match="counters"):
        capacity.assess(context(), supplied, peaks=peaks())


@pytest.mark.parametrize("role", capacity.ROLES)
@pytest.mark.parametrize(
    "kind", ["observation", "peak", "filesystem_id", "total_bytes", "available_bytes"]
)
def test_missing_observations_peaks_or_fields_never_default_to_zero(role, kind):
    supplied, required = observations(), peaks()
    if kind == "observation":
        supplied.pop(role)
    elif kind == "peak":
        required.pop(role)
    else:
        supplied[role].pop(kind)
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=required)


def test_peak_argument_is_mandatory_and_combined_peak_must_be_positive():
    with pytest.raises(TypeError):
        capacity.assess(context(), observations())
    with pytest.raises(capacity.LabCapacityError, match="positive"):
        capacity.assess(context(), observations(), peaks=dict.fromkeys(capacity.ROLES, 0))


@pytest.mark.parametrize("where", ["observations", "peaks", "record"])
def test_unknown_fields_are_rejected(where):
    supplied, required = observations(), peaks()
    target = {"observations": supplied, "peaks": required, "record": supplied["evidence"]}[where]
    target["approved"] = True
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=required)


@pytest.mark.parametrize(
    "bad", [None, "", "../guest", "/dev/sda", "secret\nvalue", "x" * 129, True]
)
def test_filesystem_identity_is_explicit_and_bounded(bad):
    supplied = observations()
    supplied["evidence"]["filesystem_id"] = bad
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(context(), supplied, peaks=peaks())


@pytest.mark.parametrize("bad", [None, {}, True, "lab", object()])
def test_exact_context_required(bad):
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(bad, observations(), peaks=peaks())


def test_subclass_hooks_are_not_invoked():
    class Foreign(lab.LabContext):
        def to_record(self):
            raise AssertionError("Do not invoke subclass hooks")

    class ForeignMapping(dict):
        def items(self):
            raise AssertionError("Do not invoke subclass hooks")

    ctx = context()
    foreign = Foreign(**{key: getattr(ctx, key) for key in ctx.__dataclass_fields__})
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(foreign, observations(), peaks=peaks())
    with pytest.raises(capacity.LabCapacityError):
        capacity.assess(ctx, ForeignMapping(observations()), peaks=peaks())


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine_id", "other-engine"),
        ("vm_name", "codex-grok-lab-other"),
        ("vm_uuid", "12345678-1234-1234-1234-123456789abd"),
        ("source_commit", "c" * 40),
        (
            "images",
            (
                ("original", "sha256:" + "4" * 64),
                ("candidate", "sha256:" + "2" * 64),
                ("nginx", "sha256:" + "3" * 64),
            ),
        ),
    ],
)
def test_full_context_drift_refuses_even_with_same_namespace(field, value):
    ctx = context()
    result = capacity.assess(ctx, observations(), peaks=peaks())
    foreign = replace(ctx, **{field: value})
    assert foreign.namespace == ctx.namespace
    with pytest.raises(capacity.LabCapacityError, match="assessment"):
        capacity.verify(result, foreign, observations(), peaks=peaks())


@pytest.mark.parametrize("change", ["nonminimum_available", "identity", "peak_distribution"])
def test_exact_input_drift_refuses_even_if_capacity_totals_still_match(change):
    supplied, required = observations(), peaks()
    supplied["docker"]["available_bytes"] += GIB
    result = capacity.assess(context(), supplied, peaks=required)
    if change == "nonminimum_available":
        supplied["docker"]["available_bytes"] += 1
    elif change == "identity":
        for role in capacity.ROLES:
            supplied[role]["filesystem_id"] = "other-device"
    else:
        required["evidence"] += 1
        required["docker"] -= 1
    with pytest.raises(capacity.LabCapacityError, match="assessment"):
        capacity.verify(result, context(), supplied, peaks=required)


@pytest.mark.parametrize("change", ["extra", "mode", "digest", "float", "roles", "status"])
def test_tampered_receipt_refuses_without_coercion(change):
    result = capacity.assess(context(), observations(), peaks=peaks())
    if change == "float":
        group = result["filesystems"][0]
        group["available_bytes"] = float(group["available_bytes"])
    elif change == "roles":
        result["filesystems"][0]["roles"].reverse()
    else:
        key = {"extra": "approved", "mode": "mode", "digest": "context_sha256", "status": "status"}[
            change
        ]
        result[key] = True
    with pytest.raises(capacity.LabCapacityError, match="assessment"):
        capacity.verify(result, context(), observations(), peaks=peaks())


def test_context_inputs_and_returned_receipts_are_detached():
    ctx, supplied, required = context(), observations(), peaks()
    snapshot = copy.deepcopy((supplied, required))
    result = capacity.assess(ctx, supplied, peaks=required)
    original = copy.deepcopy(result)
    assert (supplied, required) == snapshot
    assert ctx.digest() == result["context_sha256"]
    with pytest.raises(FrozenInstanceError):
        ctx.engine_id = "other"
    supplied["evidence"]["available_bytes"] -= 1
    required["evidence"] += 1
    assert result == original
    result["filesystems"][0]["roles"].append("foreign")
    assert capacity.assess(ctx, snapshot[0], peaks=snapshot[1]) == original


def test_mapping_order_does_not_change_assessment():
    supplied, required = observations(shared=False), peaks()
    reversed_inputs = dict(reversed(list(supplied.items())))
    reversed_peaks = dict(reversed(list(required.items())))
    assert capacity.assess(context(), supplied, peaks=required) == capacity.assess(
        context(), reversed_inputs, peaks=reversed_peaks
    )


def test_receipt_and_errors_omit_private_identifiers_and_make_no_admission_claim():
    ctx, supplied = context(), observations()
    for role in capacity.ROLES:
        supplied[role]["filesystem_id"] = "private-opaque-filesystem-identity"
    result = capacity.assess(ctx, supplied, peaks=peaks())
    encoded = json.dumps(result)
    for private in (
        ctx.vm_name,
        ctx.vm_uuid,
        ctx.engine_id,
        ctx.source_commit,
        supplied["evidence"]["filesystem_id"],
    ):
        assert private not in encoded
    assert result["scope"] == "supplied_values_only_not_execution_admission"
    assert not {"approved", "admitted", "measured", "release_gate_satisfied"} & result.keys()
    supplied["docker"]["filesystem_id"] = "private/invalid/value"
    with pytest.raises(capacity.LabCapacityError) as failure:
        capacity.assess(ctx, supplied, peaks=peaks())
    assert "private" not in str(failure.value)
