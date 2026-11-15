"""Pure auxiliary fixture recipes, not creation/start/adoption authority.

The original-image reference has its own Compose ownership and named volume.
An admitted controller must seed that fresh volume and CREATE the reference only;
it must never start it. Its refusal entrypoint is defense in depth: even exit 78
means the never-started invariant was violated. This is synthetic-data/image
preservation, not a snapshot of the initialized working browser or its runtime.

The separately owned probe has an idle Python entrypoint, bypassing browser
startup. Both entrypoints disable automatic site imports as well as user paths.
It references only the planned default network. Its external network
declaration is a name reference, not proof of actual network identity or permission
to adopt it. No TCP targets or probe logic are supplied. A later controller still
needs live identity/endpoint bindings and positive controls before testing denial.

No files, networks, volumes, processes, journals or admission records are created
or observed. Named-volume backing storage requires separate guest Docker-root
admission; only the script bind is generation-contained. No deletion, replacement
or start permission follows from these deterministic byte inputs.

These recipes constrain declared settings only. They do not inspect inherited
image VOLUME/ENV metadata, rule out image-declared anonymous volumes or establish
Python/runtime compatibility of the pinned original image. Image-material and
actual-runtime admission remain separate requirements before any resource action.
"""

import copy

import network_lab_capacity as capacity
import network_policy as policy
import yaml

REFERENCE_COMPOSE = "configuration/auxiliary/reference.compose.yml"
PROBE_COMPOSE = "configuration/auxiliary/probe.compose.yml"
PROBE_SCRIPT = "configuration/auxiliary/probe_idle.py"
REFERENCE_SEED = "seed/reference-generation.txt"
_REFERENCE_ROLE = "frozen-reference"
_PROBE_ROLE = "outsider-probe"
_PROBE_TARGET = "/opt/codex-lab/probe_idle.py"
_REFUSAL = (
    "import sys; "
    "sys.stderr.write('Synthetic preservation reference: startup forbidden\\n'); "
    "sys.exit(78)"
)
_IDLE = b'''"""Synthetic idle probe; no network tests or browser startup."""

import signal


def _stop(_signal, _frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)
while True:
    signal.pause()
'''


class LabAuxiliaryError(ValueError):
    """Sanitized refusal without private caller identity or configuration details."""


def _service(context, role):
    return {
        "container_name": context.project + "-" + role,
        "image": dict(context.images)["original"],
        "pull_policy": "never",
        "restart": "no",
        "healthcheck": {"disable": True},
        "command": [],
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": "64m",
        "cpus": 0.5,
        "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}},
    }


def _reference(context):
    service = _service(context, _REFERENCE_ROLE)
    service.update(
        entrypoint=["python", "-I", "-B", "-S", "-c", _REFUSAL],
        network_mode="none",
        volumes=[
            {
                "type": "volume",
                "source": "reference-data",
                "target": "/data",
                "read_only": True,
                "volume": {"nocopy": True},
            }
        ],
    )
    return {
        "name": context.project + "-reference",
        "services": {_REFERENCE_ROLE: service},
        "volumes": {
            "reference-data": {"name": context.project + "-reference-data", "driver": "local"}
        },
    }


def _probe(context):
    service = _service(context, _PROBE_ROLE)
    service.update(
        entrypoint=["python", "-I", "-B", "-S", "-u", _PROBE_TARGET],
        networks={"default": None},
        volumes=[
            {
                "type": "bind",
                "source": str(context.generation_root / PROBE_SCRIPT),
                "target": _PROBE_TARGET,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
    )
    return {
        "name": context.project + "-probe",
        "services": {_PROBE_ROLE: service},
        "networks": {
            "default": {"name": context.resource_map()[policy.DEFAULT_NAME], "external": True}
        },
    }


def auxiliary_fixture_inputs(context, *, lab_binding):
    """Return detached relative-path -> bytes recipes for the exact supplied binding.

    Materialization, reference seed installation and all resource identity checks
    belong to a later admitted controller. Reference and probe recipes must remain
    separate from the working Compose ownership tuple and from each other. The
    returned seed attributes synthetic reference data only; it proves no restore.
    """
    try:
        ctx = capacity._context(context)
        ctx.verify_journal_binding(lab_binding)
        binding = copy.deepcopy(lab_binding)
        result = {
            REFERENCE_COMPOSE: (
                "# Synthetic preservation reference: CREATE ONLY; NEVER START.\n"
                "# Any start, including refusal exit 78, violates its retained invariant.\n"
                + yaml.safe_dump(_reference(ctx), sort_keys=False)
            ).encode(),
            PROBE_COMPOSE: (
                "# Synthetic idle probe only; no targets, denial evidence or admission.\n"
                "# Resolve and verify the existing default network identity before attachment.\n"
                + yaml.safe_dump(_probe(ctx), sort_keys=False)
            ).encode(),
            PROBE_SCRIPT: _IDLE,
            REFERENCE_SEED: (
                "synthetic-reference-generation\n"
                "not-an-initialized-working-data-snapshot\n"
                "context_sha256=" + ctx.digest() + "\n"
            ).encode(),
        }
        ctx.verify_journal_binding(lab_binding)
        if capacity._context(context) != ctx or not capacity._exact(binding, lab_binding):
            raise LabAuxiliaryError("Auxiliary context or retained binding changed")
        return result
    except LabAuxiliaryError:
        raise
    except Exception:  # noqa: BLE001 -- malformed input diagnostics may include private identities.
        raise LabAuxiliaryError(
            "Auxiliary fixture inputs require an exact lab context and binding"
        ) from None
