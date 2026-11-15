"""Pure auxiliary recipes and in-memory snippet checks; no real processes/signals/Docker."""

import ast
import builtins
import copy
import io
import os
import socket
import subprocess
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import network_lab_auxiliary as auxiliary
import network_lab_context as lab
import network_lab_fixtures as fixtures
import network_policy as policy
import pytest
import yaml
from test_network_lab_context import record


def context():
    return lab.from_record(record())


def inputs(ctx=None):
    ctx = ctx or context()
    return auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=ctx.journal_binding())


def models(ctx=None):
    bundle = inputs(ctx)
    return (
        yaml.safe_load(bundle[auxiliary.REFERENCE_COMPOSE]),
        yaml.safe_load(bundle[auxiliary.PROBE_COMPOSE]),
    )


@pytest.mark.parametrize("scenario", sorted(lab.SCENARIOS))
def test_reference_and_probe_have_separate_ownership_outside_working_project(scenario):
    ctx = replace(context(), scenario=scenario)
    reference, probe = models(ctx)
    original = yaml.safe_load(
        fixtures.original_fixture_inputs(ctx, lab_binding=ctx.journal_binding())[fixtures.COMPOSE]
    )
    assert len({reference["name"], probe["name"], original["name"]}) == 3
    assert reference["name"] == ctx.project + "-reference"
    assert probe["name"] == ctx.project + "-probe"
    ref_role, ref = next(iter(reference["services"].items()))
    probe_role, outsider = next(iter(probe["services"].items()))
    assert ref_role == "frozen-reference" and probe_role == "outsider-probe"
    assert ref["container_name"] == ctx.project + "-frozen-reference"
    assert outsider["container_name"] == ctx.project + "-outsider-probe"
    identities = {item["container_name"] for item in original["services"].values()}
    assert not {ref["container_name"], outsider["container_name"]} & identities
    assert {ref_role, probe_role}.isdisjoint(policy.MEMBERS)
    assert len(reference["services"]) == len(probe["services"]) == 1


def test_reference_volume_is_separate_readonly_local_and_never_copy_up():
    ctx = context()
    reference, probe = models(ctx)
    ref = reference["services"]["frozen-reference"]
    original = yaml.safe_load(
        fixtures.original_fixture_inputs(ctx, lab_binding=ctx.journal_binding())[fixtures.COMPOSE]
    )
    assert reference["volumes"] == {
        "reference-data": {"name": ctx.project + "-reference-data", "driver": "local"}
    }
    assert (
        reference["volumes"]["reference-data"]["name"]
        != original["volumes"]["original-data"]["name"]
    )
    assert ref["volumes"] == [
        {
            "type": "volume",
            "source": "reference-data",
            "target": "/data",
            "read_only": True,
            "volume": {"nocopy": True},
        }
    ]
    assert ref["network_mode"] == "none"
    assert "networks" not in ref and "networks" not in reference
    assert "volumes" not in probe


def test_reference_refusal_entrypoint_clears_browser_command_and_exits_78_in_memory():
    reference, _ = models()
    ref = reference["services"]["frozen-reference"]
    assert ref["entrypoint"][:5] == ["python", "-I", "-B", "-S", "-c"]
    assert len(ref["entrypoint"]) == 6 and ref["command"] == []
    stderr = io.StringIO()

    def exit_code(code):
        raise SystemExit(code)

    fake_sys = SimpleNamespace(stderr=stderr, exit=exit_code)

    def importer(name, *_):
        assert name == "sys"
        return fake_sys

    with pytest.raises(SystemExit) as caught:
        exec(
            compile(ref["entrypoint"][5], "<synthetic-refusal>", "exec"),
            {"__builtins__": {"__import__": importer}},
        )
    assert caught.value.code == 78
    assert stderr.getvalue() == "Synthetic preservation reference: startup forbidden\n"
    assert b"CREATE ONLY; NEVER START" in inputs()[auxiliary.REFERENCE_COMPOSE]
    assert b"including refusal exit 78" in inputs()[auxiliary.REFERENCE_COMPOSE]


def test_probe_has_only_default_membership_no_aliases_and_exact_readonly_script_bind():
    ctx = context()
    _, probe = models(ctx)
    service = probe["services"]["outsider-probe"]
    assert probe["networks"] == {
        "default": {"name": ctx.resource_map()[policy.DEFAULT_NAME], "external": True}
    }
    assert service["networks"] == {"default": None}
    assert "network_mode" not in service
    assert service["entrypoint"] == [
        "python",
        "-I",
        "-B",
        "-S",
        "-u",
        "/opt/codex-lab/probe_idle.py",
    ]
    assert service["command"] == []
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": str(ctx.generation_root / auxiliary.PROBE_SCRIPT),
            "target": "/opt/codex-lab/probe_idle.py",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    source = PurePosixPath(service["volumes"][0]["source"])
    assert ctx.generation_root in source.parents
    assert source.relative_to(ctx.generation_root).as_posix() in inputs(ctx)
    assert ctx.resource_map()[policy.NETWORK_NAME].encode() not in b"\n".join(inputs(ctx).values())


def test_idle_script_only_installs_stop_handlers_and_waits_without_real_signals():
    script = inputs()[auxiliary.PROBE_SCRIPT]
    parsed = ast.parse(script)
    assert [
        name.name
        for node in ast.walk(parsed)
        if isinstance(node, ast.Import)
        for name in node.names
    ] == ["signal"]
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(parsed))
    handlers, calls = {}, []

    class WaitObserved(Exception):
        pass

    def register(signum, handler):
        handlers[signum] = handler

    def pause():
        calls.append("idle-wait")
        raise WaitObserved

    fake_signal = SimpleNamespace(SIGTERM=15, SIGINT=2, signal=register, pause=pause)

    def importer(name, *_):
        assert name == "signal"
        return fake_signal

    with pytest.raises(WaitObserved):
        exec(
            compile(parsed, "<synthetic-idle>", "exec"),
            {"__builtins__": {"__import__": importer, "SystemExit": SystemExit}},
        )
    assert set(handlers) == {15, 2} and calls == ["idle-wait"]
    for signum, stop in handlers.items():
        with pytest.raises(SystemExit) as caught:
            stop(signum, None)
        assert caught.value.code == 0


def test_both_recipes_use_original_pin_and_disable_inherited_startup_and_health_behavior():
    ctx = context()
    for model in models(ctx):
        service = next(iter(model["services"].values()))
        assert service["image"] == dict(ctx.images)["original"]
        assert service["pull_policy"] == "never" and service["restart"] == "no"
        assert service["healthcheck"] == {"disable": True}
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert not {
            "ports",
            "expose",
            "build",
            "environment",
            "env_file",
            "secrets",
            "privileged",
            "cap_add",
            "devices",
            "links",
            "external_links",
            "pid",
            "ipc",
            "depends_on",
        } & set(service)
        assert "/app/start.sh" not in str(service["entrypoint"])


def test_relative_outputs_are_deterministic_detached_bounded_and_context_attributed():
    ctx = context()
    binding = ctx.journal_binding()
    before = copy.deepcopy(binding)
    first = auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding)
    second = auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding)
    assert first == second and first is not second and binding == before
    assert set(first) == {
        auxiliary.REFERENCE_COMPOSE,
        auxiliary.PROBE_COMPOSE,
        auxiliary.PROBE_SCRIPT,
        auxiliary.REFERENCE_SEED,
    }
    assert sum(map(len, first.values())) < 8 * 1024
    for name, data in first.items():
        path = PurePosixPath(name)
        assert str(path) == name and not path.is_absolute() and ".." not in path.parts
        assert type(data) is bytes and data.endswith(b"\n") and b"\r" not in data
    assert (
        first[auxiliary.REFERENCE_SEED]
        == (
            "synthetic-reference-generation\nnot-an-initialized-working-data-snapshot\n"
            "context_sha256=" + ctx.digest() + "\n"
        ).encode()
    )
    first.clear()
    assert inputs(ctx) == second


@pytest.mark.parametrize(
    "field,value",
    [
        ("scenario", "configuration-conflict"),
        ("context_id", "c" * 32),
        ("source_commit", "d" * 40),
        ("engine_id", "changed-engine"),
    ],
)
def test_reference_seed_tracks_generation_and_complete_binding(field, value):
    ctx = context()
    changed = replace(ctx, **{field: value})
    assert inputs(ctx)[auxiliary.REFERENCE_SEED] != inputs(changed)[auxiliary.REFERENCE_SEED]
    assert changed.digest().encode() in inputs(changed)[auxiliary.REFERENCE_SEED]


def test_initial_fixture_inputs_and_policy_are_preserved():
    ctx = context()
    binding = ctx.journal_binding()
    initial = fixtures.original_fixture_inputs(ctx, lab_binding=binding)
    original_model = yaml.safe_load(initial[fixtures.COMPOSE])
    planned, receipt = policy.plan_networks(original_model, lab_context=ctx)
    auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding)
    assert initial == fixtures.original_fixture_inputs(ctx, lab_binding=binding)
    assert policy.plan_networks(original_model, lab_context=ctx) == (planned, receipt)
    assert receipt["release_gate_satisfied"] is False


def test_generator_has_no_filesystem_process_network_or_environment_operations(monkeypatch):
    def unexpected(*_, **__):
        raise AssertionError("Host operation attempted")

    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "open", unexpected)
        isolated.setattr(os, "open", unexpected)
        isolated.setattr(os, "getenv", unexpected)
        isolated.setattr(Path, "open", unexpected)
        isolated.setattr(Path, "resolve", unexpected)
        isolated.setattr(subprocess, "run", unexpected)
        isolated.setattr(subprocess, "Popen", unexpected)
        isolated.setattr(socket, "socket", unexpected)
        result = inputs()
    assert result[auxiliary.PROBE_SCRIPT]


@pytest.mark.parametrize(
    "field,value",
    [
        ("vm_uuid", "11111111-2222-3333-4444-555555555555"),
        ("vm_name", "codex-grok-lab-other"),
        ("engine_id", "changed-engine"),
        ("source_commit", "f" * 40),
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
def test_full_context_mismatch_refuses_before_rendering(field, value, monkeypatch):
    ctx = context()
    changed = replace(ctx, **{field: value})
    assert changed.resource_map() == ctx.resource_map()
    monkeypatch.setattr(
        auxiliary.yaml, "safe_dump", lambda *_, **__: pytest.fail("Unexpected render")
    )
    with pytest.raises(auxiliary.LabAuxiliaryError):
        auxiliary.auxiliary_fixture_inputs(changed, lab_binding=ctx.journal_binding())


@pytest.mark.parametrize("binding", [None, {}, True, "private-invalid-binding"])
def test_invalid_binding_refuses_without_private_details(binding):
    with pytest.raises(auxiliary.LabAuxiliaryError) as caught:
        auxiliary.auxiliary_fixture_inputs(context(), lab_binding=binding)
    assert "private-invalid-binding" not in str(caught.value)


@pytest.mark.parametrize("value", [None, {}, True, record()])
def test_exact_descriptor_is_required(value):
    with pytest.raises(auxiliary.LabAuxiliaryError):
        auxiliary.auxiliary_fixture_inputs(value, lab_binding=context().journal_binding())


def test_context_mutation_during_serialization_refuses(monkeypatch):
    ctx = context()
    binding = ctx.journal_binding()
    dump = auxiliary.yaml.safe_dump

    def changed(*args, **kwargs):
        binding["context"]["source_commit"] = "c" * 40
        return dump(*args, **kwargs)

    monkeypatch.setattr(auxiliary.yaml, "safe_dump", changed)
    with pytest.raises(auxiliary.LabAuxiliaryError):
        auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding)


def test_api_and_outputs_contain_no_targets_credentials_or_admission_controls():
    ctx = context()
    for name, value in {
        "target": "192.0.2.1",
        "port": 8443,
        "network": "host",
        "image": "unreviewed",
        "approved": True,
        "root": "/mnt/s",
    }.items():
        with pytest.raises(TypeError):
            auxiliary.auxiliary_fixture_inputs(
                ctx, lab_binding=ctx.journal_binding(), **{name: value}
            )
    bundle = inputs(ctx)
    all_bytes = b"\n".join(bundle.values())
    for forbidden in (
        b"192.0.2.1",
        b"grok.com",
        b"voipguru.org",
        b"/mnt/s",
        b"/home/dblagbro",
        b"/run/docker.sock",
        b"BRIDGE_TOKEN",
        b"execution_admission",
        b"operator_deployment_approved",
    ):
        assert forbidden not in all_bytes
    assert not any("journal" in path or "state." in path for path in bundle)
