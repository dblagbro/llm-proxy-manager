"""Accelerator admission refusal contract (v5.23 slice 4 skeleton).

This is **resource back-pressure** — refusing or delaying work the host
cannot physically serve. It is not MCP capability-signalling
(``docs/5.10-mcp-backpressure-design.md``).

503 vs 429 (spec §6.7):
- **429** means the *client* sent too much (``queue_full``). Slow down.
- **503** means the *host* cannot serve right now. Retry unchanged.
Both always carry ``Retry-After``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse

# code -> (status, default retry_after_sec)
ADMISSION_CONTRACT: dict[str, tuple[int, int]] = {
    "model_loading": (503, 30),
    "vram_exhausted": (503, 90),
    "ram_watermark": (503, 30),
    "host_swapping": (503, 60),
    "thrash_guard": (503, 600),
    "queue_full": (429, 15),
    "queue_timeout": (503, 0),
    "residency_not_warm": (503, 30),
}


@dataclass
class ResourceUnavailable(Exception):
    """Structured local-accelerator refusal."""

    code: str
    message: str
    retry_after_sec: int
    accelerator: str = "local-gpu-0"
    resident_model: Optional[str] = None
    requested_model: Optional[str] = None
    queue_depth: int = 0
    remedy: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> int:
        mapped = ADMISSION_CONTRACT.get(self.code)
        return mapped[0] if mapped else 503

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(int(self.retry_after_sec))}

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": "resource_unavailable",
                "code": self.code,
                "param": None,
            },
            "llmp": {
                "accelerator": self.accelerator,
                "resident_model": self.resident_model,
                "requested_model": self.requested_model,
                "retry_after_sec": int(self.retry_after_sec),
                "queue_depth": self.queue_depth,
                "remedy": self.remedy,
                **self.extra,
            },
        }

    def to_http_exception(self) -> HTTPException:
        return HTTPException(
            status_code=self.status,
            detail=self.body(),
            headers=self.headers(),
        )

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content=self.body(),
            headers=self.headers(),
        )


def resource_unavailable(
    code: str,
    message: str,
    *,
    retry_after_sec: Optional[int] = None,
    **kwargs: Any,
) -> ResourceUnavailable:
    """Build a refusal using the spec table defaults when retry is omitted."""
    if code not in ADMISSION_CONTRACT:
        raise ValueError(f"unknown accelerator admission code: {code}")
    _status, default_retry = ADMISSION_CONTRACT[code]
    retry = default_retry if retry_after_sec is None else int(retry_after_sec)
    return ResourceUnavailable(
        code=code,
        message=message,
        retry_after_sec=retry,
        **kwargs,
    )
