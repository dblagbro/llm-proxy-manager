"""Sanitized Docker adapter for preparation; no CLI or release authorization.

Read-only unless explicitly enabled with transaction and consumer identities.
Callers must hold the maintenance lock and use the transaction journal and
configuration CAS; mutation capability is not proof an attachment is owned.
Requires root-capable host nsenter/ip and Docker gateway-priority support.
No application commands or payloads run inside containers. Raw inspection and
route data are hashed in memory and are never logged or returned to callers.
All Docker calls select the local Unix socket explicitly so a remote CLI context
cannot be combined with this host's process namespaces.

Original endpoint fingerprints cover the complete Docker endpoint metadata.
Default-route fingerprints cover IPv4 and IPv6 routes, excluding only volatile
expiry/cache data. Config fingerprints cover Image, Config, HostConfig and Mounts
but exclude NetworkSettings, whose expected membership changes are tracked apart.
The adapter does not validate Compose files, browser authorization, snapshots or
rollback acceptance. Isolated integration and operator review remain required.
"""

import copy
import hashlib
import json
import re
import subprocess

import network_policy as policy

DOCKER = "/usr/bin/docker"
DOCKER_SOCKET = "unix:///var/run/docker.sock"
NSENTER = "/usr/bin/nsenter"
IP = "/usr/sbin/ip"
OWNER_LABEL = "com.tmr.security.grok-transaction"
PROJECT_LABEL = "com.docker.compose.project"
NETWORK_LABEL = "com.docker.compose.network"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TX = re.compile(r"[0-9a-f]{32}\Z")
_FIXTURE = re.compile(r"codex-grok-nettest-[a-z0-9][a-z0-9-]{0,63}\Z")
_RESOURCES = (*policy.MEMBERS, policy.DEFAULT_NAME, policy.NETWORK_NAME)
_MAX_OUTPUT = 16 * 1024 * 1024


class DockerBoundaryError(RuntimeError):
    """Sanitized adapter failure; never includes raw subprocess diagnostics."""


def _require(condition, message):
    if not condition:
        raise DockerBoundaryError(message)


def _hex(value):
    return type(value) is str and bool(_HEX.fullmatch(value))


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise DockerBoundaryError("Invalid private fingerprint input") from None
    return hashlib.sha256(encoded).hexdigest()


def subprocess_runner(argv, timeout):
    """Capture only; callers receive sanitized errors, never CalledProcessError."""
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except Exception:  # noqa: BLE001 -- exception text may embed private command output.
        raise DockerBoundaryError("Host command did not complete") from None


class DockerHost:
    """Dependency-injected adapter matching NetworkTransaction's host interface.

    `resource_map` is optional. For an isolated fixture, provide every canonical
    role/network mapped below one explicit `codex-grok-nettest-*` compose_project.
    Projections normalize fixture resources back to canonical names. No mixed
    production/fixture mapping is accepted. `allowed_bridge_id` only recognizes
    a separately verified member; no method changes the Grok container lifecycle.
    """

    def __init__(
        self,
        runner=None,
        *,
        enable_mutations=False,
        transaction_id=None,
        allowed_consumers=None,
        allowed_bridge_id=None,
        resource_map=None,
        compose_project=None,
    ):
        _require(type(enable_mutations) is bool, "Invalid mutation capability")
        _require(
            transaction_id is None
            or (type(transaction_id) is str and bool(_TX.fullmatch(transaction_id))),
            "Invalid transaction identity",
        )
        _require(allowed_bridge_id is None or _hex(allowed_bridge_id), "Invalid bridge identity")
        if allowed_consumers is not None:
            _require(
                type(allowed_consumers) is dict and set(allowed_consumers) == set(policy.CONSUMERS),
                "Invalid consumer allowlist",
            )
            _require(
                all(_hex(value) for value in allowed_consumers.values()),
                "Invalid consumer identity",
            )
            _require(
                len(set(allowed_consumers.values())) == len(policy.CONSUMERS),
                "Duplicate consumer identity",
            )
        if enable_mutations:
            _require(
                transaction_id is not None and allowed_consumers is not None,
                "Mutation binding is incomplete",
            )
        if resource_map is None:
            _require(compose_project in (None, "docker"), "Production project identity differs")
            resources = {name: name for name in _RESOURCES}
            project = "docker"
        else:
            _require(
                type(resource_map) is dict and set(resource_map) == set(_RESOURCES),
                "Incomplete fixture resource map",
            )
            _require(
                type(compose_project) is str and bool(_FIXTURE.fullmatch(compose_project)),
                "Fixture project prefix is invalid",
            )
            _require(
                all(
                    type(value) is str
                    and value.startswith(compose_project + "-")
                    and bool(re.fullmatch(r"[a-z0-9-]{1,128}", value))
                    for value in resource_map.values()
                ),
                "Fixture resource prefix is invalid",
            )
            _require(
                len(set(resource_map.values())) == len(_RESOURCES), "Fixture resource names collide"
            )
            resources = copy.deepcopy(resource_map)
            project = compose_project
        _require(
            allowed_bridge_id is None
            or allowed_consumers is None
            or allowed_bridge_id not in allowed_consumers.values(),
            "Bridge and consumer identities collide",
        )
        self.runner = runner or subprocess_runner
        self.enabled = enable_mutations
        self.transaction_id = transaction_id
        self.allowed = copy.deepcopy(allowed_consumers)
        self.bridge_id = allowed_bridge_id
        self.resources = resources
        self.project = project
        self._guards = {}
        self._processes = {}

    def _run(self, argv, timeout=20):
        _require(type(timeout) is int and 1 <= timeout <= 60, "Invalid host timeout")
        if argv[0] == DOCKER:
            argv = [DOCKER, "--host", DOCKER_SOCKET, *argv[1:]]
        try:
            result = self.runner(list(argv), timeout)
            _require(result.returncode == 0, "Host command failed; inspect privately")
            output = result.stdout
            if type(output) is bytes:
                _require(len(output) <= _MAX_OUTPUT, "Host output exceeds reviewed bound")
                output = output.decode("utf-8", errors="strict")
            _require(
                type(output) is str and len(output.encode()) <= _MAX_OUTPUT, "Invalid host output"
            )
            return output
        except DockerBoundaryError:
            raise
        except Exception:  # noqa: BLE001 -- never expose runner or decoding diagnostics.
            raise DockerBoundaryError("Host command response is unavailable") from None

    def _json(self, argv, timeout=20):
        try:
            return json.loads(self._run(argv, timeout))
        except DockerBoundaryError:
            raise
        except (TypeError, ValueError):
            raise DockerBoundaryError("Host returned invalid JSON") from None

    def _inspect_container(self, reference, role):
        _require(role in policy.MEMBERS, "Unapproved container role")
        _require(reference == self.resources[role] or _hex(reference), "Invalid inspect reference")
        values = self._json([DOCKER, "inspect", "--type", "container", reference])
        _require(
            type(values) is list and len(values) == 1 and type(values[0]) is dict,
            "Ambiguous container inspection",
        )
        value = values[0]
        _require(
            _hex(value.get("Id")) and value.get("Name") == "/" + self.resources[role],
            "Container name or identity differs",
        )
        if _hex(reference):
            _require(value["Id"] == reference, "Exact container identity differs")
        if self.allowed is not None and role in policy.CONSUMERS:
            _require(value["Id"] == self.allowed[role], "Bound consumer was replaced")
        if self.bridge_id is not None and role == policy.BRIDGE:
            _require(value["Id"] == self.bridge_id, "Bound bridge identity differs")
        state = value.get("State")
        _require(
            type(state) is dict
            and state.get("Running") is True
            and state.get("Restarting", False) is False,
            "Container is not stably running",
        )
        _require(
            type(state.get("Pid")) is int and state["Pid"] > 0, "Invalid container process identity"
        )
        _require(
            type(state.get("StartedAt")) is str and bool(state["StartedAt"]),
            "Missing container start identity",
        )
        _require(
            type(value.get("RestartCount")) is int and value["RestartCount"] >= 0,
            "Invalid restart counter",
        )
        for key in ("Config", "HostConfig", "NetworkSettings"):
            _require(type(value.get(key)) is dict, "Incomplete container configuration")
        _require(
            type(value.get("Mounts")) is list
            and type(value.get("Image")) is str
            and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value["Image"])),
            "Invalid image or mount identity",
        )
        mode = value["HostConfig"].get("NetworkMode")
        _require(
            mode in (self.resources[policy.DEFAULT_NAME], self.resources[policy.NETWORK_NAME]),
            "Container namespace mode requires review",
        )
        if role == policy.BRIDGE:
            _require(
                not value["HostConfig"].get("PortBindings"), "Bridge host ports must be unpublished"
            )
            ports = value["NetworkSettings"].get("Ports", {})
            _require(
                type(ports) is dict and not any(ports.values()),
                "Bridge runtime ports must be unpublished",
            )
        networks = value["NetworkSettings"].get("Networks")
        _require(
            type(networks) is dict and bool(networks), "Container network memberships are absent"
        )
        _require(
            set(networks)
            <= {self.resources[policy.DEFAULT_NAME], self.resources[policy.NETWORK_NAME]},
            "Unreviewed runtime network membership",
        )
        return value

    @staticmethod
    def _config_hash(value):
        return _digest({key: value[key] for key in ("Image", "Config", "HostConfig", "Mounts")})

    @classmethod
    def _guard(cls, value):
        return _digest(
            {
                "id": value["Id"],
                "name": value["Name"],
                "pid": value["State"]["Pid"],
                "started": value["State"]["StartedAt"],
                "restarts": value["RestartCount"],
                "config": cls._config_hash(value),
                "network": value["NetworkSettings"],
            }
        )

    def _routes(self, pid, family):
        routes = self._json(
            [
                NSENTER,
                "--target",
                str(pid),
                "--net",
                "--",
                IP,
                "-j",
                family,
                "route",
                "show",
                "default",
            ]
        )
        _require(type(routes) is list, "Invalid default route response")
        normalized = []
        for route in routes:
            _require(
                type(route) is dict and route.get("dst") == "default", "Non-default route returned"
            )
            normalized.append(
                {key: value for key, value in route.items() if key not in {"expires", "cache"}}
            )
        return sorted(normalized, key=lambda route: json.dumps(route, sort_keys=True))

    def _endpoint(self, role, cid, logical, endpoint):
        _require(
            type(endpoint) is dict
            and _hex(endpoint.get("NetworkID"))
            and _hex(endpoint.get("EndpointID")),
            "Invalid endpoint identity",
        )
        priority = endpoint.get("GwPriority", 0)
        _require(type(priority) is int, "Invalid endpoint gateway priority")
        aliases = [] if endpoint.get("Aliases") is None else endpoint["Aliases"]
        dns_names = [] if endpoint.get("DNSNames") is None else endpoint["DNSNames"]
        _require(
            type(aliases) is list
            and type(dns_names) is list
            and all(type(value) is str for value in [*aliases, *dns_names]),
            "Invalid endpoint aliases",
        )
        if logical == policy.NETWORK_NAME:
            allowed_names = {role, self.resources[role], cid, cid[:12]}
            _require(
                {*aliases, *dns_names} <= allowed_names and role in {*aliases, *dns_names},
                "Dedicated endpoint aliases differ",
            )
            _require(
                priority == (-1 if role in policy.CONSUMERS else 0),
                "Dedicated gateway priority differs",
            )
        # The original full metadata is hashed above. Do not export private or
        # custom legacy aliases; only this normalized, approved role is exposed.
        return {
            "id": endpoint["NetworkID"],
            "attachment_sha256": _digest(endpoint),
            "aliases": [role],
            "gw_priority": priority,
        }

    def container(self, role):
        _require(type(role) is str and role in policy.MEMBERS, "Unapproved container role")
        first = self._inspect_container(self.resources[role], role)
        guard = self._guard(first)
        routes = {
            "ipv4": self._routes(first["State"]["Pid"], "-4"),
            "ipv6": self._routes(first["State"]["Pid"], "-6"),
        }
        last = self._inspect_container(first["Id"], role)
        _require(self._guard(last) == guard, "Container changed during namespace route inspection")
        reverse = {
            physical: logical
            for logical, physical in self.resources.items()
            if logical in {policy.DEFAULT_NAME, policy.NETWORK_NAME}
        }
        networks = {
            reverse[name]: self._endpoint(role, last["Id"], reverse[name], endpoint)
            for name, endpoint in last["NetworkSettings"]["Networks"].items()
        }
        self._guards[role] = (last["Id"], guard)
        self._processes[role] = (
            last["Id"],
            last["State"]["Pid"],
            last["State"]["StartedAt"],
            last["RestartCount"],
        )
        return {
            "name": role,
            "id": last["Id"],
            "config_sha256": self._config_hash(last),
            "default_route_sha256": _digest(routes),
            "networks": networks,
        }

    def _inspect_network(self, network_id):
        _require(_hex(network_id), "Invalid network identity")
        values = self._json([DOCKER, "network", "inspect", network_id])
        _require(
            type(values) is list and len(values) == 1 and type(values[0]) is dict,
            "Ambiguous network inspection",
        )
        value = values[0]
        _require(
            value.get("Id") == network_id
            and value.get("Name") == self.resources[policy.NETWORK_NAME],
            "Exact network identity differs",
        )
        _require(
            value.get("Driver") == "bridge"
            and value.get("Internal") is False
            and value.get("Scope") == "local"
            and value.get("Ingress", False) is False
            and value.get("ConfigOnly", False) is False
            and value.get("EnableIPv4", True) is True
            and value.get("EnableIPv6", False) is False
            and not value.get("Options"),
            "Dedicated network configuration differs",
        )
        labels = value.get("Labels")
        _require(type(labels) is dict, "Missing network ownership labels")
        owner = labels.get(OWNER_LABEL)
        _require(
            type(owner) is str and bool(_TX.fullmatch(owner)),
            "Missing network transaction ownership",
        )
        if self.transaction_id is not None:
            _require(owner == self.transaction_id, "Network belongs to another transaction")
        _require(
            labels.get(PROJECT_LABEL) == self.project
            and labels.get(NETWORK_LABEL) == policy.NETWORK,
            "Compose network ownership differs",
        )
        members = value.get("Containers")
        _require(type(members) is dict, "Invalid network member inspection")
        expected_names = {self.resources[role]: role for role in policy.MEMBERS}
        normalized = {}
        for cid, metadata in members.items():
            _require(
                _hex(cid)
                and type(metadata) is dict
                and type(metadata.get("Name")) is str
                and metadata["Name"] in expected_names,
                "Unapproved network member",
            )
            role = expected_names[metadata["Name"]]
            _require(role not in normalized.values(), "Duplicate network member role")
            if self.enabled and role == policy.BRIDGE:
                _require(self.bridge_id == cid, "Bridge member is not explicitly bound")
            inspected = self._inspect_container(cid, role)
            endpoint = inspected["NetworkSettings"]["Networks"].get(
                self.resources[policy.NETWORK_NAME]
            )
            _require(
                type(endpoint) is dict
                and endpoint.get("NetworkID") == network_id
                and metadata.get("EndpointID") == endpoint.get("EndpointID"),
                "Network and container endpoints disagree",
            )
            self._endpoint(role, cid, policy.NETWORK_NAME, endpoint)
            normalized[cid] = role
        return value, normalized

    def network(self, name):
        _require(name == policy.NETWORK_NAME, "Unapproved network name")
        physical = self.resources[name]
        listing = self._run(
            [
                DOCKER,
                "network",
                "ls",
                "--no-trunc",
                "--filter",
                "name=^" + re.escape(physical) + "$",
                "--format",
                "{{.ID}}",
            ]
        )
        ids = listing.split()
        _require(len(ids) <= 1 and all(_hex(value) for value in ids), "Ambiguous network lookup")
        if not ids:
            return None  # Only successful, empty exact-name lookup establishes absence.
        value, members = self._inspect_network(ids[0])
        return {
            "name": policy.NETWORK_NAME,
            "id": value["Id"],
            "transaction_id": value["Labels"][OWNER_LABEL],
            "driver": "bridge",
            "internal": False,
            "members": members,
        }

    def _mutation_allowed(self):
        _require(
            self.enabled and self.transaction_id is not None and self.allowed is not None,
            "Adapter is read-only",
        )

    def _namespace_guard(self):
        ids = self._run([DOCKER, "ps", "-a", "--no-trunc", "--format", "{{.ID}}"]).split()
        _require(
            len(ids) <= 512 and len(ids) == len(set(ids)) and all(_hex(value) for value in ids),
            "Container inventory exceeds reviewed scope",
        )
        _require(set(self.allowed.values()) <= set(ids), "Bound consumer is absent from inventory")
        values = self._json([DOCKER, "inspect", "--type", "container", *ids])
        _require(type(values) is list and len(values) == len(ids), "Incomplete container inventory")
        modes = {}
        names = {}
        for item in values:
            _require(
                type(item) is dict
                and item.get("Id") in ids
                and type(item.get("HostConfig")) is dict
                and type(item.get("Name")) is str,
                "Invalid inventory identity",
            )
            _require(item["Id"] not in modes, "Duplicate inventory identity")
            mode = item["HostConfig"].get("NetworkMode", "")
            _require(type(mode) is str, "Invalid inventory namespace mode")
            _require(not mode.startswith("service:"), "Unresolved inventory namespace sharing")
            modes[item["Id"]] = mode
            names[item["Name"].lstrip("/")] = item["Id"]
        targets = set(self.allowed.values()) | ({self.bridge_id} if self.bridge_id else set())
        current_bridge = names.get(self.resources[policy.BRIDGE])
        if current_bridge is not None:
            targets.add(current_bridge)
        for original in modes:
            visited = set()
            cursor = original
            while modes[cursor].startswith("container:"):
                _require(cursor not in visited, "Cyclic namespace sharing requires review")
                visited.add(cursor)
                reference = modes[cursor].removeprefix("container:")
                matches = [
                    cid
                    for cid in modes
                    if cid == names.get(reference)
                    or (len(reference) >= 12 and cid.startswith(reference))
                ]
                _require(len(matches) == 1, "Namespace owner is unresolved")
                cursor = matches[0]
                _require(cursor not in targets, "Another container shares an approved namespace")

    def _consumer(self, cid):
        self._mutation_allowed()
        _require(
            _hex(cid) and cid in self.allowed.values(), "Unapproved mutation container identity"
        )
        return next(role for role, value in self.allowed.items() if value == cid)

    def _stable_again(self, role):
        cid, guard = self._guards[role]
        _require(
            self._guard(self._inspect_container(cid, role)) == guard,
            "Consumer drift before network operation",
        )

    def create_network(self, name, transaction_id):
        self._mutation_allowed()
        _require(
            name == policy.NETWORK_NAME and transaction_id == self.transaction_id,
            "Network creation binding differs",
        )
        _require(self.network(name) is None, "Dedicated network already exists")
        for role in policy.CONSUMERS:
            value = self.container(role)
            _require(
                set(value["networks"]) == {policy.DEFAULT_NAME}
                and value["networks"][policy.DEFAULT_NAME]["gw_priority"] >= 0,
                "Initial consumer topology requires review",
            )
        self._namespace_guard()
        for role in policy.CONSUMERS:
            self._stable_again(role)
        labels = [
            OWNER_LABEL + "=" + transaction_id,
            PROJECT_LABEL + "=" + self.project,
            NETWORK_LABEL + "=" + policy.NETWORK,
        ]
        argv = [DOCKER, "network", "create", "--driver", "bridge"]
        for label in labels:
            argv.extend(["--label", label])
        network_id = self._run([*argv, self.resources[name]], timeout=30).strip()
        _require(_hex(network_id), "Invalid network creation receipt")
        observed = self.network(name)
        _require(
            observed is not None and observed["id"] == network_id and not observed["members"],
            "Created network differs from receipt",
        )
        return network_id

    def _bound_network(self, network_id):
        _require(_hex(network_id), "Invalid mutation network identity")
        observed = self.network(policy.NETWORK_NAME)
        _require(
            observed is not None and observed["id"] == network_id, "Owned network identity differs"
        )
        return observed

    def connect(self, network_id, container_id, alias, gw_priority):
        role = self._consumer(container_id)
        _require(
            alias == role and type(gw_priority) is int and gw_priority == -1,
            "Connection options differ",
        )
        self._bound_network(network_id)
        before = self.container(role)
        process_before = self._processes[role]
        _require(
            set(before["networks"]) == {policy.DEFAULT_NAME}
            and before["networks"][policy.DEFAULT_NAME]["gw_priority"] >= 0,
            "Consumer is already attached or topology differs",
        )
        self._namespace_guard()
        self._bound_network(network_id)
        self._stable_again(role)
        self._run(
            [
                DOCKER,
                "network",
                "connect",
                "--gw-priority",
                "-1",
                "--alias",
                alias,
                network_id,
                container_id,
            ],
            timeout=30,
        )
        after = self.container(role)
        _require(
            self._processes[role] == process_before, "Consumer restarted during network operation"
        )
        self._preserved(before, after)
        _require(
            after["networks"].get(policy.NETWORK_NAME, {}).get("id") == network_id,
            "New attachment is absent",
        )

    @staticmethod
    def _preserved(before, after):
        _require(
            all(
                before[key] == after[key] for key in ("id", "config_sha256", "default_route_sha256")
            )
            and before["networks"].get(policy.DEFAULT_NAME)
            == after["networks"].get(policy.DEFAULT_NAME),
            "Consumer configuration, default route or original membership drift",
        )

    def disconnect(self, network_id, container_id):
        role = self._consumer(container_id)
        self._bound_network(network_id)
        before = self.container(role)
        process_before = self._processes[role]
        _require(
            before["networks"].get(policy.NETWORK_NAME, {}).get("id") == network_id
            and policy.DEFAULT_NAME in before["networks"],
            "Owned attachment is absent",
        )
        self._namespace_guard()
        self._bound_network(network_id)
        self._stable_again(role)
        self._run([DOCKER, "network", "disconnect", network_id, container_id], timeout=30)
        after = self.container(role)
        _require(
            self._processes[role] == process_before, "Consumer restarted during network operation"
        )
        self._preserved(before, after)
        _require(set(after["networks"]) == {policy.DEFAULT_NAME}, "Disconnection is not observed")
