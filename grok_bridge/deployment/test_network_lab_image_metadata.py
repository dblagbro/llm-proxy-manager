"""Fake daemon/socket metadata tests only; no image execution or live collector I/O."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import network_lab_image_metadata as metadata
import network_lab_storage as storage
import pytest
from test_network_lab_bootstrap import engine as engine
from test_network_lab_storage import response
from test_network_lab_storage import unix_http as unix_http

_GET = storage._engine_get
_PLATFORM = metadata._platform
CANARY = "PRIVATE_CANARY_NOT_FOR_OUTPUT"


@pytest.fixture
def daemon(engine, monkeypatch):
    monkeypatch.setattr(metadata, "_platform", lambda: None)
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: engine.now))
    for role, pin in engine.ctx.images:
        engine.replies["/images/" + pin + "/json"].update(
            RepoTags=["mutable:first"],
            RepoDigests=["mutable@sha256:" + "f" * 64],
            Metadata={"LastTagTime": "2026-11-15T00:00:00Z"},
            Config={
                "Env": ["PATH=/usr/local/bin:/usr/bin", "PRIVATE_VALUE=" + CANARY],
                "User": "",
                "WorkingDir": "/private/working",
                "StopSignal": "SIGTERM",
                "Entrypoint": ["/private/entrypoint", CANARY],
                "Cmd": [CANARY],
                "Shell": ["/bin/sh", "-c"],
                "Volumes": {},
                "ExposedPorts": {"8443/tcp": {}},
                "Labels": {"private-label": CANARY},
                "Healthcheck": None,
                "OnBuild": [],
                "ArgsEscaped": True if role != "nginx" else None,
            },
        )
    # Metadata observation deliberately permits existing resources and ignores
    # this inventory; it must not accidentally call bootstrap or enumerate them.
    engine.replies["/info"].update(Containers=12, ContainersRunning=5, ContainersStopped=7)
    engine.replies["/containers/json?all=1"] = [{"Id": "a" * 64, "State": "exited"}]
    return engine


def collect(daemon):
    return metadata.collect(daemon.ctx, lab_binding=daemon.binding)


def image(daemon, role="original"):
    return daemon.replies["/images/" + dict(daemon.ctx.images)[role] + "/json"]


def test_only_three_pins_are_sampled_twice_with_one_deadline_and_no_resource_listing(daemon):
    result = collect(daemon)
    sample = ["/info", *["/images/" + pin + "/json" for _, pin in daemon.ctx.images], "/info"]
    assert [route for route, _ in daemon.requests] == sample * 2
    assert {deadline for _, deadline in daemon.requests} == {130.0}
    assert daemon.opened == daemon.closed == 1
    assert result["metadata_before_sha256"] == result["metadata_after_sha256"]
    assert result["images"]["original"] == {
        "config_sha256": metadata.capacity._digest(image(daemon)["Config"]),
        "environment_entry_count": 2,
        "declared_volume_count": 0,
        "healthcheck_present": False,
        "onbuild_count": 0,
        "entrypoint_count": 2,
        "command_count": 1,
        "args_escaped": True,
    }
    assert all(
        result[key] is False
        for key in (
            "execution_admission",
            "compatibility_verified",
            "material_safety_verified",
        )
    )
    public = json.dumps(result)
    for private in (
        CANARY,
        "PRIVATE_VALUE",
        "/private",
        "private-label",
        "mutable:first",
        daemon.ctx.engine_id,
        "/var/lib/docker",
        "/run",
    ):
        assert private not in public


def test_nonempty_declarations_are_reported_without_compatibility_or_safety_approval(daemon):
    image(daemon)["Config"].update(
        Volumes={"/data": {}, "/another": {}},
        OnBuild=["RUN " + CANARY],
        Healthcheck={
            "Test": ["CMD", CANARY],
            "Interval": 1000,
            "Timeout": 2000,
            "Retries": 2,
            "StartPeriod": 3000,
            "StartInterval": 4000,
        },
    )
    result = collect(daemon)
    summary = result["images"]["original"]
    assert summary["declared_volume_count"] == 2 and summary["onbuild_count"] == 1
    assert summary["healthcheck_present"] is True
    assert result["compatibility_verified"] is result["material_safety_verified"] is False
    assert CANARY not in json.dumps(result)


def test_absent_and_null_config_fields_keep_distinct_complete_hashes(daemon):
    image(daemon)["Config"] = {}
    absent = collect(daemon)["images"]["original"]
    image(daemon)["Config"] = {"ArgsEscaped": None, "Env": None, "Volumes": None}
    null = collect(daemon)["images"]["original"]
    assert absent["config_sha256"] != null["config_sha256"]
    assert {key: value for key, value in absent.items() if key != "config_sha256"} == {
        key: value for key, value in null.items() if key != "config_sha256"
    }


def test_mutable_tag_and_repository_digest_metadata_is_not_content_drift(daemon):
    def mutate(route, count, _leaf):
        if count > 5 and route.startswith("/images/"):
            daemon.replies[route].update(
                RepoTags=["mutable:second"],
                RepoDigests=[],
                Metadata={"LastTagTime": "2026-11-15T03:00:00Z"},
            )

    daemon.hook = mutate
    assert collect(daemon)["execution_admission"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("Id", "sha256:" + "f" * 64),
        ("Os", "windows"),
        ("Architecture", "arm64"),
        ("Variant", "v8"),
        ("Config", None),
        ("Config", []),
        ("Config", False),
    ],
)
def test_wrong_image_pin_platform_or_missing_config_refuses(daemon, field, value):
    image(daemon)[field] = value
    with pytest.raises(metadata.LabImageMetadataError):
        collect(daemon)
    assert daemon.closed == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("ArgsEscaped", 1),
        ("Env", "KEY=value"),
        ("Env", ["KEY=a", "KEY=b"]),
        ("Env", ["=value"]),
        ("Env", ["NO_SEPARATOR"]),
        ("Env", ["BAD KEY=value"]),
        ("Env", ["NUL=\0"]),
        ("Env", [False]),
        ("User", 0),
        ("WorkingDir", []),
        ("StopSignal", True),
        ("Entrypoint", {}),
        ("Cmd", [1]),
        ("Shell", False),
        ("Volumes", []),
        ("Volumes", {"/data": True}),
        ("ExposedPorts", {"80/tcp": None}),
        ("Labels", {"key": 1}),
        ("Labels", {"": "value"}),
        ("OnBuild", [None]),
        ("Healthcheck", []),
        ("Healthcheck", {"Unknown": 0}),
        ("Healthcheck", {"Test": "CMD true"}),
        ("Healthcheck", {"Retries": False}),
        ("Healthcheck", {"Timeout": 1.0}),
        ("Healthcheck", {"Interval": -1}),
        ("Healthcheck", {"StartPeriod": 2**63}),
        ("UnreviewedConfigField", "unknown"),
    ],
)
def test_strict_config_types_env_syntax_and_closed_fields_refuse(daemon, field, value):
    image(daemon)["Config"][field] = value
    with pytest.raises(metadata.LabImageMetadataError) as caught:
        collect(daemon)
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Cmd", ["x"] * 257),
        ("WorkingDir", "x" * 4097),
        ("WorkingDir", "é" * 3000),
        ("Labels", {str(i): "x" for i in range(257)}),
        ("Labels", {str(i): "x" * 4096 for i in range(17)}),
        ("Labels", {str(i): "\n" * 4096 for i in range(8)}),
    ],
)
def test_collection_bounds_refuse_before_large_configuration_can_escape(daemon, field, value):
    image(daemon)["Config"][field] = value
    with pytest.raises(metadata.LabImageMetadataError):
        collect(daemon)


@pytest.mark.parametrize("value", [None, {}, False, "lab"])
def test_invalid_binding_refuses_before_any_host_io(daemon, value):
    with pytest.raises(metadata.LabImageMetadataError):
        metadata.collect(daemon.ctx, lab_binding=value)
    assert daemon.opened == 0 and daemon.requests == []


def test_same_namespace_wrong_full_binding_refuses_before_host_io(daemon):
    other = replace(daemon.ctx, source_commit="f" * 40)
    assert other.project == daemon.ctx.project
    with pytest.raises(metadata.LabImageMetadataError):
        metadata.collect(other, lab_binding=daemon.binding)
    assert daemon.opened == 0


@pytest.mark.parametrize(
    "change", ["config", "socket", "parents", "peer", "engine", "root", "context", "binding"]
)
def test_observed_or_input_binding_drift_refuses_and_always_closes(daemon, change):
    def mutate(route, count, leaf):
        if count == 7:
            if change == "config":
                daemon.replies[route]["Config"]["Env"].append("NEW=value")
            elif change == "socket":
                leaf.identity = (2, 3, 4)
            elif change == "parents":
                leaf.parent.bindings = []
            elif change == "peer":
                daemon.peer = (99, 0, 0)
            elif change == "engine":
                daemon.replies["/info"]["ID"] = "other-engine"
            elif change == "root":
                daemon.replies["/info"]["DockerRootDir"] = "/other/docker"
            elif change == "context":
                object.__setattr__(daemon.ctx, "source_commit", "f" * 40)
            else:
                daemon.binding["context"]["source_commit"] = "f" * 40

    daemon.hook = mutate
    with pytest.raises(metadata.LabImageMetadataError):
        collect(daemon)
    assert daemon.closed == 1


@pytest.mark.parametrize(
    "platform,uid,arch",
    [
        ("win32", 0, "x86_64"),
        ("linux", 1000, "x86_64"),
        ("linux", 0, "aarch64"),
    ],
)
def test_real_platform_guard_is_not_bypassed_by_descriptor(
    daemon, monkeypatch, platform, uid, arch
):
    monkeypatch.setattr(metadata, "_platform", _PLATFORM)
    monkeypatch.setattr(metadata, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        metadata,
        "os",
        SimpleNamespace(geteuid=lambda: uid, uname=lambda: SimpleNamespace(machine=arch)),
    )
    with pytest.raises(metadata.LabImageMetadataError):
        collect(daemon)
    assert daemon.opened == 0 and daemon.requests == []


def test_result_and_inputs_are_detached_and_no_raw_transport_error_is_exposed(daemon):
    before = copy.deepcopy((daemon.binding, daemon.replies))
    result = collect(daemon)
    result["images"]["original"]["environment_entry_count"] = 0
    assert (daemon.binding, daemon.replies) == before
    assert collect(daemon)["images"]["original"]["environment_entry_count"] == 2

    def fail(*_):
        raise RuntimeError(CANARY)

    daemon.hook = fail
    with pytest.raises(metadata.LabImageMetadataError) as caught:
        collect(daemon)
    assert CANARY not in str(caught.value)
    assert daemon.opened == daemon.closed


def test_deadline_is_checked_after_final_socket_guard(daemon):
    def expire():
        if len(daemon.requests) == 10:
            daemon.now = 130

    daemon.verify_hook = expire
    with pytest.raises(metadata.LabImageMetadataError):
        collect(daemon)
    assert daemon.closed == 1


@pytest.mark.parametrize(
    "fault", [None, "slow-total", "duplicate", "redirect", "peer", "oversized"]
)
def test_ten_requests_use_real_fixed_framed_transport_with_only_fake_sockets(
    daemon, unix_http, monkeypatch, fault
):
    routes = []

    def framed(leaf, route, *, deadline):
        routes.append(route)
        body = json.dumps(daemon.replies[route]).encode()
        headers = None
        if fault == "duplicate" and route.startswith("/images/"):
            body = body.replace(
                b'"ArgsEscaped": true', b'"ArgsEscaped": true, "ArgsEscaped": false'
            )
        if fault == "oversized" and route.startswith("/images/"):
            headers = b"Content-Length: 999999999\r\n"
        if fault == "peer" and len(routes) == 6:
            unix_http.peer = (99, 0, 0)
        status = b"302 Redirect" if fault == "redirect" else b"200 OK"
        unix_http.wire = response(body, headers=headers, status=status)
        return _GET(leaf, route, deadline=deadline)

    monkeypatch.setattr(storage, "_engine_get", framed)
    monkeypatch.setattr(metadata, "time", storage.time)
    if fault == "slow-total":
        unix_http.tick = 3.5
    if fault is None:
        assert collect(daemon)["execution_admission"] is False
        assert len(routes) == len(unix_http.sent) == 10
        assert all(
            request.startswith(b"GET ") and b"Authorization:" not in request
            for request in unix_http.sent
        )
    else:
        with pytest.raises(metadata.LabImageMetadataError):
            collect(daemon)
        assert len(routes) <= 10
    if fault == "slow-total":
        assert 5 < len(routes) < 10 and unix_http.now <= metadata.TOTAL_SECONDS + 3.5
    assert daemon.closed == 1


def test_no_public_endpoint_or_transport_override_exists(daemon):
    with pytest.raises(TypeError):
        metadata.collect(daemon.ctx, lab_binding=daemon.binding, transport=lambda: None)
    assert daemon.opened == 0
