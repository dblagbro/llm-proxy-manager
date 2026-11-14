"""Pure preparation for the reviewed Grok network boundary; never a migration.

Inputs are resolved Compose JSON, not raw YAML. The caller must independently
verify Docker network-connect gateway-priority support and Compose >= 2.33.1
before deployment, then verify actual routes and membership. This module does
not inspect Docker, authorize a release, write files, or establish reachability.
Private configuration remains in memory. Receipts contain constant topology
identifiers only; exceptions never interpolate input values.
"""

import copy
import json
import math
import re

BRIDGE = "llm-proxy2-grok-bridge"
CONSUMERS = ("nginx", "llm-proxy2")
MEMBERS = (BRIDGE, *CONSUMERS)
DEFAULT = "default"
DEFAULT_NAME = "docker_default"
NETWORK = "grok_security"
NETWORK_NAME = "docker_grok_security"
NETWORK_DEFINITION = {"name": NETWORK_NAME, "driver": "bridge", "internal": False}
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class NetworkPolicyError(ValueError):
    """Sanitized, fail-closed policy refusal."""


def _require(condition, message):
    if not condition:
        raise NetworkPolicyError(message)


def _json_value(value):
    """Reject custom objects and non-JSON values before copying or comparing."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        _require(math.isfinite(value), "Non-finite JSON value")
        return
    if type(value) is list:
        for child in value:
            _json_value(child)
        return
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "Invalid JSON key")
        for child in value.values():
            _json_value(child)
        return
    raise NetworkPolicyError("Unsupported JSON value")


def _same_json(left, right):
    # Python equality considers False == 0 and 1 == 1.0. Neither is an exact
    # unchanged JSON value for configuration-scope or priority validation.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _attachment(value):
    if value is None:
        return
    _require(type(value) is dict, "Ambiguous network attachment")
    _require(
        set(value) <= {"aliases", "priority", "gw_priority"},
        "Unreviewed network attachment options",
    )
    if "aliases" in value:
        aliases = value["aliases"]
        _require(type(aliases) is list, "Invalid network aliases")
        _require(
            all(type(alias) is str and _ALIAS.fullmatch(alias) for alias in aliases),
            "Invalid network aliases",
        )
        _require(
            len({alias.casefold() for alias in aliases}) == len(aliases),
            "Duplicate network aliases",
        )
    for key in ("priority", "gw_priority"):
        if key in value:
            _require(type(value[key]) is int, "Invalid network priority")
            _require(value[key] >= 0, "Existing network priority requires review")


def _definition(model, candidate=False):
    _json_value(model)
    _require(type(model) is dict, "Expected resolved Compose object")
    services, networks = model.get("services"), model.get("networks")
    _require(type(services) is dict and type(networks) is dict, "Missing Compose mappings")
    _require(all(type(service) is dict for service in services.values()), "Invalid service")
    _require(all(member in services for member in MEMBERS), "Required service is absent")
    default = networks.get(DEFAULT)
    _require(type(default) is dict, "Missing reviewed default network")
    _require(default.get("name") == DEFAULT_NAME, "Default network identity differs")
    _require(
        set(default) <= {"name", "driver", "internal", "external", "attachable", "ipam", "labels"},
        "Unreviewed default network options",
    )
    _require(default.get("driver", "bridge") == "bridge", "Default network is not bridge")
    _require(default.get("internal", False) is False, "Default network blocks egress")
    _require(default.get("ipam", {}) == {}, "Default network addressing requires review")
    for key in ("external", "attachable"):
        if key in default:
            _require(type(default[key]) is bool, "Invalid default network option")
    physical = []
    for key, definition in networks.items():
        _require(type(definition) is dict, "Ambiguous network declaration")
        name = definition.get("name")
        _require(type(name) is str and bool(_ALIAS.fullmatch(name)), "Unresolved network name")
        physical.append(name)
        if key != NETWORK:
            _require(name != NETWORK_NAME, "Dedicated network name collision")
    _require(len(physical) == len(set(physical)), "Physical network name collision")
    if candidate:
        _require(
            _same_json(networks.get(NETWORK), NETWORK_DEFINITION),
            "Dedicated network definition differs",
        )
    else:
        _require(NETWORK not in networks, "Dedicated network already declared")
    return services, networks


def _memberships(service):
    _require("network_mode" not in service, "Network mode requires separate review")
    _require(
        not service.get("links") and not service.get("external_links"),
        "Network links require review",
    )
    memberships = service.get("networks")
    _require(type(memberships) is dict and bool(memberships), "Unresolved service networks")
    return memberships


def _target(service, name):
    if name == BRIDGE:
        _require(
            "ports" not in service or service["ports"] == [], "Bridge ports must be unpublished"
        )
    _require(service.get("container_name", name) == name, "Target container alias requires review")


def _alias_ownership(services, networks):
    """Check declared default-network aliases without exporting their values."""
    reserved = set(MEMBERS)
    for name, service in services.items():
        memberships = service.get("networks", {})
        if "network_mode" in service:
            # Namespace sharing can inherit a target's dedicated network even
            # without a declared attachment. Only unrelated host/none modes are
            # understood here; host access is outside this bridge boundary.
            mode = service["network_mode"]
            _require(
                name not in MEMBERS and type(mode) is str and mode in {"host", "none"},
                "Network namespace mode requires separate review",
            )
            _require(
                type(memberships) is dict and not memberships, "Ambiguous unrelated network mode"
            )
            continue
        _require(type(memberships) is dict and bool(memberships), "Unresolved service networks")
        _require(set(memberships) <= set(networks), "Unknown network membership")
        if DEFAULT not in memberships:
            continue
        attachment = memberships[DEFAULT]
        _require(attachment is None or type(attachment) is dict, "Ambiguous default attachment")
        aliases = [] if attachment is None else attachment.get("aliases", [])
        _require(type(aliases) is list, "Invalid default aliases")
        _require(all(type(alias) is str for alias in aliases), "Invalid default aliases")
        aliases = [*aliases, name]
        for key in ("container_name", "hostname"):
            if key in service:
                _require(type(service[key]) is str, "Invalid service identity")
                aliases.append(service[key])
        _require(
            not ({alias.casefold() for alias in aliases} & (reserved - {name})),
            "Reserved service alias collision",
        )


def _new_attachment(name):
    # Lower priority keeps consumers' existing default route preferred. Docker's
    # unrelated `priority` field does not select the default gateway.
    # Compose omits the default zero value when normalizing resolved JSON.
    # Grok has only one network, so no explicit gateway priority is needed.
    return {"aliases": [name]} if name == BRIDGE else {"aliases": [name], "gw_priority": -1}


def plan_networks(model):
    """Return (private copied Compose model, sanitized topology receipt)."""
    services, networks = _definition(model)
    _alias_ownership(services, networks)
    for name in MEMBERS:
        service = services[name]
        _target(service, name)
        memberships = _memberships(service)
        _require(set(memberships) == {DEFAULT}, "Target network topology requires review")
        _attachment(memberships[DEFAULT])
        if name == BRIDGE:
            aliases = (memberships[DEFAULT] or {}).get("aliases", [])
            _require(
                all(alias == BRIDGE for alias in aliases),
                "Additional bridge aliases require review",
            )
    result = copy.deepcopy(model)
    result["networks"][NETWORK] = copy.deepcopy(NETWORK_DEFINITION)
    for name in MEMBERS:
        memberships = {} if name == BRIDGE else copy.deepcopy(services[name]["networks"])
        memberships[NETWORK] = _new_attachment(name)
        result["services"][name]["networks"] = memberships
    expected_runtime_networks(result)
    receipt = {
        "schema": 1,
        "status": "prepared_not_deployed",
        "production_modified": False,
        "network": {**NETWORK_DEFINITION, "logical_name": NETWORK, "members": list(MEMBERS)},
        "services": {
            name: {
                "before_networks": [DEFAULT_NAME],
                "after_networks": ([DEFAULT_NAME] if name in CONSUMERS else []) + [NETWORK_NAME],
                "dedicated_gateway_priority": 0 if name == BRIDGE else -1,
            }
            for name in MEMBERS
        },
        "required_capabilities": {
            "compose_minimum_version": "2.33.1",
            "docker_network_connect_gw_priority": True,
        },
        "live_routes_and_reachability_verified": False,
        "release_gate_satisfied": False,
    }
    return result, receipt


def expected_runtime_networks(candidate):
    """Map only the three approved services to physical networks and options.

    This is expected state, not Docker inspect output. It rejects extra members
    and unsafe target topology. Call verify_network_scope too when comparing a
    prepared candidate with a baseline; this function cannot establish provenance.
    """
    services, networks = _definition(candidate, candidate=True)
    _alias_ownership(services, networks)
    members = set()
    for name, service in services.items():
        memberships = service.get("networks", {})
        _require(type(memberships) is dict, "Unresolved service networks")
        if NETWORK in memberships:
            members.add(name)
    _require(members == set(MEMBERS), "Dedicated network member allowlist differs")
    result = {}
    for name in MEMBERS:
        service = services[name]
        _target(service, name)
        memberships = _memberships(service)
        expected = {NETWORK} | ({DEFAULT} if name in CONSUMERS else set())
        _require(set(memberships) == expected, "Candidate target topology differs")
        _require(
            _same_json(memberships[NETWORK], _new_attachment(name)), "Dedicated attachment differs"
        )
        if name in CONSUMERS:
            _attachment(memberships[DEFAULT])
        result[name] = {
            networks[key]["name"]: copy.deepcopy(options) for key, options in memberships.items()
        }
    return result


def verify_network_scope(before, candidate):
    """Refuse any change outside the exact reviewed network transformation."""
    expected, _ = plan_networks(before)
    _json_value(candidate)
    _require(_same_json(candidate, expected), "Candidate differs from reviewed network scope")
    expected_runtime_networks(candidate)
