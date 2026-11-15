"""Pure seed-helper report consistency, not runtime provenance or action authority.

This separate schema covers only the working/reference seed helpers. Auxiliary
v1, its store, production exclusivity and release journals are unchanged. Recipe
and inspection hashes cannot prove their sources, actual request ordering,
durability, a fresh volume or uninterrupted daemon/backing-directory continuity.
Every continuity component here is supplied metadata; cold validation does not
re-establish a live controller, daemon process, lock or held volume descriptor.

The future controller must persist each intent before its single effect and bind
reports to actual attributable IDs and independent inspections. A fast exit needs
no observed running interval. Timestamps are syntax checks, not clock ordering or
freshness evidence. Wait responses and helper stdout are not substitutes for the
terminal inspection plus exact seed reinspection. No immutable volume ID exists.

Diagnostics retain prior events and close only normal progression. This is not a
new emergency recovery/stop authorization gate. No retries, adoption, persistence,
runtime calls, helper exemption or execution permission are implemented. A future
finite lab-only retained-helper allowance remains separately reviewed work.
"""

import re
from datetime import datetime

import network_lab_auxiliary_state as bounded
import network_lab_capacity as capacity
import network_lab_seed as seed

SCHEMA = "grok-network-lab-seed-state-v1"
SCOPE = "supplied_seed_helper_intents_and_reports_only"
ROLES = ("working", "reference")
MAX_EVENTS = 16
PHASES = (
    "planned",
    "prerequisites_reported",
    "create_intent",
    "created_reported",
    "start_intent",
    "start_response_reported",
    "terminal_reported",
    "seed_reported",
)
CONTINUITY_HASHES = frozenset(
    {
        "guest_boot_sha256",
        "controller_process_sha256",
        "daemon_process_sha256",
        "socket_peer_sha256",
        "operation_lock_sha256",
        "backing_directory_sha256",
        "backing_mount_sha256",
    }
)
_SHARED_CONTINUITY = CONTINUITY_HASHES - {"backing_directory_sha256", "backing_mount_sha256"}
_DIAGNOSTICS = frozenset(
    {
        "create_ambiguous",
        "start_ambiguous",
        "continuity_lost",
        "observer_failed",
        "resource_conflict",
        "helper_missing",
        "helper_replaced",
        "configuration_changed",
        "helper_failed",
        "seed_mismatch",
    }
)


class LabSeedStateError(ValueError):
    """Sanitized malformed/inconsistent record refusal."""


def _require(condition, message):
    if not condition:
        raise LabSeedStateError(message)


def _fields(value, fields):
    _require(capacity._fields(value, fields), "Invalid seed record fields")


def _hashes(value, fields):
    _require(all(bounded._hex(value[key]) for key in fields), "Invalid seed evidence digest")


def _inputs(context, binding):
    ctx = capacity._context(context)
    retained = bounded._snapshot(binding)
    ctx.verify_journal_binding(retained)
    return ctx, retained


def _guard(context, binding, ctx, retained):
    current, current_binding = _inputs(context, binding)
    _require(ctx == current and capacity._exact(retained, current_binding), "Seed binding changed")


def _preparation(context, binding, transaction):
    _require(
        bounded._hex(transaction, 32) and transaction != "0" * 32,
        "Invalid seed transaction identity",
    )
    roles = {}
    for role in ROLES:
        recipe = seed.seed_helper_recipe(context, lab_binding=binding, role=role)
        body = recipe["create_body"]
        roles[role] = {
            "helper_name": recipe["helper_name"],
            "volume_name": body["HostConfig"]["Mounts"][0]["Source"],
            "image": body["Image"],
            "create_body_sha256": bounded._digest(body),
            "script_sha256": recipe["script_sha256"],
            "seed_manifest": recipe["seed_manifest"],
            "volume_identity": "no_immutable_docker_id",
        }
    return {
        "schema": SCHEMA,
        "scope": SCOPE,
        "execution_admission": False,
        "compatibility_verified": False,
        "lab_context": binding,
        "transaction_id": transaction,
        "roles": roles,
    }


def _continuity(record, context):
    _fields(record, CONTINUITY_HASHES | {"scope", "engine_id"})
    _require(
        record["scope"] == "supplied_consistency_only" and record["engine_id"] == context.engine_id,
        "Seed continuity report scope or engine differs",
    )
    _hashes(record, CONTINUITY_HASHES)


def _runtime(record, *, created):
    flags = {"running", "paused", "restarting", "dead", "oom_killed"}
    counters = {"pid", "restart_count", "exit_code"}
    _fields(record, flags | counters | {"status", "error", "started_at", "finished_at"})
    _require(
        record["status"] == ("created" if created else "exited")
        and record["error"] == ""
        and all(record[key] is False for key in flags)
        and all(type(record[key]) is int and record[key] == 0 for key in counters),
        "Seed runtime report violates phase invariants",
    )
    for key in ("started_at", "finished_at"):
        stamp = record[key]
        if created:
            _require(stamp == bounded.ZERO_TIME, "Created seed helper was already started")
        else:
            _require(
                type(stamp) is str
                and stamp != bounded.ZERO_TIME
                and bool(
                    re.fullmatch(
                        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]{1,9})?Z",
                        stamp,
                    )
                ),
                "Invalid seed terminal timestamp",
            )
            # Python 3.10 rejects nanosecond fractions in fromisoformat. The
            # regex validates their syntax; parse only calendar/whole seconds.
            parsed = datetime.fromisoformat(stamp[:19] + "+00:00")
            fraction = stamp.split(".", 1)[1][:-1] if "." in stamp else ""
            _require(
                (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second)
                != (1, 1, 1, 0, 0, 0)
                or any(digit != "0" for digit in fraction),
                "Seed terminal timestamp is semantically zero",
            )


def _diagnostic(record, phase):
    _fields(record, {"code", "evidence_sha256", "observed_id"})
    code = record["code"]
    _require(type(code) is str and code in _DIAGNOSTICS, "Unknown seed diagnostic")
    _hashes(record, ("evidence_sha256",))
    _require(
        record["observed_id"] is None or bounded._hex(record["observed_id"]),
        "Invalid diagnostic container identity",
    )
    if code in {"create_ambiguous", "start_ambiguous"}:
        _require(phase == code.replace("_ambiguous", "_intent"), "Ambiguity lacks matching intent")
    elif code == "continuity_lost":
        _require(phase != "planned", "Continuity loss has no retained report")
    elif code in {"helper_missing", "helper_replaced", "configuration_changed"}:
        _require(
            PHASES.index(phase) >= PHASES.index("create_intent"), "Diagnostic lacks helper intent"
        )
    elif code in {"helper_failed", "seed_mismatch"}:
        _require(
            PHASES.index(phase) >= PHASES.index("start_intent"), "Diagnostic lacks start intent"
        )


def _replay(context, state):
    phases = dict.fromkeys(ROLES, "planned")
    baselines, creates, starts, created = {}, {}, {}, {}
    operations, identifiers = set(), set()
    closed = False
    shared = None
    for event in state["events"]:
        _fields(event, {"role", "phase", "record"})
        role, phase, record = event["role"], event["phase"], event["record"]
        _require(
            type(role) is str and role in ROLES and type(phase) is str, "Unknown seed role or phase"
        )
        current = phases[role]
        if phase == "diagnostic":
            _diagnostic(record, current)
            closed = True
            continue
        _require(not closed, "Seed diagnostic closes normal progression")
        index = PHASES.index(current) + 1
        _require(
            index < len(PHASES) and phase == PHASES[index], "Seed phase skips, repeats or reverses"
        )
        _require(
            type(record) is dict and "continuity" in record, "Seed report lacks continuity fields"
        )
        _continuity(record["continuity"], context)
        plan = state["roles"][role]
        if phase == "prerequisites_reported":
            _fields(
                record,
                {
                    "image",
                    "image_inspection_sha256",
                    "volume_name",
                    "volume_inspection_sha256",
                    "continuity",
                },
            )
            _require(
                record["image"] == plan["image"] and record["volume_name"] == plan["volume_name"],
                "Seed prerequisite differs from recipe",
            )
            _hashes(record, ("image_inspection_sha256", "volume_inspection_sha256"))
            _require(
                all(
                    previous["backing_directory_sha256"]
                    != record["continuity"]["backing_directory_sha256"]
                    for previous in baselines.values()
                ),
                "Seed roles report the same backing directory",
            )
            baselines[role] = record["continuity"]
            reported_shared = {key: record["continuity"][key] for key in _SHARED_CONTINUITY}
            _require(
                shared is None or capacity._exact(shared, reported_shared),
                "Seed roles report different controller or daemon continuity",
            )
            shared = reported_shared
        else:
            _require(
                capacity._exact(record["continuity"], baselines[role]),
                "Seed continuity report changed",
            )
            if phase in {"create_intent", "start_intent"}:
                _fields(
                    record,
                    {"operation_id", "continuity"}
                    | (
                        {"helper_name", "create_body_sha256"}
                        if phase == "create_intent"
                        else {"target_id"}
                    ),
                )
                operation = record["operation_id"]
                _require(
                    bounded._hex(operation, 32)
                    and operation != "0" * 32
                    and operation not in operations,
                    "Invalid or reused seed operation identity",
                )
                operations.add(operation)
                if phase == "create_intent":
                    _require(
                        record["helper_name"] == plan["helper_name"]
                        and record["create_body_sha256"] == plan["create_body_sha256"],
                        "Seed create intent differs from recipe",
                    )
                    creates[role] = operation
                else:
                    _require(
                        record["target_id"] == created[role]["returned_id"],
                        "Seed start target differs",
                    )
                    starts[role] = operation
            elif phase == "created_reported":
                _fields(
                    record,
                    {
                        "operation_id",
                        "http_status",
                        "returned_id",
                        "inspected_id",
                        "helper_name",
                        "image",
                        "configuration_sha256",
                        "mounts_sha256",
                        "inspection_sha256",
                        "runtime",
                        "warnings",
                        "continuity",
                    },
                )
                identifier = record["returned_id"]
                _require(
                    record["operation_id"] == creates[role]
                    and type(record["http_status"]) is int
                    and record["http_status"] == 201
                    and type(record["warnings"]) is list
                    and not record["warnings"]
                    and bounded._hex(identifier)
                    and identifier == record["inspected_id"]
                    and identifier not in identifiers,
                    "Seed create response is not an exact attributable report",
                )
                _require(
                    record["helper_name"] == plan["helper_name"]
                    and record["image"] == plan["image"],
                    "Created seed helper differs from recipe",
                )
                _hashes(record, ("configuration_sha256", "mounts_sha256", "inspection_sha256"))
                _runtime(record["runtime"], created=True)
                created[role] = record
                identifiers.add(identifier)
            elif phase == "start_response_reported":
                _fields(record, {"operation_id", "target_id", "http_status", "continuity"})
                _require(
                    record["operation_id"] == starts[role]
                    and record["target_id"] == created[role]["returned_id"]
                    and type(record["http_status"]) is int
                    and record["http_status"] == 204,
                    "Seed start response is not attributable first-start success",
                )
            elif phase == "terminal_reported":
                _fields(
                    record,
                    {
                        "id",
                        "start_operation_id",
                        "helper_name",
                        "image",
                        "configuration_sha256",
                        "mounts_sha256",
                        "inspection_sha256",
                        "runtime",
                        "continuity",
                    },
                )
                _require(
                    record["id"] == created[role]["returned_id"]
                    and record["start_operation_id"] == starts[role]
                    and all(
                        record[key] == created[role][key]
                        for key in ("helper_name", "image", "configuration_sha256", "mounts_sha256")
                    ),
                    "Terminal seed helper identity or retained preparation differs",
                )
                _hashes(record, ("inspection_sha256",))
                _runtime(record["runtime"], created=False)
            else:
                _fields(
                    record,
                    {
                        "id",
                        "start_operation_id",
                        "volume_name",
                        "manifest",
                        "seed_inspection_sha256",
                        "continuity",
                    },
                )
                _require(
                    record["id"] == created[role]["returned_id"]
                    and record["start_operation_id"] == starts[role]
                    and record["volume_name"] == plan["volume_name"]
                    and capacity._exact(record["manifest"], plan["seed_manifest"]),
                    "Seed reinspection differs from exact retained plan",
                )
                _hashes(record, ("seed_inspection_sha256",))
        phases[role] = phase


def _validated(context, binding, value):
    state = bounded._snapshot(value)
    _require(type(state) is dict, "Seed state must be an object")
    preparation = _preparation(context, binding, state.get("transaction_id"))
    _fields(state, set(preparation) | {"predecessor_sha256", "events"})
    _require(
        all(capacity._exact(state[key], expected) for key, expected in preparation.items()),
        "Seed preparation or full context differs",
    )
    events = state["events"]
    _require(type(events) is list and len(events) <= MAX_EVENTS, "Seed events exceed bound")
    _require(
        state["predecessor_sha256"] is None
        if not events
        else bounded._hex(state["predecessor_sha256"]),
        "Seed predecessor is inconsistent with retained history",
    )
    _replay(context, state)
    return state


def initial_state(context, *, lab_binding, transaction_id):
    """Create an empty detached consistency record, never a runtime capability."""
    try:
        ctx, binding = _inputs(context, lab_binding)
        result = {
            **_preparation(ctx, binding, transaction_id),
            "predecessor_sha256": None,
            "events": [],
        }
        result = _validated(ctx, binding, result)
        _guard(context, lab_binding, ctx, binding)
        return result
    except LabSeedStateError:
        raise
    except Exception:  # noqa: BLE001 -- arbitrary input errors must not expose private values.
        raise LabSeedStateError("Seed initialization requires exact bounded inputs") from None


def validate_state(context, *, lab_binding, state, previous=None):
    """Validate supplied history or exactly one append; no disk/runtime ordering proof."""
    try:
        ctx, binding = _inputs(context, lab_binding)
        current = _validated(ctx, binding, state)
        if previous is not None:
            prior = _validated(ctx, binding, previous)
            preparation = set(current) - {"events", "predecessor_sha256"}
            _require(
                all(capacity._exact(current[key], prior[key]) for key in preparation),
                "Seed preparation changed across predecessor",
            )
            _require(
                len(current["events"]) == len(prior["events"]) + 1
                and capacity._exact(current["events"][:-1], prior["events"])
                and current["predecessor_sha256"] == bounded._digest(prior),
                "Seed update is not one append to the exact predecessor",
            )
        _guard(context, lab_binding, ctx, binding)
        return current
    except LabSeedStateError:
        raise
    except Exception:  # noqa: BLE001 -- JSON/time/parser diagnostics may contain private values.
        raise LabSeedStateError("Seed state is malformed or inconsistent") from None
