"""Synthetic daemon observations and framed fake-socket HTTP; no live Docker I/O."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import network_lab_bootstrap as bootstrap
import network_lab_context as lab
import network_lab_storage as storage
import pytest
from test_network_lab_context import record
from test_network_lab_storage import FakeLeaf, response
from test_network_lab_storage import unix_http as unix_http

_GET = storage._engine_get
_PLATFORM = bootstrap._platform


def context():
    return lab.from_record(record())


def network(name, digit):
    return {
        "Name": name,
        "Id": digit * 64,
        "Scope": "local",
        "Driver": {"bridge": "bridge", "host": "host", "none": "null"}[name],
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "ConfigOnly": False,
        "IPAM": {"Driver": "default", "Config": []},
        "Options": {"com.docker.network.bridge.default_bridge": "true"} if name == "bridge" else {},
        "Containers": {},
        "Labels": {},
    }


@pytest.fixture
def engine(monkeypatch):
    ctx = context()
    networks = [network(name, str(i)) for i, name in enumerate(("bridge", "host", "none"), 4)]
    info = {
        "ID": ctx.engine_id,
        "DockerRootDir": "/var/lib/docker",
        "Driver": "overlay2",
        "DriverStatus": [["Backing Filesystem", "extfs"]],
        "OSType": "linux",
        "Architecture": "x86_64",
        "Containers": 0,
        "ContainersRunning": 0,
        "ContainersPaused": 0,
        "ContainersStopped": 0,
        "Swarm": {
            "LocalNodeState": "inactive",
            "NodeID": "",
            "NodeAddr": "",
            "ControlAvailable": False,
            "RemoteManagers": None,
            "Error": "",
        },
        "Images": 91,  # Unrelated images are deliberately permitted/unobserved.
    }
    replies = {
        "/info": info,
        "/containers/json?all=1": [],
        "/volumes": {"Volumes": [], "Warnings": None},
        "/networks": [{k: v for k, v in item.items() if k != "Containers"} for item in networks],
        **{"/networks/" + item["Id"]: item for item in networks},
        **{
            "/images/" + pin + "/json": {
                "Id": pin,
                "Os": "linux",
                "Architecture": "amd64",
                "Config": {"Env": ["PRIVATE_FIXTURE=do-not-print"]},
            }
            for _, pin in ctx.images
        },
    }
    state = SimpleNamespace(
        ctx=ctx,
        binding=ctx.journal_binding(),
        replies=replies,
        requests=[],
        opened=0,
        closed=0,
        checks=0,
        now=100.0,
        peer=(42, 0, 0),
        hook=lambda *_: None,
        verify_hook=lambda: None,
    )

    class Leaf:
        def __init__(self, path, kind):
            assert path == storage.SOCKET and kind == "socket"
            state.opened += 1
            self.identity = (1, 2, 0o140660, 0, 0, 1)
            self.parent = SimpleNamespace(bindings=[{"private_path": "/run"}])

        def verify(self):
            state.checks += 1
            state.verify_hook()

        def close(self):
            state.closed += 1

    def get(leaf, route, *, deadline):
        state.requests.append((route, deadline))
        state.hook(route, len(state.requests), leaf)
        leaf.verify()
        return copy.deepcopy(state.replies[route]), state.peer

    clock = SimpleNamespace(monotonic=lambda: state.now)
    monkeypatch.setattr(bootstrap, "time", clock)
    monkeypatch.setattr(storage, "time", clock)
    monkeypatch.setattr(bootstrap, "_platform", lambda: None)
    monkeypatch.setattr(storage, "_Leaf", Leaf)
    monkeypatch.setattr(storage, "_engine_get", get)
    return state


def collect(engine):
    return bootstrap.collect(engine.ctx, lab_binding=engine.binding)


def test_two_complete_samples_inspect_exact_ids_and_only_required_images(engine):
    result = collect(engine)
    routes = [route for route, _ in engine.requests]
    assert len(routes) == 22 and routes[:11] == routes[11:]
    assert routes.count("/containers/json?all=1") == 2
    assert routes.count("/networks") == 2
    assert sum(route.startswith("/networks/") for route in routes) == 6
    assert sum(route.startswith("/images/") for route in routes) == 6
    assert "/images/json" not in routes
    assert len({deadline for _, deadline in engine.requests}) == 1
    assert result["inventory_before_sha256"] == result["inventory_after_sha256"]
    assert result["observed_counts"] == {
        "containers": 0,
        "volumes": 0,
        "networks": 3,
        "endpoints": 0,
    }
    assert result["execution_admission"] is False and result["unrelated_images_observed"] is False
    assert engine.opened == engine.closed == 1
    public = json.dumps(result)
    for private in (
        engine.ctx.engine_id,
        "/var/lib/docker",
        "PRIVATE_FIXTURE",
        "do-not-print",
        "/run",
    ):
        assert private not in public


@pytest.mark.parametrize("state", ["created", "running", "paused", "exited", "dead", "removing"])
def test_every_container_state_including_retained_fixtures_refuses(engine, state):
    engine.replies["/containers/json?all=1"] = [{"Id": "a" * 64, "State": state}]
    with pytest.raises(bootstrap.LabBootstrapError, match="zero containers"):
        collect(engine)
    assert len(engine.requests) == 2 and engine.closed == 1


@pytest.mark.parametrize("value", [None, {}, False, "", 0])
def test_incomplete_container_inventory_refuses(engine, value):
    engine.replies["/containers/json?all=1"] = value
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"Volumes": []},
        {"Warnings": []},
        {"Volumes": [{}], "Warnings": []},
        {"Volumes": [], "Warnings": ["private-daemon-warning"]},
        {"Volumes": False, "Warnings": []},
        {"Volumes": [], "Warnings": {}},
    ],
)
def test_volumes_and_warnings_fail_closed(engine, value):
    engine.replies["/volumes"] = value
    with pytest.raises(bootstrap.LabBootstrapError) as caught:
        collect(engine)
    assert "private-daemon-warning" not in str(caught.value)


def test_empty_nullable_volume_array_is_supported(engine):
    engine.replies["/volumes"]["Volumes"] = None
    assert collect(engine)["observed_counts"]["volumes"] == 0


@pytest.mark.parametrize(
    "field,value",
    [("LocalNodeState", v) for v in (None, "", "active", "pending", "locked", "error")]
    + [
        ("NodeID", "existing-node"),
        ("NodeAddr", "192.0.2.1"),
        ("Cluster", {}),
        ("Nodes", 1),
        ("Managers", False),
        ("ControlAvailable", True),
        ("ControlAvailable", 0),
        ("RemoteManagers", [{}]),
        ("Error", "private-error"),
    ],
)
def test_swarm_membership_or_ambiguous_state_refuses(engine, field, value):
    engine.replies["/info"]["Swarm"][field] = value
    with pytest.raises(bootstrap.LabBootstrapError, match="Swarm"):
        collect(engine)
    assert len(engine.requests) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("ID", "wrong-engine"),
        ("OSType", "windows"),
        ("OSType", None),
        ("Architecture", "aarch64"),
        ("Architecture", None),
        ("Containers", 1),
        ("Containers", False),
        ("ContainersRunning", 1),
        ("ContainersStopped", None),
        ("Driver", "overlayfs"),
        ("DriverStatus", [["driver-type", "io.containerd.snapshotter.v1"]]),
    ],
)
def test_engine_identity_platform_and_classic_store_refuse(engine, field, value):
    engine.replies["/info"][field] = value
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)
    assert len(engine.requests) == 1


@pytest.mark.parametrize(
    "case",
    [
        "extra",
        "missing",
        "duplicate-name",
        "duplicate-id",
        "custom",
        "short-id",
        "driver",
        "scope",
        "internal",
        "bool-type",
        "ipam",
        "default-bridge",
        "inspect-id",
        "inspect-name",
        "inspect-endpoint",
        "missing-endpoints",
        "list-drift",
    ],
)
def test_network_inventory_and_exact_inspections_refuse(engine, case):
    listed = engine.replies["/networks"]
    inspected = engine.replies["/networks/" + listed[0]["Id"]]
    if case == "extra":
        listed.append(copy.deepcopy(listed[0]))
    elif case == "missing":
        listed.pop()
    elif case == "duplicate-name":
        listed[1] = copy.deepcopy(listed[0])
    elif case == "duplicate-id":
        listed[1]["Id"] = listed[0]["Id"]
    elif case == "custom":
        listed[0]["Name"] = "lab-retained-custom"
    elif case == "short-id":
        listed[0]["Id"] = "123"
    elif case in {"driver", "scope", "internal", "bool-type", "ipam", "default-bridge"}:
        key, value = {
            "driver": ("Driver", "overlay"),
            "scope": ("Scope", "swarm"),
            "internal": ("Internal", True),
            "bool-type": ("ConfigOnly", 0),
            "ipam": ("IPAM", {"Driver": "other"}),
            "default-bridge": ("Options", {}),
        }[case]
        listed[0][key] = value
    elif case == "inspect-id":
        inspected["Id"] = "f" * 64
    elif case == "inspect-name":
        inspected.update(Name="host", Driver="host")
    elif case == "inspect-endpoint":
        inspected["Containers"] = {"f" * 64: {"EndpointID": "e" * 64}}
    elif case == "missing-endpoints":
        inspected.pop("Containers")
    else:
        inspected["Labels"] = {"changed": "yes"}
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)
    assert engine.closed == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("Id", "sha256:" + "f" * 64),
        ("Id", None),
        ("Os", "windows"),
        ("Architecture", "arm64"),
        ("Variant", "v8"),
        ("Architecture", None),
    ],
)
def test_image_identity_and_platform_refuse(engine, field, value):
    route = "/images/" + dict(engine.ctx.images)["original"] + "/json"
    engine.replies[route][field] = value
    with pytest.raises(bootstrap.LabBootstrapError, match="image"):
        collect(engine)


def test_missing_pinned_image_refuses_without_raw_error(engine):
    def missing(route, *_):
        if route.startswith("/images/"):
            raise OSError("private-daemon-missing-image-detail")

    engine.hook = missing
    with pytest.raises(bootstrap.LabBootstrapError) as caught:
        collect(engine)
    assert "private-daemon" not in str(caught.value) and engine.closed == 1


@pytest.mark.parametrize(
    "change",
    ["peer", "engine", "docker-root", "socket", "network", "image", "containers", "binding"],
)
def test_drift_between_samples_refuses(engine, change):
    def drift(route, count, leaf):
        if count != 12:
            return
        if change == "peer":
            engine.peer = (43, 0, 0)
        elif change == "engine":
            engine.replies["/info"]["ID"] = "replacement-engine"
        elif change == "docker-root":
            engine.replies["/info"]["DockerRootDir"] = "/var/lib/replacement"
        elif change == "socket":
            leaf.identity = (1, 99, 0o140660, 0, 0, 1)
        elif change == "network":
            for item in engine.replies["/networks"]:
                item["Labels"] = {"changed": "yes"}
                engine.replies["/networks/" + item["Id"]]["Labels"] = {"changed": "yes"}
        elif change == "image":
            key = "/images/" + dict(engine.ctx.images)["candidate"] + "/json"
            engine.replies[key]["Config"]["Env"] = ["changed"]
        elif change == "containers":
            engine.replies["/containers/json?all=1"] = [{"State": "created"}]
        else:
            engine.binding["context"]["source_commit"] = "f" * 40

    engine.hook = drift
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)
    assert engine.closed == 1


def test_network_list_reordering_does_not_rebase_identity(engine):
    engine.hook = lambda _, count, __: (
        engine.replies["/networks"].reverse() if count == 12 else None
    )
    assert collect(engine)["execution_admission"] is False


def test_one_total_deadline_covers_every_request_and_both_samples(engine):
    def slow(*_):
        engine.now += 2

    engine.hook = slow
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)
    assert len(engine.requests) == 15 and engine.closed == 1
    assert {deadline for _, deadline in engine.requests} == {130.0}


def test_final_socket_revalidation_refuses(engine):
    def changed():
        if len(engine.requests) == 22:
            raise OSError("private-path changed")

    engine.verify_hook = changed
    with pytest.raises(bootstrap.LabBootstrapError) as caught:
        collect(engine)
    assert "private-path" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine_id", "changed"),
        ("source_commit", "c" * 40),
        ("vm_uuid", "11111111-2222-3333-4444-555555555555"),
    ],
)
def test_full_context_drift_with_same_namespace_refuses_before_io(engine, field, value):
    other = replace(engine.ctx, **{field: value})
    assert other.namespace == engine.ctx.namespace
    with pytest.raises(bootstrap.LabBootstrapError):
        bootstrap.collect(other, lab_binding=engine.binding)
    assert engine.opened == 0 and engine.requests == []


@pytest.mark.parametrize("ctx", [None, {}, True])
def test_invalid_input_refuses_before_io(engine, ctx):
    with pytest.raises(bootstrap.LabBootstrapError):
        bootstrap.collect(ctx, lab_binding=engine.binding)
    assert engine.opened == 0 and engine.requests == []


@pytest.mark.parametrize(
    "route",
    [
        "/containers/create",
        "/images/create",
        "/info?x=1",
        "/version",
        "/images/json",
        "/networks/name",
        "/networks/" + "a" * 12,
        "/images/latest/json",
        "/info\r\nPOST /containers/create",
        None,
    ],
)
def test_shared_transport_refuses_any_unreviewed_route_before_io(route, unix_http):
    leaf = FakeLeaf()
    with pytest.raises(storage.LabStorageError, match="route"):
        storage._engine_get(leaf, route)
    assert unix_http.sent == [] and leaf.checks == 0


@pytest.mark.parametrize(
    "route",
    [
        "/containers/json?all=1",
        "/volumes",
        "/networks",
        "/networks/" + "a" * 64,
        "/images/sha256:" + "b" * 64 + "/json",
    ],
)
def test_shared_transport_sends_only_fixed_get_with_no_auth(route, unix_http):
    unix_http.wire = response(b"[]")
    data, peer = storage._engine_get(FakeLeaf(), route, deadline=1.5)
    assert data == [] and peer == (20, 0, 0)
    assert unix_http.sent == [
        ("GET " + route + " HTTP/1.1\r\nHost: localhost\r\n").encode()
        + b"Accept: application/json\r\nConnection: close\r\n\r\n"
    ]
    assert all(0 < timeout <= 1.5 for timeout in unix_http.timeouts)


@pytest.mark.parametrize("deadline", [False, 0, -1, "1", float("nan"), float("inf")])
def test_transport_rejects_invalid_outer_deadline_before_socket(deadline, unix_http):
    with pytest.raises(storage.LabStorageError, match="deadline"):
        storage._engine_get(FakeLeaf(), "/networks", deadline=deadline)
    assert unix_http.sent == []


def test_outer_deadline_cannot_be_reset_by_a_slow_response(unix_http):
    unix_http.tick, unix_http.chunk = 0.4, 1
    with pytest.raises(storage.LabStorageError, match="deadline"):
        storage._engine_get(FakeLeaf(), "/networks", deadline=1.0)
    assert unix_http.now <= 1.4


def test_public_collection_has_no_transport_or_endpoint_injection(engine):
    with pytest.raises(TypeError):
        bootstrap.collect(engine.ctx, lab_binding=engine.binding, route="/info")
    with pytest.raises(TypeError):
        bootstrap.collect(engine.ctx, lab_binding=engine.binding, transport=lambda: None)
    assert engine.opened == 0


@pytest.mark.parametrize("fault", [None, "slow-total", "malformed", "peer", "http-error"])
def test_bootstrap_uses_real_shared_framed_transport_with_bounded_fake_socket(
    engine, unix_http, monkeypatch, fault
):
    routes = []

    def framed(leaf, route, *, deadline):
        routes.append(route)
        body = json.dumps(engine.replies[route]).encode()
        if fault == "malformed" and route == "/volumes":
            body = b'{"Volumes":[],"Volumes":[{}],"Warnings":[]}'
        if fault == "peer" and len(routes) == 12:
            unix_http.peer = (99, 0, 0)
        status = (
            b"404 Not Found"
            if fault == "http-error" and route.startswith("/images/")
            else b"200 OK"
        )
        unix_http.wire = response(body, status=status)
        return _GET(leaf, route, deadline=deadline)

    monkeypatch.setattr(storage, "_engine_get", framed)
    monkeypatch.setattr(bootstrap, "time", storage.time)
    if fault == "slow-total":
        unix_http.tick = 2.0
    if fault is None:
        assert collect(engine)["execution_admission"] is False
        assert len(routes) == len(unix_http.sent) == 22
    else:
        with pytest.raises(bootstrap.LabBootstrapError):
            collect(engine)
        assert len(routes) < 22
    assert engine.closed == 1
    if fault == "slow-total":
        assert unix_http.now <= bootstrap.TOTAL_SECONDS + 2
        assert len(routes) > 11  # The same deadline extends into sample two without resetting.


@pytest.mark.parametrize(
    "platform,uid,arch",
    [("win32", 0, "x86_64"), ("linux", 1000, "x86_64"), ("linux", 0, "aarch64")],
)
def test_real_platform_gate_refuses_before_opening_socket(engine, monkeypatch, platform, uid, arch):
    monkeypatch.setattr(bootstrap, "_platform", _PLATFORM)
    monkeypatch.setattr(bootstrap, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        bootstrap,
        "os",
        SimpleNamespace(geteuid=lambda: uid, uname=lambda: SimpleNamespace(machine=arch)),
    )
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)
    assert engine.opened == 0 and engine.requests == []


def test_collection_never_mutates_caller_binding_or_inventory(engine):
    binding, inputs = copy.deepcopy(engine.binding), copy.deepcopy(engine.replies)
    collect(engine)
    assert binding == engine.binding and inputs == engine.replies


def test_deadline_applies_after_final_input_validation(engine):
    def expire():
        if len(engine.requests) == 22:
            engine.now = 130

    engine.verify_hook = expire
    with pytest.raises(bootstrap.LabBootstrapError):
        collect(engine)
    assert engine.closed == 1
