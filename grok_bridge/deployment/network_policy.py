"""Pure preparation for the reviewed Grok network boundary; never a migration.

Inputs are resolved Compose JSON, not raw YAML. The caller must independently
verify Docker network-connect gateway-priority support and Compose >= 2.33.1
before deployment, then verify actual routes and membership. This module does
not inspect Docker, authorize a release, write files, or establish reachability.
Private configuration remains in memory. Receipts contain topology identifiers
and, in lab mode, a context digest; exceptions never interpolate input values.
The optional lab descriptor changes physical names, never release authority.
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


def _identity(lab_context=None):
    """Return exact physical names; a lab descriptor never grants authority."""
    if lab_context is None:
        return {name: name for name in (*MEMBERS, DEFAULT_NAME, NETWORK_NAME)}
    # Delayed import: the pure descriptor imports the canonical roles above.
    from network_lab_context import LabContext, LabContextError, from_record

    _require(type(lab_context) is LabContext, "Invalid lab context type")
    try:
        validated = from_record(lab_context.to_record())
    except LabContextError:
        raise NetworkPolicyError("Invalid lab context identity") from None
    _require(validated == lab_context, "Lab context identity differs")
    return validated.resource_map()


def _dedicated(names):
    return {"name": names[NETWORK_NAME], "driver": "bridge", "internal": False}


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


def _definition(model, candidate=False, *, lab_context=None):
    names = _identity(lab_context)
    _json_value(model)
    _require(type(model) is dict, "Expected resolved Compose object")
    if lab_context is not None:
        _require(model.get("name") == lab_context.project, "Lab Compose project differs")
    services, networks = model.get("services"), model.get("networks")
    _require(type(services) is dict and type(networks) is dict, "Missing Compose mappings")
    _require(all(type(service) is dict for service in services.values()), "Invalid service")
    _require(all(member in services for member in MEMBERS), "Required service is absent")
    default = networks.get(DEFAULT)
    _require(type(default) is dict, "Missing reviewed default network")
    _require(default.get("name") == names[DEFAULT_NAME], "Default network identity differs")
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
            _require(name != names[NETWORK_NAME], "Dedicated network name collision")
    _require(len(physical) == len(set(physical)), "Physical network name collision")
    if candidate:
        _require(
            _same_json(networks.get(NETWORK), _dedicated(names)),
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


def _target(service, name, *, lab_context=None):
    names = _identity(lab_context)
    if name == BRIDGE:
        _require(
            "ports" not in service or service["ports"] == [], "Bridge ports must be unpublished"
        )
    _require(
        service.get("container_name", name) == names[name],
        "Target container alias requires review",
    )


def _identity_aliases(name, service):
    result = [name]
    for key in ("container_name", "hostname"):
        if key in service:
            _require(type(service[key]) is str, "Invalid service identity")
            result.append(service[key])
    return result


def _check_aliases(name, aliases, names):
    reserved = {value.casefold() for value in (*MEMBERS, *(names[r] for r in MEMBERS))}
    allowed = {name.casefold(), names[name].casefold()} if name in MEMBERS else set()
    _require(
        not ({alias.casefold() for alias in aliases} & (reserved - allowed)),
        "Reserved service alias collision",
    )


def _alias_ownership(services, networks, *, lab_context=None):
    """Check declared default-network aliases without exporting their values."""
    names = _identity(lab_context)
    for name, service in services.items():
        if lab_context is not None:
            # Candidate Grok is dedicated-only; its hostname and all physical
            # container identities must still be checked without default.
            _check_aliases(name, _identity_aliases(name, service), names)
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
        _check_aliases(name, [*aliases, *_identity_aliases(name, service)], names)


def _new_attachment(name):
    # Lower priority keeps consumers' existing default route preferred. Docker's
    # unrelated `priority` field does not select the default gateway.
    # Compose omits the default zero value when normalizing resolved JSON.
    # Grok has only one network, so no explicit gateway priority is needed.
    return {"aliases": [name]} if name == BRIDGE else {"aliases": [name], "gw_priority": -1}


def plan_networks(model, *, lab_context=None):
    """Return (private copied Compose model, sanitized topology receipt)."""
    names = _identity(lab_context)
    services, networks = _definition(model, lab_context=lab_context)
    _alias_ownership(services, networks, lab_context=lab_context)
    for name in MEMBERS:
        service = services[name]
        _target(service, name, lab_context=lab_context)
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
    result["networks"][NETWORK] = _dedicated(names)
    for name in MEMBERS:
        memberships = {} if name == BRIDGE else copy.deepcopy(services[name]["networks"])
        memberships[NETWORK] = _new_attachment(name)
        result["services"][name]["networks"] = memberships
    expected_runtime_networks(result, lab_context=lab_context)
    receipt = {
        "schema": 1,
        "status": "prepared_not_deployed",
        "production_modified": False,
        "network": {**_dedicated(names), "logical_name": NETWORK, "members": list(MEMBERS)},
        "services": {
            name: {
                "before_networks": [names[DEFAULT_NAME]],
                "after_networks": ([names[DEFAULT_NAME]] if name in CONSUMERS else [])
                + [names[NETWORK_NAME]],
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
    if lab_context is not None:
        receipt["mode"] = "lab"
        receipt["context_sha256"] = lab_context.digest()
    return result, receipt


def expected_runtime_networks(candidate, *, lab_context=None):
    """Map only the three approved services to physical networks and options.

    This is expected state, not Docker inspect output. It rejects extra members
    and unsafe target topology. Call verify_network_scope too when comparing a
    prepared candidate with a baseline; this function cannot establish provenance.
    """
    services, networks = _definition(candidate, candidate=True, lab_context=lab_context)
    _alias_ownership(services, networks, lab_context=lab_context)
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
        _target(service, name, lab_context=lab_context)
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


def _review_binding(receipt, expected, *, lab_context=None):
    if lab_context is None:
        _require(receipt is None, "Lab review receipt requires lab context")
        return
    _json_value(receipt)
    _require(_same_json(receipt, expected), "Lab review receipt differs")


def verify_network_scope(before, candidate, *, lab_context=None, review_receipt=None):
    """Refuse changes outside scope and require the lab context's exact receipt.

    The receipt detects context drift, not an authorized reviewer: its hash is
    not a signature or permit. Runtime identity/isolation and release gates are
    independent. expected_runtime_networks validates topology only.
    """
    expected, receipt = plan_networks(before, lab_context=lab_context)
    _review_binding(review_receipt, receipt, lab_context=lab_context)
    _json_value(candidate)
    _require(_same_json(candidate, expected), "Candidate differs from reviewed network scope")
    expected_runtime_networks(candidate, lab_context=lab_context)
