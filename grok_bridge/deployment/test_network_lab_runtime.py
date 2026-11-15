"""Supplied runtime-record consistency only; no Docker, health or host admission."""

import copy
from dataclasses import replace
from pathlib import Path, PurePosixPath

import network_lab_context as lab
import network_policy as policy
import pytest
import release as r
from test_network_lab_context import record

ORIGINAL_ID = "1" * 64
CANDIDATE_ID = "2" * 64
ROLLBACK_ID = "3" * 64
DEFAULT_ID = "a" * 64
DEDICATED_ID = "b" * 64
REFUSALS = (r.Refused, policy.NetworkPolicyError)


def fixture(role="candidate"):
    ctx = lab.from_record(record())
    pins, names = dict(ctx.images), ctx.resource_map()
    old = {
        "Id": ORIGINAL_ID,
        "Name": "/" + names[r.SERVICE],
        "Image": pins["original"],
        "RestartCount": 2,
        "State": {"Running": False, "Status": "exited"},
        "Config": {
            "Cmd": ["synthetic-server"],
            "Entrypoint": ["synthetic-entrypoint"],
            "Healthcheck": {"Test": ["CMD", "synthetic-health"]},
            "Env": [
                "BRIDGE_TOKEN=synthetic-only",
                "BRIDGE_PUBLIC_PATH=/grok-bridge",
                "BRIDGE_PUBLIC_ORIGIN=https://old-lab.invalid",
                "KEEP=unchanged",
            ],
            "Labels": {
                "com.docker.compose.project": ctx.project,
                "com.docker.compose.service": r.SERVICE,
            },
        },
        "HostConfig": {
            "NetworkMode": names[policy.DEFAULT_NAME],
            "Memory": 128,
            "MemorySwap": 256,
            "NanoCpus": 500,
            "CpuShares": 16,
            "PidsLimit": 64,
            "RestartPolicy": {"Name": "no"},
            "ShmSize": 128,
            "Privileged": False,
            "CapAdd": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "PortBindings": {},
        },
        "NetworkSettings": {
            "Networks": {names[policy.DEFAULT_NAME]: {"NetworkID": DEFAULT_ID}},
            "Ports": {},
        },
        "Mounts": [
            {
                "Destination": "/data",
                "Type": "volume",
                "Name": "synthetic-original",
                "Source": "/custom/docker/volumes/synthetic-original/_data",
                "RW": True,
                "Driver": "local",
            },
            {
                "Destination": "/unchanged",
                "Type": "bind",
                "Source": str(ctx.configuration_root / "unchanged"),
                "RW": False,
            },
        ],
    }
    live = copy.deepcopy(old)
    live["Id"] = CANDIDATE_ID if role == "candidate" else ROLLBACK_ID
    live["RestartCount"] = 0
    live["Image"] = pins[role]
    data = old["Mounts"][0]["Source"]
    network_id = None
    if role == "candidate":
        data = str(ctx.generation_root / "candidate-data")
        network_id = DEDICATED_ID
        live["HostConfig"]["NetworkMode"] = names[policy.NETWORK_NAME]
        live["NetworkSettings"]["Networks"] = {
            names[policy.NETWORK_NAME]: {"NetworkID": DEDICATED_ID}
        }
        live["Mounts"][0] = {"Destination": "/data", "Type": "bind", "Source": data, "RW": True}
        env = dict(entry.split("=", 1) for entry in old["Config"]["Env"])
        env.update(
            BRIDGE_PUBLIC_ORIGIN="https://grok-lab.invalid",
            BRIDGE_AUTH_URL="http://llm-proxy2:3000/api/auth/me",
        )
        live["Config"]["Env"] = [key + "=" + value for key, value in env.items()]
    return {
        "old": old,
        "live": live,
        "data": data,
        "image": pins[role],
        "managed_environment": {"KEEP": "unchanged"},
        "network_id": network_id,
        "lab_context": ctx,
        "lab_binding": ctx.journal_binding(),
    }


@pytest.mark.parametrize("role", ["candidate", "original"])
@pytest.mark.parametrize("state", ["created", "running", "exited"])
def test_candidate_and_original_records_are_checked_without_health_or_running_admission(
    role, state
):
    arguments = fixture(role)
    arguments["live"]["State"] = {
        "Status": state,
        "Running": state == "running",
        "Health": {"Status": "unhealthy"},
    }
    before = copy.deepcopy(arguments)
    assert r.assert_runtime(**arguments) is None
    assert arguments == before


@pytest.mark.parametrize("role", ["candidate", "original"])
def test_lab_validation_is_lexical_without_filesystem_or_host_io(role, monkeypatch):
    arguments = fixture(role)

    def prohibited(*args, **kwargs):
        raise AssertionError("Lab comparison attempted I/O")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "resolve", prohibited)
        scoped.setattr(Path, "stat", prohibited)
        scoped.setattr(Path, "open", prohibited)
        scoped.setattr(Path, "read_bytes", prohibited)
        scoped.setattr(r.subprocess, "run", prohibited)
        assert r.assert_runtime(**arguments) is None


def test_same_original_identity_preserves_original_restart_count():
    arguments = fixture("original")
    arguments["live"] = copy.deepcopy(arguments["old"])
    r.assert_runtime(**arguments)
    arguments["live"]["RestartCount"] += 1
    with pytest.raises(r.Refused, match="restarted"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("field", ["vm_uuid", "vm_name", "engine_id", "source_commit", "images"])
def test_full_context_drift_refuses_same_namespace_before_record_comparison(field):
    arguments = fixture()
    ctx = arguments["lab_context"]
    changes = {
        "vm_uuid": "12345678-1234-1234-1234-123456789abd",
        "vm_name": "codex-grok-lab-other",
        "engine_id": "other-engine",
        "source_commit": "c" * 40,
        "images": tuple(
            (role, "sha256:" + "4" * 64 if role == "nginx" else pin) for role, pin in ctx.images
        ),
    }
    changed = replace(ctx, **{field: changes[field]})
    assert (
        changed.generation_root == ctx.generation_root
        and changed.resource_map() == ctx.resource_map()
    )
    arguments["lab_context"] = changed
    with pytest.raises(r.Refused, match="journal binding"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("bad", [None, {}, False, "binding", "schema", "digest", "extra"])
def test_supplied_binding_is_mandatory_and_exact(bad):
    arguments = fixture()
    if bad in ("schema", "digest", "extra"):
        key, value = {
            "schema": ("schema", "future"),
            "digest": ("context_sha256", "0" * 64),
            "extra": ("permit", True),
        }[bad]
        arguments["lab_binding"][key] = value
    else:
        arguments["lab_binding"] = bad
    before = copy.deepcopy(arguments)
    with pytest.raises(r.Refused, match="journal binding"):
        r.assert_runtime(**arguments)
    assert arguments == before


@pytest.mark.parametrize("bad", [{}, False, "lab", object()])
def test_malformed_context_is_refused(bad):
    arguments = fixture()
    arguments["lab_context"] = bad
    with pytest.raises(policy.NetworkPolicyError):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("side", ["old", "live"])
@pytest.mark.parametrize(
    "field,bad",
    [
        ("Id", "short"),
        ("Id", "A" * 64),
        ("Id", None),
        ("Name", "/llm-proxy2-grok-bridge"),
        ("Image", "mutable:latest"),
        ("project", "docker"),
        ("service", "other"),
    ],
)
def test_raw_names_ids_labels_and_images_are_bound(side, field, bad):
    arguments = fixture()
    target = arguments[side]
    if field in ("project", "service"):
        target["Config"]["Labels"]["com.docker.compose." + field] = bad
    else:
        target[field] = bad
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("field", ["Config", "HostConfig", "Mounts", "NetworkSettings"])
def test_malformed_raw_shapes_refuse_sanitized(field):
    arguments = fixture()
    arguments["live"][field] = None
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


def test_candidate_cannot_reuse_original_container_id_or_other_pinned_image_role():
    arguments = fixture()
    arguments["live"]["Id"] = arguments["old"]["Id"]
    with pytest.raises(r.Refused, match="reuse"):
        r.assert_runtime(**arguments)
    arguments = fixture()
    arguments["image"] = dict(arguments["lab_context"].images)["nginx"]
    arguments["live"]["Image"] = arguments["image"]
    with pytest.raises(r.Refused, match="not pinned"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("network_id", [None, "", "short", "A" * 64, False, DEFAULT_ID])
def test_candidate_has_no_default_network_fallback(network_id):
    arguments = fixture()
    arguments["network_id"] = network_id
    with pytest.raises(r.Refused, match="owned dedicated network ID"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("network_id", [DEDICATED_ID, "", False])
def test_original_runtime_rejects_any_candidate_network_id(network_id):
    arguments = fixture("original")
    arguments["network_id"] = network_id
    with pytest.raises(r.Refused, match="cannot use a candidate network ID"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "change",
    [
        "candidate-default",
        "candidate-extra",
        "candidate-wrong-id",
        "candidate-mode",
        "original-name",
        "original-id",
        "original-mode",
        "published",
        "malformed-endpoint",
    ],
)
def test_network_scope_and_original_network_identity_refuse_drift(change):
    arguments = fixture()
    names = arguments["lab_context"].resource_map()
    current = arguments["live"]["NetworkSettings"]["Networks"]
    original = arguments["old"]["NetworkSettings"]["Networks"]
    if change == "candidate-default":
        arguments["live"]["NetworkSettings"]["Networks"] = copy.deepcopy(original)
    elif change == "candidate-extra":
        current[names[policy.DEFAULT_NAME]] = {"NetworkID": DEFAULT_ID}
    elif change == "candidate-wrong-id":
        current[names[policy.NETWORK_NAME]]["NetworkID"] = "c" * 64
    elif change == "candidate-mode":
        arguments["live"]["HostConfig"]["NetworkMode"] = names[policy.DEFAULT_NAME]
    elif change == "original-name":
        arguments["old"]["NetworkSettings"]["Networks"] = {
            "docker_default": {"NetworkID": DEFAULT_ID}
        }
    elif change == "original-id":
        original[names[policy.DEFAULT_NAME]]["NetworkID"] = ""
    elif change == "original-mode":
        arguments["old"]["HostConfig"]["NetworkMode"] = "docker_default"
    elif change == "published":
        arguments["live"]["NetworkSettings"]["Ports"] = {"8443/tcp": [{"HostPort": "8443"}]}
    else:
        current[names[policy.NETWORK_NAME]] = None
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("role", ["candidate", "original"])
@pytest.mark.parametrize("side", ["old", "live"])
@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong-type", "rw-false", "rw-integer"])
def test_data_mount_cardinality_type_and_exact_rw_refuse(role, side, change):
    arguments = fixture(role)
    mounts = arguments[side]["Mounts"]
    if change == "missing":
        mounts.pop(0)
    elif change == "duplicate":
        mounts.append(copy.deepcopy(mounts[0]))
    elif change == "wrong-type":
        mounts[0]["Type"] = "bind" if mounts[0]["Type"] == "volume" else "volume"
    elif change == "rw-false":
        mounts[0]["RW"] = False
    else:
        mounts[0]["RW"] = 1
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("name", [None, "", "/named/volume", "${VOLUME}", False])
def test_original_named_volume_name_is_required(name):
    arguments = fixture()
    arguments["old"]["Mounts"][0]["Name"] = name
    with pytest.raises(r.Refused, match="named volume"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Name", "other-volume"),
        ("Source", "/different/store/_data"),
        ("RW", False),
        ("Type", "bind"),
    ],
)
def test_rollback_mount_must_match_retained_name_source_type_rw(field, value):
    arguments = fixture("original")
    arguments["live"]["Mounts"][0][field] = value
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


def test_rollback_backing_path_is_not_rewritten_or_assumed_under_default_docker_root():
    arguments = fixture("original")
    source = "/guest-local/custom-store/volumes/retained/_data"
    arguments["data"] = source
    for side in ("old", "live"):
        arguments[side]["Mounts"][0]["Source"] = source
    r.assert_runtime(**arguments)
    arguments["data"] = "/different/_data"
    with pytest.raises(r.Refused, match="named-volume binding"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "variant",
    [
        "relative",
        "root",
        "traversal",
        "dot",
        "double-slash",
        "trailing-slash",
        "path-object",
        "outside",
    ],
)
def test_candidate_paths_are_exact_canonical_strings(variant):
    arguments = fixture()
    good = arguments["data"]
    bad = {
        "relative": "candidate-data",
        "root": "/",
        "traversal": good + "/../candidate-data",
        "dot": good.replace("/candidate-data", "/./candidate-data"),
        "double-slash": good.replace("/candidate-data", "//candidate-data"),
        "trailing-slash": good + "/",
        "path-object": PurePosixPath(good),
        "outside": "/private/candidate-data",
    }[variant]
    arguments["data"] = bad
    arguments["live"]["Mounts"][0]["Source"] = str(bad)
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "source",
    [
        "relative",
        "/",
        "/store/../original",
        "/store//original",
        "/store/./original",
        "/store/original/",
        "//store/original",
    ],
)
def test_matching_rollback_path_aliases_still_refuse_without_resolution(source):
    arguments = fixture("original")
    arguments["data"] = source
    for side in ("old", "live"):
        arguments[side]["Mounts"][0]["Source"] = source
    with pytest.raises(r.Refused, match="canonical"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize("field", ["Cmd", "Entrypoint", "Healthcheck"])
def test_shared_command_checks_remain_active(field):
    arguments = fixture()
    arguments["live"]["Config"][field] = "changed"
    with pytest.raises(r.Refused, match="Command/entrypoint/healthcheck"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "field",
    [
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "CpuShares",
        "PidsLimit",
        "RestartPolicy",
        "ShmSize",
        "Privileged",
        "CapAdd",
        "CapDrop",
        "SecurityOpt",
        "ReadonlyRootfs",
        "PortBindings",
    ],
)
def test_shared_security_and_limit_checks_remain_active(field):
    arguments = fixture()
    arguments["live"]["HostConfig"][field] = "changed"
    with pytest.raises(r.Refused, match="limits/security"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "section,field,original,changed",
    [
        ("Config", "Cmd", ["synthetic", 0], ["synthetic", False]),
        ("Config", "Entrypoint", ["synthetic", 128], ["synthetic", 128.0]),
        (
            "Config",
            "Healthcheck",
            {"Test": ["CMD", "synthetic"], "Interval": 128},
            {"Test": ["CMD", "synthetic"], "Interval": 128.0},
        ),
        ("Config", "Healthcheck", {"Disable": False}, {"Disable": 0}),
        ("HostConfig", "Memory", 128, 128.0),
        ("HostConfig", "Privileged", False, 0),
        (
            "HostConfig",
            "RestartPolicy",
            {"Name": "no", "MaximumRetryCount": 0},
            {"Name": "no", "MaximumRetryCount": False},
        ),
    ],
)
def test_lab_configuration_compares_recursive_json_types_only_in_lab(
    section, field, original, changed
):
    # Adversarial supplied JSON tests comparison fidelity, not command-schema validity.
    assert original == changed and not policy._same_json(original, changed)
    arguments = fixture()
    for side in ("old", "live"):
        arguments[side][section][field] = copy.deepcopy(original)
    r.assert_runtime(**arguments)
    arguments["live"][section][field] = copy.deepcopy(changed)
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)
    production, _ = production_fixture()
    production["old"][section][field] = copy.deepcopy(original)
    production["live"][section][field] = copy.deepcopy(changed)
    r.assert_runtime(**production)


@pytest.mark.parametrize(
    "field,value",
    [("Source", "/other"), ("Destination", "/other"), ("Type", "volume"), ("RW", True)],
)
def test_unrelated_mount_comparison_remains_active(field, value):
    arguments = fixture()
    arguments["live"]["Mounts"][1][field] = value
    with pytest.raises(r.Refused, match="Unrelated mount"):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "key,value",
    [
        ("BRIDGE_TOKEN", "changed"),
        ("BRIDGE_PUBLIC_PATH", "/changed"),
        ("BRIDGE_PUBLIC_ORIGIN", "https://www.voipguru.org"),
        ("BRIDGE_AUTH_URL", "http://outside.invalid"),
        ("KEEP", "changed"),
    ],
)
def test_token_managed_environment_and_fixed_lab_origin_are_preserved(key, value):
    arguments = fixture()
    environment = dict(entry.split("=", 1) for entry in arguments["live"]["Config"]["Env"])
    environment[key] = value
    arguments["live"]["Config"]["Env"] = [key + "=" + value for key, value in environment.items()]
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


@pytest.mark.parametrize(
    "change", ["duplicate", "no-equals", "missing-token", "empty-token", "managed-list"]
)
def test_ambiguous_environment_does_not_silently_collapse(change):
    arguments = fixture()
    if change == "duplicate":
        arguments["live"]["Config"]["Env"].append("BRIDGE_TOKEN=synthetic-only")
    elif change == "no-equals":
        arguments["live"]["Config"]["Env"].append("INVALID")
    elif change == "managed-list":
        arguments["managed_environment"] = []
    else:
        for side in ("old", "live"):
            env = [e for e in arguments[side]["Config"]["Env"] if not e.startswith("BRIDGE_TOKEN=")]
            if change == "empty-token":
                env.append("BRIDGE_TOKEN=")
            arguments[side]["Config"]["Env"] = env
    with pytest.raises(r.Refused):
        r.assert_runtime(**arguments)


def production_fixture():
    arguments = fixture()
    ctx = arguments.pop("lab_context")
    arguments.pop("lab_binding")
    arguments["image"] = r.CANDIDATE
    arguments["data"] = "/private/candidate-data"
    for side, image, physical_network, network_id in (
        ("old", r.ORIGINAL, policy.DEFAULT_NAME, DEFAULT_ID),
        ("live", r.CANDIDATE, policy.NETWORK_NAME, DEDICATED_ID),
    ):
        item = arguments[side]
        item["Name"] = "/" + r.SERVICE
        item["Image"] = image
        item["Config"]["Labels"]["com.docker.compose.project"] = "docker"
        item["HostConfig"]["NetworkMode"] = physical_network
        item["NetworkSettings"]["Networks"] = {physical_network: {"NetworkID": network_id}}
        item["Mounts"][1]["Source"] = "/unchanged"
    arguments["live"]["Mounts"][0]["Source"] = arguments["data"]
    environment = dict(entry.split("=", 1) for entry in arguments["live"]["Config"]["Env"])
    environment.update(r.ENV_ADDITIONS)
    arguments["live"]["Config"]["Env"] = [key + "=" + value for key, value in environment.items()]
    return arguments, ctx


def test_production_default_preserves_existing_resolution_and_comparison(monkeypatch):
    arguments, _ = production_fixture()
    calls = []
    resolve = Path.resolve

    def observed(path, *args, **kwargs):
        calls.append(str(path))
        return resolve(path, *args, **kwargs)

    before = copy.deepcopy(arguments)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "resolve", observed)
        r.assert_runtime(**arguments)
        r.assert_runtime(**arguments, lab_context=None, lab_binding=None)
    assert len(calls) == 4 and arguments == before


@pytest.mark.parametrize("marker", ["binding", "name", "project", "data", "mount"])
def test_production_refuses_lab_markers_before_filesystem_resolution(marker, monkeypatch):
    arguments, ctx = production_fixture()
    if marker == "binding":
        arguments["lab_binding"] = ctx.journal_binding()
    elif marker == "name":
        arguments["live"]["Name"] = "/" + ctx.resource_map()[r.SERVICE]
    elif marker == "project":
        arguments["old"]["Config"]["Labels"]["com.docker.compose.project"] = ctx.project
    elif marker == "data":
        arguments["data"] = str(ctx.generation_root / "candidate-data")
    else:
        arguments["live"]["Mounts"][0]["Source"] = str(ctx.generation_root / "candidate-data")

    def prohibited(*args, **kwargs):
        raise AssertionError("Lab input reached production filesystem resolution")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "resolve", prohibited)
        with pytest.raises(r.Refused, match="requires its exact context"):
            r.assert_runtime(**arguments)
