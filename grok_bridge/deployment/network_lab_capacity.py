"""Pure lab capacity arithmetic; no commands, files, measurement or admission.

The caller supplies two observations using one canonical stable identity per
filesystem, and explicit simultaneous upcoming peaks. These values are not
trusted measurements. A future read-only filesystem/Docker collector must bind
them to actual storage and establish freshness. VM/engine identity, mount
classification, isolation and operator authority remain independent requirements.
Peak-estimate adequacy, actual available space and reserve persistence are also
unverified; a positive result establishes capacity arithmetic only.
This module does not replace any production reserve or release guard.
"""

import hashlib
import json
import re

from network_lab_context import LabContext, LabContextError, from_record

SCHEMA = "grok-network-lab-capacity-v1"
ROLES = ("evidence", "docker")
RESERVE_BYTES = 10 * 1024**3
MAX_BYTES = 2**63 - 1
_OBSERVATION_FIELDS = {"filesystem_id", "total_bytes", "available_bytes"}


class LabCapacityError(ValueError):
    """Sanitized refusal without supplied identities or private values."""


def _require(condition, message):
    if not condition:
        raise LabCapacityError(message)


def _fields(value, expected):
    return (
        type(value) is dict
        and all(type(key) is str for key in value)
        and set(value) == set(expected)
    )


def _byte_count(value):
    return type(value) is int and 0 <= value <= MAX_BYTES


def _context(value):
    _require(type(value) is LabContext, "Invalid lab context type")
    try:
        validated = from_record(value.to_record())
    except LabContextError:
        raise LabCapacityError("Invalid lab context identity") from None
    _require(validated == value, "Lab context identity differs")
    return validated


def _snapshot(observations, peaks):
    _require(_fields(observations, ROLES), "Storage observations are incomplete")
    _require(_fields(peaks, ROLES), "Explicit upcoming peaks are incomplete")
    copied = {}
    copied_peaks = {}
    for role in ROLES:
        item = observations[role]
        _require(_fields(item, _OBSERVATION_FIELDS), "Invalid storage observation fields")
        filesystem = item["filesystem_id"]
        _require(
            type(filesystem) is str
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", filesystem)),
            "Invalid filesystem identity",
        )
        total, available = item["total_bytes"], item["available_bytes"]
        _require(
            _byte_count(total) and _byte_count(available) and available <= total,
            "Inconsistent storage counters",
        )
        _require(_byte_count(peaks[role]), "Invalid upcoming peak")
        copied[role] = {
            "filesystem_id": filesystem,
            "total_bytes": total,
            "available_bytes": available,
        }
        copied_peaks[role] = peaks[role]
    _require(sum(copied_peaks.values()) > 0, "Upcoming allocation must be positive")
    return copied, copied_peaks


def _digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def assess(context, observations, *, peaks):
    """Assess supplied bytes only; raise if any filesystem lacks reserve + peak.

    Both roles and peaks are mandatory. An explicit zero peak for one role is
    allowed, but their sum must be positive. Every distinct filesystem retains
    the fixed reserve, including one whose role has an explicitly zero peak.
    Shared storage uses one reserve, summed peaks and the lower available count.
    The returned detached JSON record is not a permit or a fresh measurement.
    """
    context = _context(context)
    observations, peaks = _snapshot(observations, peaks)
    grouped = {}
    for role in ROLES:
        item = observations[role]
        filesystem = item["filesystem_id"]
        if filesystem not in grouped:
            grouped[filesystem] = {
                "filesystem_sha256": _digest(filesystem),
                "roles": [],
                "total_bytes": item["total_bytes"],
                "available_bytes": item["available_bytes"],
                "peak_bytes": 0,
                "reserve_bytes": RESERVE_BYTES,
            }
        group = grouped[filesystem]
        _require(group["total_bytes"] == item["total_bytes"], "Filesystem totals conflict")
        group["roles"].append(role)
        group["available_bytes"] = min(group["available_bytes"], item["available_bytes"])
        group["peak_bytes"] += peaks[role]
    for group in grouped.values():
        group["required_bytes"] = RESERVE_BYTES + group["peak_bytes"]
        _require(
            group["available_bytes"] >= group["required_bytes"],
            "Insufficient supplied capacity reserve",
        )
    return {
        "schema": SCHEMA,
        "mode": "lab",
        "status": "supplied_capacity_arithmetic_satisfied",
        "scope": "supplied_values_only_not_execution_admission",
        "context_sha256": context.digest(),
        "inputs_sha256": _digest({"observations": observations, "peaks": peaks}),
        "filesystems": list(grouped.values()),
    }


def _exact(actual, expected):
    """Compare only the bounded expected shape, without coercion or custom hooks."""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return _fields(actual, expected) and all(
            _exact(actual[key], expected[key]) for key in expected
        )
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            _exact(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def verify(assessment, context, observations, *, peaks):
    """Refuse any receipt/input/context drift; no authenticity or freshness claim."""
    expected = assess(context, observations, peaks=peaks)
    _require(_exact(assessment, expected), "Capacity assessment differs")
