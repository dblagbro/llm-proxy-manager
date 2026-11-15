"""Pure auxiliary intent/receipt consistency, never ownership or action authority.

The append-only events describe supplied reports, not observed effects. A later
controller must durably persist each intent before its effect and bind returned
receipts to live inspections. ``previous`` checks one proposed append and a
canonical predecessor digest; neither that digest nor a valid history proves
durability, provenance, freshness or actual ordering. Cold validation without a
predecessor checks the retained record only, not its relationship to disk.

Ordinary local volumes have no immutable Docker object ID. A create response can
refer to an existing volume. Volume continuity therefore stays explicitly
unverified, including after a reported seed installation. Held backing-directory
and mount identity, secure materialization and restart reconciliation are future
work. No name, label, timestamp or digest promotes a volume to owned/fresh.

Reference/probe identities remain separate from the working release journal.
The reference must never start; the default network is borrowed. Any diagnostic
retains earlier events and closes normal progression for this generation. No
retry, replacement, cleanup, permission, I/O or controller entry point exists.
Image inheritance and actual mount/network/configuration checks remain future
live work: supplied inspection hashes cannot establish their contents or safety.
"""

import hashlib
import json
import re
from datetime import datetime

import network_lab_auxiliary as auxiliary
import network_lab_capacity as capacity
import network_policy as policy

SCHEMA = "grok-network-lab-auxiliary-state-v1"
SCOPE = "supplied_auxiliary_intents_and_receipts_only"
MAX_RECORD_BYTES = 65536
MAX_EVENTS = 32
ZERO_TIME = "0001-01-01T00:00:00Z"
_VOLUME = "reference_volume"
_REFERENCE = "reference_container"
_PROBE = "probe_container"
_NETWORK = "default_network"
_IMAGE = "original_image"
_PHASES = {
    _VOLUME: ("planned", "create_intent", "creation_reported", "seed_intent", "seed_recorded"),
    _REFERENCE: ("planned", "create_intent", "created_recorded"),
    _PROBE: (
        "planned",
        "create_intent",
        "created_recorded",
        "start_intent",
        "running_recorded",
        "stop_intent",
        "stopped_recorded",
    ),
    _NETWORK: ("planned", "borrowed_recorded"),
    _IMAGE: ("planned", "metadata_recorded"),
}
_DIAGNOSTICS = {
    "creation_ambiguous": "unresolved",
    "seed_ambiguous": "unresolved",
    "start_ambiguous": "unresolved",
    "stop_ambiguous": "unresolved",
    "volume_continuity_lost": "unresolved",
    "observer_failed": "unresolved",
    "reference_started": "violation",
    "resource_missing": "violation",
    "resource_replaced": "violation",
    "material_changed": "violation",
    "configuration_changed": "violation",
}


class LabAuxiliaryStateError(ValueError):
    """Sanitized refusal; caller diagnostics/identities are never interpolated."""


def _require(condition, message):
    if not condition:
        raise LabAuxiliaryStateError(message)


def _fields(value, keys):
    _require(capacity._fields(value, keys), "Invalid auxiliary record fields")


def _hex(value, length=64):
    return type(value) is str and bool(re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value))


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate auxiliary JSON key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise LabAuxiliaryStateError("Nonfinite auxiliary JSON value")


def _snapshot(value):
    if type(value) is bytes:
        _require(0 < len(value) <= MAX_RECORD_BYTES, "Auxiliary serialized record exceeds bound")
        value = json.loads(
            value.decode("utf-8"), object_pairs_hook=_object, parse_constant=_nonfinite
        )
    budget = [4096]

    def visit(item, depth=0):
        budget[0] -= 1
        _require(depth <= 12 and budget[0] >= 0, "Auxiliary record structure exceeds bound")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int:
            _require(-(2**63) <= item < 2**63, "Auxiliary integer exceeds bound")
            return item
        if kind is str:
            _require(
                len(item) <= 4096 and len(item.encode("utf-8")) <= 4096,
                "Auxiliary string exceeds bound",
            )
            return item
        if kind is list:
            _require(len(item) <= 128, "Auxiliary sequence exceeds bound")
            return [visit(child, depth + 1) for child in item]
        if kind is dict:
            _require(len(item) <= 128, "Auxiliary mapping exceeds bound")
            _require(all(type(key) is str for key in item), "Invalid auxiliary mapping key")
            return {visit(key, depth + 1): visit(child, depth + 1) for key, child in item.items()}
        raise LabAuxiliaryStateError("Invalid auxiliary JSON type")

    result = visit(value)
    _require(len(_encoded(result)) <= MAX_RECORD_BYTES, "Auxiliary serialized record exceeds bound")
    return result


def _preparation(context, binding, transaction_id):
    _require(
        _hex(transaction_id, 32) and transaction_id != "0" * 32,
        "Invalid auxiliary transaction identity",
    )
    bundle = auxiliary.auxiliary_fixture_inputs(context, lab_binding=binding)
    materials = {path: hashlib.sha256(data).hexdigest() for path, data in bundle.items()}
    original = dict(context.images)["original"]
    resources = {
        _IMAGE: {"image": original},
        _VOLUME: {
            "name": context.project + "-reference-data",
            "driver": "local",
            "identity": "no_immutable_docker_id",
            "continuity": "unverified",
            "seed_manifest": [
                {
                    "path": "reference-generation.txt",
                    "type": "regular",
                    "size": len(bundle[auxiliary.REFERENCE_SEED]),
                    "sha256": materials[auxiliary.REFERENCE_SEED],
                }
            ],
        },
        _NETWORK: {
            "name": context.resource_map()[policy.DEFAULT_NAME],
            "classification": "borrowed_not_owned",
            "driver": "bridge",
            "internal": False,
        },
    }
    for role, service, suffix in (
        (_REFERENCE, "frozen-reference", "reference"),
        (_PROBE, "outsider-probe", "probe"),
    ):
        resources[role] = {
            "name": context.project + "-" + service,
            "service": service,
            "project": context.project + "-" + suffix,
            "image": original,
        }
    return {
        "schema": SCHEMA,
        "mode": "lab",
        "scope": SCOPE,
        "execution_admission": False,
        "lab_context": binding,
        "transaction_id": transaction_id,
        "materials": materials,
        "resources": resources,
    }


def _checksums(record, fields):
    _require(all(_hex(record[key]) for key in fields), "Invalid auxiliary inspection digest")


def _runtime(value, phase, started=None):
    _fields(value, {"status", "running", "started_at", "restart_count", "exit_code"})
    status = {
        "created_recorded": "created",
        "running_recorded": "running",
        "stopped_recorded": "exited",
    }[phase]
    _require(
        value["status"] == status
        and value["running"] is (status == "running")
        and type(value["restart_count"]) is int
        and value["restart_count"] == 0
        and type(value["exit_code"]) is int
        and value["exit_code"] == 0,
        "Auxiliary runtime report violates phase invariants",
    )
    stamp = value["started_at"]
    if status == "created":
        _require(stamp == ZERO_TIME, "Auxiliary creation report is not never-started")
    else:
        _require(
            type(stamp) is str
            and stamp != ZERO_TIME
            and bool(
                re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                    r"(?:\.[0-9]{1,9})?Z",
                    stamp,
                )
            ),
            "Invalid auxiliary start timestamp",
        )
        datetime.fromisoformat(stamp[:-1] + "+00:00")
        _require(started is None or stamp == started, "Recorded probe start identity changed")


def _diagnostic(resource, phase, record, current):
    _fields(record, {"code", "evidence_sha256", "observed_id"})
    code = record["code"]
    _require(type(code) is str and _DIAGNOSTICS.get(code) == phase, "Unknown auxiliary diagnostic")
    _checksums(record, ("evidence_sha256",))
    observed = record["observed_id"]
    _require(observed is None or _hex(observed), "Invalid diagnostic object identity")
    _require(
        resource not in {_VOLUME, _IMAGE} or observed is None,
        "Diagnostic invents an immutable volume or image object identity",
    )
    requirements = {
        "creation_ambiguous": ({_VOLUME, _REFERENCE, _PROBE}, "create_intent"),
        "seed_ambiguous": ({_VOLUME}, "seed_intent"),
        "start_ambiguous": ({_PROBE}, "start_intent"),
        "stop_ambiguous": ({_PROBE}, "stop_intent"),
    }
    if code in requirements:
        roles, required_phase = requirements[code]
        _require(
            resource in roles and current == required_phase,
            "Ambiguity diagnostic has no matching retained intent",
        )
    if code == "reference_started":
        _require(
            resource == _REFERENCE and current != "planned",
            "Reference-start diagnostic has no reference intent",
        )
    if code == "volume_continuity_lost":
        _require(
            resource == _VOLUME and current not in {"planned", "create_intent"},
            "Volume continuity diagnostic has no reported volume",
        )


def _container_created(resource, record, plan, borrowed, used_ids):
    _fields(
        record,
        {
            "operation_id",
            "returned_id",
            "inspected_id",
            "name",
            "image",
            "configuration_sha256",
            "mounts_sha256",
            "networks_sha256",
            "inspection_sha256",
            "default_network_id",
            "runtime",
        },
    )
    identifier = record["returned_id"]
    _require(
        _hex(identifier) and record["inspected_id"] == identifier and identifier not in used_ids,
        "Auxiliary create response identity differs or collides",
    )
    _require(
        record["name"] == plan["name"] and record["image"] == plan["image"],
        "Auxiliary container receipt differs from its recipe",
    )
    _checksums(
        record, ("configuration_sha256", "mounts_sha256", "networks_sha256", "inspection_sha256")
    )
    _require(
        record["default_network_id"] == (borrowed if resource == _PROBE else None),
        "Auxiliary container network binding differs",
    )
    _runtime(record["runtime"], "created_recorded")
    used_ids.add(identifier)


def _probe_report(record, created, phase, started):
    _fields(
        record,
        {
            "operation_id",
            "id",
            "configuration_sha256",
            "mounts_sha256",
            "networks_sha256",
            "inspection_sha256",
            "default_network_id",
            "runtime",
        },
    )
    _require(record["id"] == created["returned_id"], "Recorded probe identity changed")
    for field in ("configuration_sha256", "mounts_sha256", "default_network_id"):
        _require(record[field] == created[field], "Recorded probe preparation changed")
    _checksums(record, ("networks_sha256", "inspection_sha256"))
    _runtime(record["runtime"], phase, started)


def _replay(state):
    phases = dict.fromkeys(_PHASES, "planned")
    intents, receipts, used_ids, operation_ids = {}, {}, set(), set()
    terminal = False
    for event in state["events"]:
        _fields(event, {"resource", "phase", "record"})
        resource, phase, record = event["resource"], event["phase"], event["record"]
        _require(
            type(resource) is str and resource in _PHASES and type(phase) is str,
            "Unknown auxiliary resource or phase",
        )
        if phase in {"unresolved", "violation"}:
            _diagnostic(resource, phase, record, phases[resource])
            terminal = True
            continue
        _require(not terminal, "Auxiliary diagnostic forbids further progress in this generation")
        sequence = _PHASES[resource]
        index = sequence.index(phases[resource]) + 1
        _require(
            index < len(sequence) and phase == sequence[index],
            "Auxiliary transition skips, repeats or reverses a phase",
        )
        plan = state["resources"][resource]
        if phase.endswith("_intent"):
            fields = {"operation_id"}
            if phase == "create_intent":
                fields.add("absence_observation_sha256")
            _fields(record, fields)
            operation = record["operation_id"]
            _require(
                _hex(operation, 32) and operation != "0" * 32 and operation not in operation_ids,
                "Invalid or reused auxiliary operation identity",
            )
            if phase == "create_intent":
                _checksums(record, ("absence_observation_sha256",))
            if resource in {_REFERENCE, _PROBE} and phase == "create_intent":
                _require(
                    phases[_IMAGE] == "metadata_recorded",
                    "Container intent precedes pinned image metadata report",
                )
                dependency, required = (
                    (_VOLUME, "seed_recorded")
                    if resource == _REFERENCE
                    else (_NETWORK, "borrowed_recorded")
                )
                _require(phases[dependency] == required, "Container intent lacks its prerequisite")
            operation_ids.add(operation)
            intents[resource] = operation
        elif resource == _IMAGE:
            _fields(record, {"image", "inspection_sha256"})
            _require(record["image"] == plan["image"], "Image metadata pin differs")
            _checksums(record, ("inspection_sha256",))
        elif resource == _NETWORK:
            _fields(record, {"name", "id", "static_configuration_sha256", "inspection_sha256"})
            _require(
                record["name"] == plan["name"]
                and _hex(record["id"])
                and record["id"] not in used_ids,
                "Borrowed network binding differs or collides",
            )
            _checksums(record, ("static_configuration_sha256", "inspection_sha256"))
            used_ids.add(record["id"])
        else:
            _require(
                type(record) is dict and record.get("operation_id") == intents[resource],
                "Auxiliary receipt has no matching retained intent",
            )
            if resource == _VOLUME and phase == "creation_reported":
                _fields(record, {"operation_id", "returned_name", "inspection_sha256"})
                _require(record["returned_name"] == plan["name"], "Volume response name differs")
                _checksums(record, ("inspection_sha256",))
            elif resource == _VOLUME:
                _fields(record, {"operation_id", "volume_name", "manifest", "inspection_sha256"})
                _require(
                    record["volume_name"] == plan["name"]
                    and capacity._exact(record["manifest"], plan["seed_manifest"]),
                    "Reported synthetic seed location or manifest differs",
                )
                _checksums(record, ("inspection_sha256",))
            elif phase == "created_recorded":
                network = receipts.get((_NETWORK, "borrowed_recorded"), {}).get("id")
                _container_created(resource, record, plan, network, used_ids)
            else:
                created = receipts[(_PROBE, "created_recorded")]
                prior_start = receipts.get((_PROBE, "running_recorded"), {}).get("runtime", {})
                _probe_report(record, created, phase, prior_start.get("started_at"))
        phases[resource] = phase
        receipts[(resource, phase)] = record


def _validated(context, binding, value):
    state = _snapshot(value)
    _require(type(state) is dict, "Auxiliary state must be an object")
    preparation = _preparation(context, binding, state.get("transaction_id"))
    _fields(state, set(preparation) | {"predecessor_sha256", "events"})
    _require(
        all(capacity._exact(state[key], expected) for key, expected in preparation.items()),
        "Auxiliary preparation or full context differs",
    )
    events = state["events"]
    _require(
        type(events) is list and len(events) <= MAX_EVENTS, "Auxiliary event count exceeds bound"
    )
    _require(
        (state["predecessor_sha256"] is None if not events else _hex(state["predecessor_sha256"])),
        "Invalid auxiliary predecessor digest",
    )
    _replay(state)
    return state


def _inputs(context, binding):
    ctx = capacity._context(context)
    retained = _snapshot(binding)
    ctx.verify_journal_binding(retained)
    return ctx, retained


def _guard(context, binding, ctx, retained):
    _require(
        capacity._context(context) == ctx and capacity._exact(_snapshot(binding), retained),
        "Auxiliary caller context or binding changed",
    )


def initial_state(context, *, lab_binding, transaction_id):
    """Return a detached plan only. No resources, intent persistence or permission."""
    try:
        ctx, retained = _inputs(context, lab_binding)
        result = _preparation(ctx, retained, transaction_id)
        result.update(predecessor_sha256=None, events=[])
        _guard(context, lab_binding, ctx, retained)
        return _snapshot(result)
    except LabAuxiliaryStateError:
        raise
    except Exception:  # noqa: BLE001 -- malformed input may contain private values.
        raise LabAuxiliaryStateError("Invalid auxiliary context, binding or plan") from None


def validate_state(context, *, lab_binding, state, previous=None):
    """Validate supplied dict/UTF-8 JSON bytes; optionally check exactly one append.

    A previously diagnosed generation can append diagnostics only. Existing
    intents/receipts cannot be dropped or rebased. The predecessor is a consistency
    hash, not a successful-write receipt, signature or live ownership attestation.
    """
    try:
        ctx, retained = _inputs(context, lab_binding)
        result = _validated(ctx, retained, state)
        if previous is not None:
            old = _validated(ctx, retained, previous)
            _require(
                all(
                    capacity._exact(result[key], old[key])
                    for key in old
                    if key not in {"predecessor_sha256", "events"}
                ),
                "Retained auxiliary preparation changed",
            )
            _require(
                result["predecessor_sha256"] == _digest(old)
                and len(result["events"]) == len(old["events"]) + 1
                and capacity._exact(result["events"][:-1], old["events"]),
                "Auxiliary predecessor or append-only history differs",
            )
        _guard(context, lab_binding, ctx, retained)
        return result
    except LabAuxiliaryStateError:
        raise
    except Exception:  # noqa: BLE001 -- malformed input may contain private values.
        raise LabAuxiliaryStateError("Invalid auxiliary state or predecessor") from None
