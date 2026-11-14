"""Bounded read-only empty-engine bootstrap observations, never execution admission.

Only the three pinned images are inspected; unrelated images are unobserved.
Every container, volume or custom network, including retained test artifacts,
refuses bootstrap. Matching samples cannot rule out transient changes or reserve
the daemon against another actor. Host jobs, NIC/hypervisor isolation, source and
image provenance, operator scope and future workload admission remain unverified.
The session wrapper holds its advisory operation lock; this module grants no lock
or mutation capability and has no CLI, remote context or cleanup operation.
"""

import copy
import os
import re
import sys
import time

import network_lab_capacity as capacity
import network_lab_storage as storage

SCHEMA = "grok-network-lab-bootstrap-observation-v1"
SCOPE = "two_empty_engine_samples_and_pinned_images_only"
TOTAL_SECONDS = 30
_BUILTINS = {"bridge": "bridge", "host": "host", "none": "null"}


class LabBootstrapError(RuntimeError):
    """Sanitized refusal; never includes raw inventory or engine diagnostics."""


def _require(condition, message):
    if not condition:
        raise LabBootstrapError(message)


def _platform():
    _require(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        "Bootstrap observation requires root on Linux",
    )
    _require(os.uname().machine == "x86_64", "Bootstrap observation requires Linux x86_64")


def _info(data, peer, ctx):
    identity = storage._engine_identity(data, peer)
    _require(identity["engine_id"] == ctx.engine_id, "Observed bootstrap engine differs")
    _require(
        data.get("OSType") == "linux" and data.get("Architecture") in {"amd64", "x86_64"},
        "Bootstrap engine platform is unsupported",
    )
    swarm = data.get("Swarm")
    _require(
        type(swarm) is dict
        and swarm.get("LocalNodeState") == "inactive"
        and swarm.get("NodeID") == ""
        and swarm.get("NodeAddr") == ""
        and swarm.get("ControlAvailable") is False
        and "RemoteManagers" in swarm
        and swarm.get("RemoteManagers") in (None, [])
        and swarm.get("Error") == ""
        and swarm.get("Cluster") is None
        and all(
            type(swarm.get(key, 0)) is int and swarm.get(key, 0) == 0
            for key in ("Nodes", "Managers")
        ),
        "Bootstrap requires an inactive nonmember Swarm state",
    )
    _require(
        all(
            type(data.get(key)) is int and data[key] == 0
            for key in ("Containers", "ContainersRunning", "ContainersPaused", "ContainersStopped")
        ),
        "Bootstrap engine reports existing containers",
    )
    return {**identity, "os": data["OSType"], "architecture": data["Architecture"], "swarm": swarm}


def _network(data):
    _require(type(data) is dict, "Malformed bootstrap network")
    name = data.get("Name")
    _require(type(name) is str and name in _BUILTINS, "Custom bootstrap network exists")
    _require(
        type(data.get("Id")) is str and bool(re.fullmatch(r"[0-9a-f]{64}", data["Id"])),
        "Malformed bootstrap network identity",
    )
    _require(
        data.get("Scope") == "local"
        and data.get("Driver") == _BUILTINS[name]
        and all(
            data.get(key) is False for key in ("Internal", "Attachable", "Ingress", "ConfigOnly")
        )
        and type(data.get("IPAM")) is dict
        and data["IPAM"].get("Driver") == "default"
        and type(data.get("Options")) is dict,
        "Bootstrap network configuration differs from built-in scope",
    )
    if name == "bridge":
        _require(
            data["Options"].get("com.docker.network.bridge.default_bridge") == "true",
            "Bootstrap bridge is not the daemon default",
        )
    if "Containers" in data:
        _require(
            type(data["Containers"]) is dict and not data["Containers"], "Network endpoints exist"
        )
    return name


def _sample(get, ctx):
    first_data, peer = get("/info")
    first = _info(first_data, peer, ctx)
    containers, _ = get("/containers/json?all=1")
    _require(type(containers) is list and not containers, "Bootstrap requires zero containers")
    volumes, _ = get("/volumes")
    _require(
        type(volumes) is dict
        and "Volumes" in volumes
        and volumes["Volumes"] in (None, [])
        and "Warnings" in volumes
        and volumes["Warnings"] in (None, []),
        "Bootstrap requires zero volumes and no volume-list warnings",
    )
    listed, _ = get("/networks")
    _require(
        type(listed) is list and len(listed) == 3, "Bootstrap requires exactly three built-ins"
    )
    names, identifiers, networks = set(), set(), {}
    for item in listed:
        name = _network(item)
        _require(name not in names and item["Id"] not in identifiers, "Duplicate bootstrap network")
        names.add(name)
        identifiers.add(item["Id"])
        inspected, _ = get("/networks/" + item["Id"])
        _require(_network(inspected) == name, "Bootstrap network inspection differs")
        # NetworkList omits Containers in modern APIs; inspect the exact ID.
        _require(
            type(inspected.get("Containers")) is dict and not inspected["Containers"],
            "Bootstrap network endpoint inspection is incomplete",
        )
        for key in item:
            _require(
                key in inspected and capacity._exact(item[key], inspected[key]),
                "Bootstrap network list and inspection disagree",
            )
        networks[name] = inspected
    _require(names == set(_BUILTINS), "Bootstrap built-in network set differs")
    images = {}
    for role, pin in ctx.images:
        inspected, _ = get("/images/" + pin + "/json")
        _require(
            type(inspected) is dict
            and inspected.get("Id") == pin
            and inspected.get("Os") == "linux"
            and inspected.get("Architecture") == "amd64"
            and inspected.get("Variant") in (None, ""),
            "Required immutable bootstrap image is absent or incompatible",
        )
        images[role] = inspected
    last_data, peer = get("/info")
    _require(
        capacity._exact(first, _info(last_data, peer, ctx)), "Bootstrap engine identity changed"
    )
    return {
        "engine": first,
        "networks": networks,
        "images": images,
        "containers": [],
        "volumes": [],
    }


def collect(context, *, lab_binding):
    """Observe two bounded inventories; caller records supply consistency only.

    Uses the fixed local socket and at most 22 allowlisted GETs. The shared
    30-second HTTP deadline covers both samples, with the existing shorter
    per-request/header/body/wire bounds. No image-list absence claim is made.
    """
    try:
        ctx = capacity._context(context)
        ctx.verify_journal_binding(lab_binding)
        binding = copy.deepcopy(lab_binding)
        digest = ctx.digest()

        def guard():
            _require(
                capacity._context(context).digest() == digest and ctx.digest() == digest,
                "Bootstrap context changed",
            )
            ctx.verify_journal_binding(lab_binding)
            _require(capacity._exact(binding, lab_binding), "Bootstrap retained binding changed")

        guard()
        _platform()
        deadline = time.monotonic() + TOTAL_SECONDS
        leaf = storage._Leaf(storage.SOCKET, "socket")
        try:
            initial = copy.deepcopy(leaf.identity)
            peer_binding = None

            def get(route):
                nonlocal peer_binding
                guard()
                storage._remaining(deadline)
                data, peer = storage._engine_get(leaf, route, deadline=deadline)
                _require(
                    peer_binding is None or peer == peer_binding, "Bootstrap daemon peer changed"
                )
                peer_binding = peer
                _require(leaf.identity == initial, "Bootstrap socket binding changed")
                guard()
                storage._remaining(deadline)
                return data, peer

            first = _sample(get, ctx)
            first_digest = capacity._digest(first)
            second = _sample(get, ctx)
            second_digest = capacity._digest(second)
            _require(first_digest == second_digest, "Bootstrap inventory changed between samples")
            leaf.verify()
            guard()
            storage._remaining(deadline)
            return {
                "schema": SCHEMA,
                "scope": SCOPE,
                "context_sha256": digest,
                "engine_socket_peer_sha256": capacity._digest(
                    {"engine": first["engine"], "socket": initial, "parents": leaf.parent.bindings}
                ),
                "inventory_before_sha256": first_digest,
                "inventory_after_sha256": second_digest,
                "observed_counts": {"containers": 0, "volumes": 0, "networks": 3, "endpoints": 0},
                "required_image_inspections": 3,
                "unrelated_images_observed": False,
                "execution_admission": False,
            }
        finally:
            leaf.close()
    except LabBootstrapError:
        raise
    except Exception:  # noqa: BLE001 -- raw daemon, path and input details remain private.
        raise LabBootstrapError("Bootstrap observation refused; prerequisites unverified") from None
