"""Paired production/lab preparation checks; no Docker, filesystem or authority."""

import copy
import json
from dataclasses import replace

import network_compose as renderer
import network_lab_context as lab
import network_policy as policy
import pytest
import yaml
from test_network_compose import RAW, specification
from test_network_lab_context import record
from test_network_policy import fixture


def context():
    return lab.from_record(record())


def baseline(ctx):
    model = fixture()
    if ctx is not None:
        names = ctx.resource_map()
        model["name"] = ctx.project
        model["networks"]["default"]["name"] = names[policy.DEFAULT_NAME]
        for role in policy.MEMBERS:
            model["services"][role]["container_name"] = names[role]
    return model


def prepared(ctx):
    model = baseline(ctx)
    candidate, receipt = policy.plan_networks(model, lab_context=ctx)
    spec = {
        "original_networks": {r: model["services"][r]["networks"] for r in policy.MEMBERS},
        "desired_networks": {r: candidate["services"][r]["networks"] for r in policy.MEMBERS},
        "dedicated_definition": candidate["networks"][policy.NETWORK],
        "lab_context": ctx,
        "review_receipt": receipt if ctx is not None else None,
    }
    return model, candidate, receipt, spec


def raw(model):
    return yaml.safe_dump(model, sort_keys=False).encode()


@pytest.mark.parametrize("scenario", sorted(lab.SCENARIOS))
def test_lab_plan_render_and_scope_keep_roles_egress_and_full_binding(scenario):
    ctx = replace(context(), scenario=scenario)
    before, candidate, receipt, spec = prepared(ctx)
    snapshot = copy.deepcopy(before)
    policy.verify_network_scope(before, candidate, lab_context=ctx, review_receipt=receipt)
    runtime = policy.expected_runtime_networks(candidate, lab_context=ctx)
    rendered = renderer.render_networks(raw(before), **spec)
    assert yaml.safe_load(rendered) == candidate
    assert before == snapshot
    assert candidate["networks"][policy.NETWORK] == ctx.network_definition()
    assert set(runtime[policy.BRIDGE]) == {ctx.resource_map()[policy.NETWORK_NAME]}
    for role in policy.MEMBERS:
        opts = candidate["services"][role]["networks"][policy.NETWORK]
        assert opts["aliases"] == [role]
        assert opts.get("gw_priority", 0) == (0 if role == policy.BRIDGE else -1)
    assert receipt["mode"] == "lab" and receipt["context_sha256"] == ctx.digest()
    assert receipt["release_gate_satisfied"] is False
    assert receipt["production_modified"] is False
    assert receipt["live_routes_and_reachability_verified"] is False
    exported = json.dumps(receipt)
    for value in (
        ctx.vm_uuid,
        ctx.engine_id,
        ctx.source_commit,
        "synthetic-never-export-this-value",
    ):
        assert value not in exported


def test_lab_calls_leave_production_results_and_globals_unchanged():
    model = fixture()
    prior = policy.plan_networks(model)
    prior_raw = renderer.render_networks(RAW, **specification())
    constants = copy.deepcopy(policy.NETWORK_DEFINITION)
    before, candidate, receipt, spec = prepared(context())
    renderer.render_networks(raw(before), **spec)
    policy.verify_network_scope(before, candidate, lab_context=context(), review_receipt=receipt)
    assert policy.plan_networks(model) == prior
    assert policy.plan_networks(model, lab_context=None) == prior
    assert renderer.render_networks(RAW, **specification(), lab_context=None) == prior_raw
    assert policy.NETWORK_DEFINITION == constants
    assert "mode" not in prior[1] and "context_sha256" not in prior[1]


def test_production_and_lab_cannot_cross_topology_modes():
    ctx = context()
    with pytest.raises(policy.NetworkPolicyError):
        policy.plan_networks(fixture(), lab_context=ctx)
    with pytest.raises(policy.NetworkPolicyError):
        policy.plan_networks(baseline(ctx))
    before, candidate, receipt, spec = prepared(ctx)
    with pytest.raises(policy.NetworkPolicyError):
        policy.expected_runtime_networks(candidate)
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(fixture()), **spec)
    spec["lab_context"] = None
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(before), **spec)
    production, planned, _, _ = prepared(None)
    with pytest.raises(policy.NetworkPolicyError):
        policy.verify_network_scope(production, planned, review_receipt=receipt)


@pytest.mark.parametrize("bad", [{}, "lab", True, 1, object()])
def test_invalid_context_types_are_refused(bad):
    model, candidate, _, spec = prepared(context())
    for operation in (
        lambda: policy.plan_networks(model, lab_context=bad),
        lambda: policy.expected_runtime_networks(candidate, lab_context=bad),
        lambda: policy.verify_network_scope(model, candidate, lab_context=bad),
        lambda: renderer.render_networks(raw(model), **{**spec, "lab_context": bad}),
    ):
        with pytest.raises((policy.NetworkPolicyError, renderer.ComposeNetworkError)):
            operation()


def test_context_subclass_refuses_before_reading_attributes():
    class Foreign(lab.LabContext):
        def to_record(self):
            raise AssertionError("Must not execute subclass methods")

    ctx = context()
    foreign = Foreign(**{key: getattr(ctx, key) for key in ctx.__dataclass_fields__})
    with pytest.raises(policy.NetworkPolicyError):
        policy.plan_networks(baseline(ctx), lab_context=foreign)


@pytest.mark.parametrize("field", ["project", "default", *policy.MEMBERS])
@pytest.mark.parametrize("bad", [None, "${FOREIGN}", "docker", "codex-foreign"])
def test_concrete_lab_identities_required_in_resolved_and_raw_inputs(field, bad):
    ctx = context()
    model, _, _, spec = prepared(ctx)
    if field == "project":
        model["name"] = bad
    elif field == "default":
        model["networks"]["default"]["name"] = bad
    else:
        if bad is None:
            model["services"][field].pop("container_name")
        else:
            model["services"][field]["container_name"] = bad
    with pytest.raises(policy.NetworkPolicyError):
        policy.plan_networks(model, lab_context=ctx)
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(model), **spec)


@pytest.mark.parametrize("stage", ["baseline", "candidate"])
@pytest.mark.parametrize("field", ["service", "container_name", "hostname", "aliases"])
@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("upper", [False, True])
def test_unrelated_cannot_steal_canonical_or_physical_alias(stage, field, physical, upper):
    ctx = context()
    model, candidate, _, _ = prepared(ctx)
    model = model if stage == "baseline" else candidate
    stolen = ctx.resource_map()[policy.BRIDGE] if physical else policy.BRIDGE
    if upper:
        stolen = stolen.upper()
    service = model["services"]["unrelated"]
    if field == "service":
        model["services"][stolen] = model["services"].pop("unrelated")
    elif field == "aliases":
        service["networks"]["default"] = {"aliases": [stolen]}
    else:
        service[field] = stolen
    operation = policy.plan_networks if stage == "baseline" else policy.expected_runtime_networks
    with pytest.raises(policy.NetworkPolicyError):
        operation(model, lab_context=ctx)


@pytest.mark.parametrize("physical", [False, True])
def test_dedicated_only_bridge_cannot_steal_consumers_hostname(physical):
    ctx = context()
    _, candidate, _, _ = prepared(ctx)
    stolen = ctx.resource_map()["nginx"] if physical else "nginx"
    candidate["services"][policy.BRIDGE]["hostname"] = stolen.upper()
    with pytest.raises(policy.NetworkPolicyError):
        policy.expected_runtime_networks(candidate, lab_context=ctx)


@pytest.mark.parametrize(
    "field,value",
    [
        ("vm_uuid", "12345678-1234-1234-1234-123456789abd"),
        ("vm_name", "codex-grok-lab-other"),
        ("engine_id", "another-engine"),
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
def test_foreign_full_context_receipt_refuses_even_when_names_match(field, value):
    ctx = context()
    other = replace(ctx, **{field: value})
    model, candidate, _, spec = prepared(other)
    _, old_receipt = policy.plan_networks(baseline(ctx), lab_context=ctx)
    assert old_receipt["context_sha256"] != other.digest()
    if field not in {"context_id", "scenario"}:
        assert ctx.resource_map() == other.resource_map()
        policy.expected_runtime_networks(candidate, lab_context=ctx)  # Topology only.
    with pytest.raises(policy.NetworkPolicyError, match="receipt"):
        policy.verify_network_scope(model, candidate, lab_context=other, review_receipt=old_receipt)
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(model), **{**spec, "review_receipt": old_receipt})


@pytest.mark.parametrize("field", ["mode", "context_sha256", "release_gate_satisfied", "extra"])
@pytest.mark.parametrize("remove", [False, True])
def test_tampered_or_incomplete_review_receipt_refuses(field, remove):
    ctx = context()
    model, candidate, receipt, spec = prepared(ctx)
    if remove:
        receipt.pop(field if field != "extra" else "network")
    else:
        receipt[field] = True
    with pytest.raises(policy.NetworkPolicyError):
        policy.verify_network_scope(model, candidate, lab_context=ctx, review_receipt=receipt)
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(model), **spec)


def test_lab_receipt_is_mandatory_and_is_not_an_execution_permit():
    ctx = context()
    model, candidate, receipt, spec = prepared(ctx)
    with pytest.raises(policy.NetworkPolicyError):
        policy.verify_network_scope(model, candidate, lab_context=ctx)
    spec.pop("review_receipt")
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(model), **spec)
    assert all(
        receipt[key] is False
        for key in (
            "production_modified",
            "release_gate_satisfied",
            "live_routes_and_reachability_verified",
        )
    )


@pytest.mark.parametrize("lab_mode", [False, True])
@pytest.mark.parametrize("unsafe", ["namespace", "ports", "extra_member", "internal", "priority"])
def test_unsafe_topology_still_refuses_in_both_modes(lab_mode, unsafe):
    ctx = context() if lab_mode else None
    model, candidate, _, _ = prepared(ctx)
    if unsafe == "namespace":
        model["services"]["unrelated"]["network_mode"] = "service:" + policy.BRIDGE
    elif unsafe == "ports":
        model["services"][policy.BRIDGE]["ports"] = [{"target": 5900, "published": "5900"}]
    else:
        if unsafe == "extra_member":
            candidate["services"]["unrelated"]["networks"][policy.NETWORK] = None
        elif unsafe == "internal":
            candidate["networks"][policy.NETWORK]["internal"] = True
        else:
            candidate["services"]["nginx"]["networks"][policy.NETWORK]["gw_priority"] = 0
        with pytest.raises(policy.NetworkPolicyError):
            policy.expected_runtime_networks(candidate, lab_context=ctx)
        return
    with pytest.raises(policy.NetworkPolicyError):
        policy.plan_networks(model, lab_context=ctx)


@pytest.mark.parametrize("field", ["alias", "ports", "namespace", "already_declared", "malformed"])
def test_raw_lab_scope_validated_independently_of_synthetic_network_spec(field):
    ctx = context()
    model, _, _, spec = prepared(ctx)
    if field == "alias":
        model["services"]["unrelated"]["hostname"] = ctx.resource_map()["nginx"]
    elif field == "ports":
        model["services"][policy.BRIDGE]["ports"] = ["5900:5900"]
    elif field == "namespace":
        model["services"]["unrelated"]["network_mode"] = "service:nginx"
    elif field == "malformed":
        model["services"] = []
    else:
        model["networks"][policy.NETWORK] = ctx.network_definition()
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(model), **spec)


def test_raw_lab_unrelated_bytes_and_implicit_memberships_survive():
    ctx = context()
    model, _, _, spec = prepared(ctx)
    model["services"]["nginx"].pop("networks")
    model["services"][policy.BRIDGE]["networks"] = ["default"]
    source = raw(model) + b"# Retained unrelated bytes.\nx-retained: 'verbatim: yes'\n"
    result = renderer.render_networks(source, **spec)
    assert result.endswith(b"# Retained unrelated bytes.\nx-retained: 'verbatim: yes'\n")
    assert b"synthetic-never-export-this-value" in result
    assert (
        yaml.safe_load(result)["services"]["nginx"]["container_name"] == ctx.resource_map()["nginx"]
    )


def test_renderer_refuses_mismatched_dedicated_definition():
    model, _, _, spec = prepared(context())
    spec["dedicated_definition"] = policy.NETWORK_DEFINITION
    with pytest.raises(renderer.ComposeNetworkError):
        renderer.render_networks(raw(model), **spec)
