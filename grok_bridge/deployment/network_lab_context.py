"""Pure identity contract for a future disconnected Grok lifecycle harness.

This module reads no files, invokes no commands and grants no execution authority.
A valid descriptor or journal binding is not an operator permit, hypervisor
isolation proof, capacity check or production release approval. Production entry
points must continue to reject lab state. The lifecycle harness is not wired yet.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import network_policy as policy

SCHEMA = "grok-network-lab-context-v1"
BINDING_SCHEMA = "grok-network-lab-binding-v1"
ROOT = PurePosixPath("/var/lib/codex-security-lab/grok-network-lifecycle")
SCENARIOS = frozenset(
    {"deploy-rollback", "pre-stop-interruption", "ambiguous-candidate", "configuration-conflict"}
)
IMAGE_ROLES = ("original", "candidate", "nginx")
MAX_RECORD_BYTES = 16384
_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "context_id",
        "scenario",
        "vm_uuid",
        "vm_name",
        "engine_id",
        "source_commit",
        "images",
    }
)


class LabContextError(ValueError):
    """Sanitized descriptor refusal; never includes private input values."""


def _require(condition, message):
    if not condition:
        raise LabContextError(message)


def _matches(value, pattern):
    return type(value) is str and bool(re.fullmatch(pattern, value))


def _keys(value, expected):
    return type(value) is dict and all(type(k) is str for k in value) and set(value) == expected


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate descriptor key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class LabContext:
    context_id: str
    scenario: str
    vm_uuid: str
    vm_name: str
    engine_id: str
    source_commit: str
    images: tuple

    def __post_init__(self):
        _require(_matches(self.context_id, r"[0-9a-f]{32}"), "Invalid lab context identity")
        _require(type(self.scenario) is str and self.scenario in SCENARIOS, "Unknown lab scenario")
        _require(
            _matches(self.vm_uuid, r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
            and self.vm_uuid != "00000000-0000-0000-0000-000000000000",
            "Invalid lab VM identity",
        )
        _require(
            _matches(self.vm_name, r"codex-grok-lab-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?"),
            "Invalid lab VM name",
        )
        _require(
            _matches(self.engine_id, r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}"),
            "Invalid engine identity",
        )
        _require(_matches(self.source_commit, r"[0-9a-f]{40}"), "Invalid reviewed source identity")
        _require(
            type(self.images) is tuple and len(self.images) == len(IMAGE_ROLES),
            "Invalid image pins",
        )
        _require(
            all(
                type(pair) is tuple
                and len(pair) == 2
                and type(pair[0]) is str
                and pair[0] == role
                and _matches(pair[1], r"sha256:[0-9a-f]{64}")
                for pair, role in zip(self.images, IMAGE_ROLES)
            ),
            "Invalid image pins",
        )
        _require(
            len({value for _, value in self.images}) == len(IMAGE_ROLES), "Image roles collide"
        )

    @property
    def namespace(self):
        return self.context_id + "-" + self.scenario

    @property
    def project(self):
        return "codex-grok-nettest-" + self.namespace

    @property
    def generation_root(self):
        return ROOT / self.namespace

    @property
    def configuration_root(self):
        return self.generation_root / "configuration"

    def resource_map(self):
        """Return a fresh complete map accepted by the existing Docker adapter."""
        return {
            **{role: self.project + "-" + role for role in policy.MEMBERS},
            policy.DEFAULT_NAME: self.project + "-default",
            policy.NETWORK_NAME: self.project + "-dedicated",
        }

    def network_definition(self):
        return {
            "name": self.resource_map()[policy.NETWORK_NAME],
            "driver": "bridge",
            "internal": False,
        }

    def to_record(self):
        return {
            "schema": SCHEMA,
            "mode": "lab",
            "context_id": self.context_id,
            "scenario": self.scenario,
            "vm_uuid": self.vm_uuid,
            "vm_name": self.vm_name,
            "engine_id": self.engine_id,
            "source_commit": self.source_commit,
            "images": dict(self.images),
        }

    def digest(self):
        data = json.dumps(self.to_record(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def journal_binding(self):
        return {
            "schema": BINDING_SCHEMA,
            "mode": "lab",
            "context_sha256": self.digest(),
            "context": self.to_record(),
        }

    def verify_journal_binding(self, binding):
        _require(
            _keys(binding, {"schema", "mode", "context_sha256", "context"}),
            "Invalid lab binding fields",
        )
        _require(
            type(binding["schema"]) is str
            and binding["schema"] == BINDING_SCHEMA
            and type(binding["mode"]) is str
            and binding["mode"] == "lab",
            "Invalid lab binding schema or mode",
        )
        other = from_record(binding["context"])
        _require(other == self, "Lab context changed since journal creation")
        _require(
            _matches(binding["context_sha256"], r"[0-9a-f]{64}")
            and binding["context_sha256"] == self.digest(),
            "Lab binding digest differs",
        )

    def verify_observed_identity(self, *, vm_uuid, vm_name, engine_id):
        """Compare supplied observations only; this never inspects or authorizes a host."""
        _require(
            all(type(v) is str for v in (vm_uuid, vm_name, engine_id))
            and (vm_uuid, vm_name, engine_id) == (self.vm_uuid, self.vm_name, self.engine_id),
            "Observed lab VM or engine identity differs",
        )


def from_record(record):
    _require(_keys(record, _FIELDS), "Invalid lab context fields")
    _require(
        type(record["schema"]) is str
        and record["schema"] == SCHEMA
        and type(record["mode"]) is str
        and record["mode"] == "lab",
        "Invalid lab context schema or mode",
    )
    _require(_keys(record["images"], set(IMAGE_ROLES)), "Invalid image roles")
    return LabContext(
        context_id=record["context_id"],
        scenario=record["scenario"],
        vm_uuid=record["vm_uuid"],
        vm_name=record["vm_name"],
        engine_id=record["engine_id"],
        source_commit=record["source_commit"],
        images=tuple((role, record["images"][role]) for role in IMAGE_ROLES),
    )


def parse_record(raw):
    """Parse bounded UTF-8 JSON and reject duplicate keys at every object depth."""
    _require(
        type(raw) is bytes and 0 < len(raw) <= MAX_RECORD_BYTES, "Invalid descriptor byte bound"
    )
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise LabContextError("Invalid lab context JSON") from None
    return from_record(record)
