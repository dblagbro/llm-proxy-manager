"""Accelerator admission decisions (v5.23 slice 4 skeleton).

No residency state machine and no request-pipeline hook yet. When
``LOCAL_ACCEL_ENABLED`` is false this is a complete no-op (spec G6):
``evaluate()`` always returns ``Admit``.

The refusal *shape* lives in ``errors.py`` so later slices can wire
``select_provider_with_503`` without inventing a second 429/503 contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from app.config import settings
from app.resources.errors import ResourceUnavailable, resource_unavailable


@dataclass(frozen=True)
class Admit:
    """Serve now. No headers, no wait."""


Decision = Union[Admit, ResourceUnavailable]


@dataclass
class AdmissionRequest:
    model: str
    accelerator_id: str = "local-gpu-0"
    requires_warm: bool = False
    estimated_ram_mb: Optional[int] = None
    estimated_vram_mb: Optional[int] = None


def admission_enabled() -> bool:
    return bool(getattr(settings, "local_accel_enabled", False))


def evaluate(req: AdmissionRequest) -> Decision:
    """Return Admit or a ResourceUnavailable refusal.

    Skeleton only: disabled → Admit. Enabled still Admits until the
    residency machine (slice 3) and watermarks (rest of slice 4) land.
    Tests exercise refusals via ``resource_unavailable()`` directly.
    """
    if not admission_enabled():
        return Admit()
    # Later: probe + residency + watermarks. Do not invent a load here.
    _ = req
    return Admit()


# Re-export so callers have one import surface.
__all__ = [
    "Admit",
    "AdmissionRequest",
    "Decision",
    "admission_enabled",
    "evaluate",
    "resource_unavailable",
]
