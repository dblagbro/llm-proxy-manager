"""Adversarial descriptor/resume checks; no Docker or filesystem operations."""

import copy
import json
from dataclasses import FrozenInstanceError, replace

import network_docker
import network_lab_context as lab
import network_policy as policy
import pytest


def record():
    return {
        "schema": lab.SCHEMA,
        "mode": "lab",
        "context_id": "a" * 32,
        "scenario": "deploy-rollback",
        "vm_uuid": "12345678-1234-1234-1234-123456789abc",
        "vm_name": "codex-grok-lab-example",
        "engine_id": "fixture-engine:123",
        "source_commit": "b" * 40,
        "images": {
            "original": "sha256:" + "1" * 64,
            "candidate": "sha256:" + "2" * 64,
            "nginx": "sha256:" + "3" * 64,
        },
    }


@pytest.mark.parametrize("scenario", sorted(lab.SCENARIOS))
def test_roundtrip_and_binding_are_exact(scenario):
    source = record()
    source["scenario"] = scenario
    context = lab.parse_record(json.dumps(source).encode())
    assert context.to_record() == source
    assert lab.from_record(context.to_record()) == context
    context.verify_journal_binding(context.journal_binding())
    assert len(context.digest()) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "production"),
        ("mode", True),
        ("schema", "grok-network-lab-context-v2"),
        ("context_id", "../production"),
        ("context_id", "A" * 32),
        ("context_id", None),
        ("scenario", "production-cutover"),
        ("scenario", ["deploy-rollback"]),
        ("vm_name", "tmrwww01"),
        ("vm_name", "tmrwww02"),
        ("vm_name", "codex-grok-lab-x\n"),
        ("vm_name", "codex-grok-lab-" + "x" * 60),
        ("vm_name", "codex-grok-lab-x-"),
        ("vm_uuid", "00000000-0000-0000-0000-000000000000"),
        ("vm_uuid", "not-a-uuid"),
        ("engine_id", "ssh://production"),
        ("engine_id", "socket\nprivate"),
        ("engine_id", 1),
        ("source_commit", "main"),
        ("source_commit", "b" * 39),
    ],
)
def test_invalid_or_production_identity_refused(field, value):
    source = record()
    source[field] = value
    with pytest.raises(lab.LabContextError):
        lab.from_record(source)


@pytest.mark.parametrize(
    "key",
    [
        "approved",
        "grok_release_gate_satisfied",
        "private_root",
        "resource_map",
        "internal",
        "capacity_reserve",
    ],
)
def test_injected_authority_paths_or_policy_refused(key):
    source = record()
    source[key] = True
    with pytest.raises(lab.LabContextError):
        lab.from_record(source)


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "tag", "collision", "uppercase", "not-dict"]
)
def test_image_roles_and_immutable_ids(mutation):
    source = record()
    images = source["images"]
    if mutation == "missing":
        images.pop("nginx")
    elif mutation == "extra":
        images["gateway"] = "sha256:" + "4" * 64
    elif mutation == "tag":
        images["original"] = "grok:latest"
    elif mutation == "collision":
        images["candidate"] = images["original"]
    elif mutation == "uppercase":
        images["candidate"] = "sha256:" + "A" * 64
    else:
        source["images"] = list(images.items())
    with pytest.raises(lab.LabContextError):
        lab.from_record(source)


def test_context_and_returned_records_do_not_alias_caller_mutations():
    source = record()
    context = lab.from_record(source)
    digest = context.digest()
    source["images"]["original"] = "grok:latest"
    context.to_record()["images"]["candidate"] = "changed"
    context.resource_map()[policy.BRIDGE] = policy.BRIDGE
    context.network_definition()["internal"] = True
    assert context.digest() == digest
    with pytest.raises(FrozenInstanceError):
        context.engine_id = "other"
    with pytest.raises(lab.LabContextError):
        replace(context, vm_name="tmrwww01")


def test_map_is_complete_disjoint_and_accepted_without_any_host_call():
    context = lab.from_record(record())
    mapping = context.resource_map()
    assert set(mapping) == {*policy.MEMBERS, policy.DEFAULT_NAME, policy.NETWORK_NAME}
    assert set(mapping).isdisjoint(mapping.values())
    assert len(set(mapping.values())) == len(mapping)

    def forbidden(*args, **kwargs):
        pytest.fail("Descriptor validation invoked a host command")

    host = network_docker.DockerHost(
        runner=forbidden, compose_project=context.project, resource_map=mapping
    )
    assert host.enabled is False
    assert host.resources == mapping
    assert context.network_definition()["internal"] is False
    assert context.generation_root == lab.ROOT / context.namespace
    assert context.configuration_root == context.generation_root / "configuration"


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_id", "c" * 32),
        ("scenario", "ambiguous-candidate"),
        ("vm_uuid", "22345678-1234-1234-1234-123456789abc"),
        ("vm_name", "codex-grok-lab-other"),
        ("engine_id", "another-engine"),
        ("source_commit", "c" * 40),
    ],
)
def test_recomputed_foreign_binding_does_not_authorize_resume(field, value):
    original = lab.from_record(record())
    changed = record()
    changed[field] = value
    foreign = lab.from_record(changed)
    with pytest.raises(lab.LabContextError):
        original.verify_journal_binding(foreign.journal_binding())


@pytest.mark.parametrize("mutation", ["digest", "mode", "schema", "extra", "image", "missing"])
def test_tampered_journal_binding_refused(mutation):
    context = lab.from_record(record())
    binding = context.journal_binding()
    if mutation == "digest":
        binding["context_sha256"] = "c" * 64
    elif mutation == "mode":
        binding["mode"] = "production"
    elif mutation == "schema":
        binding["schema"] = "future-schema"
    elif mutation == "extra":
        binding["approved"] = True
    elif mutation == "image":
        binding["context"]["images"]["candidate"] = "sha256:" + "4" * 64
    else:
        binding.pop("context")
    with pytest.raises(lab.LabContextError):
        context.verify_journal_binding(binding)


@pytest.mark.parametrize("field", ["vm_uuid", "vm_name", "engine_id"])
def test_observation_mismatch_refused_without_inspection(field):
    context = lab.from_record(record())
    observed = {key: record()[key] for key in ("vm_uuid", "vm_name", "engine_id")}
    context.verify_observed_identity(**observed)
    observed[field] += "changed"
    with pytest.raises(lab.LabContextError):
        context.verify_observed_identity(**observed)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * (lab.MAX_RECORD_BYTES + 1),
        b"\xff",
        b"{",
        b"[]",
        b"null",
        b"[" * 1200 + b"]" * 1200,
    ],
)
def test_malformed_oversize_and_deep_json_refused(raw):
    with pytest.raises(lab.LabContextError):
        lab.parse_record(raw)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_keys_are_rejected_at_any_object_depth(nested):
    raw = json.dumps(record())
    if nested:
        raw = raw.replace('"images": {', '"images": {"original": "grok:latest",')
    else:
        raw = raw.replace("{", '{"mode":"production",', 1)
    with pytest.raises(lab.LabContextError):
        lab.parse_record(raw.encode())


def test_diagnostics_do_not_disclose_rejected_private_input():
    source = copy.deepcopy(record())
    source["engine_id"] = "secret/password/example"
    with pytest.raises(lab.LabContextError) as failure:
        lab.from_record(source)
    assert "secret" not in str(failure.value) and "password" not in str(failure.value)


def test_mapping_order_does_not_change_journal_binding():
    original = record()
    reordered = dict(reversed(list(original.items())))
    reordered["images"] = dict(reversed(list(original["images"].items())))
    lab.from_record(original).verify_journal_binding(lab.from_record(reordered).journal_binding())


@pytest.mark.parametrize("length", [63, 64])
def test_vm_name_bound(length):
    source = record()
    source["vm_name"] = "codex-grok-lab-" + "x" * (length - len("codex-grok-lab-"))
    if length == 63:
        assert lab.from_record(source).vm_name == source["vm_name"]
    else:
        with pytest.raises(lab.LabContextError):
            lab.from_record(source)


def test_scenarios_with_one_context_id_have_disjoint_paths_projects_and_resources():
    contexts = [replace(lab.from_record(record()), scenario=s) for s in lab.SCENARIOS]
    assert len({c.generation_root for c in contexts}) == len(contexts)
    assert len({c.configuration_root for c in contexts}) == len(contexts)
    assert len({c.project for c in contexts}) == len(contexts)
    all_resources = [name for c in contexts for name in c.resource_map().values()]
    assert len(set(all_resources)) == len(all_resources)

    def forbidden(*args, **kwargs):
        pytest.fail("Scenario mapping validation invoked a host command")

    for c in contexts:
        host = network_docker.DockerHost(
            runner=forbidden, compose_project=c.project, resource_map=c.resource_map()
        )
        assert host.enabled is False
