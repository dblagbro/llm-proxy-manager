"""Read-only local accelerator telemetry.

Collects VRAM (NVML, then nvidia-smi), host RAM, and Ollama ``GET /api/ps``.
Every source is fail-soft: a missing binary, a refused connection, or an
import error degrades the snapshot and is recorded on ``degraded`` — the
caller never sees an exception.

Cached for ``LOCAL_ACCEL_PROBE_CACHE_SEC`` (default 2s) so a dashboard
poll does not shell out on every request.

Gated by ``LOCAL_ACCEL_ENABLED``. When false this is a complete no-op
(G6 in the 5.23 spec): no subprocess, no HTTP, no Prometheus writes.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Snapshot cache. Tests reset via ``reset_probe_cache()``.
_CACHE: tuple[float, "HostSnapshot"] | None = None
_LOGGED_MISSING: set[str] = set()


@dataclass
class GpuSnapshot:
    accelerator_id: str
    name: str
    vram_total_mb: Optional[int] = None
    vram_used_mb: Optional[int] = None
    vram_free_mb: Optional[int] = None
    source: str = "none"  # nvml | nvidia-smi | configured | none


@dataclass
class OllamaResident:
    name: str
    size_mb: Optional[int] = None
    size_vram_mb: Optional[int] = None


@dataclass
class HostSnapshot:
    enabled: bool
    ts: float
    gpus: list[GpuSnapshot] = field(default_factory=list)
    ram_total_mb: Optional[int] = None
    ram_available_mb: Optional[int] = None
    swap_total_mb: Optional[int] = None
    swap_used_mb: Optional[int] = None
    swap_rate_known: bool = False
    ollama_ok: bool = False
    ollama_models: list[OllamaResident] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reset_probe_cache() -> None:
    """Test helper — drop the in-process snapshot cache."""
    global _CACHE
    _CACHE = None


def _cache_ttl_sec() -> float:
    return float(getattr(settings, "local_accel_probe_cache_sec", 2.0) or 2.0)


def _ollama_url() -> str:
    return str(getattr(settings, "local_accel_ollama_url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434")


def _configured_vram_total_mb() -> int:
    return int(getattr(settings, "local_accel_vram_total_mb", 0) or 0)


def _log_missing_once(key: str, msg: str) -> None:
    if key in _LOGGED_MISSING:
        return
    _LOGGED_MISSING.add(key)
    logger.info(msg)


def _bytes_to_mb(n: int | float | None) -> Optional[int]:
    if n is None:
        return None
    return int(n) // (1024 * 1024)


# ── VRAM ─────────────────────────────────────────────────────────────────────


def _probe_nvml() -> list[GpuSnapshot]:
    try:
        import pynvml  # type: ignore
    except Exception:
        return []
    try:
        pynvml.nvmlInit()
    except Exception:
        return []
    out: list[GpuSnapshot] = []
    try:
        count = int(pynvml.nvmlDeviceGetCount())
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            raw_name = pynvml.nvmlDeviceGetName(handle)
            name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else str(raw_name)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            total = _bytes_to_mb(mem.total)
            used = _bytes_to_mb(mem.used)
            free = _bytes_to_mb(mem.free)
            out.append(GpuSnapshot(
                accelerator_id=f"local-gpu-{i}",
                name=name,
                vram_total_mb=total,
                vram_used_mb=used,
                vram_free_mb=free,
                source="nvml",
            ))
    except Exception:
        return []
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
    return out


def _nvidia_smi_path() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in (
            Path(r"C:\Windows\System32\nvidia-smi.exe"),
            Path(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def _probe_nvidia_smi() -> list[GpuSnapshot]:
    exe = _nvidia_smi_path()
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    out: list[GpuSnapshot] = []
    for i, line in enumerate(proc.stdout.strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            total, used, free = int(float(parts[1])), int(float(parts[2])), int(float(parts[3]))
        except ValueError:
            continue
        out.append(GpuSnapshot(
            accelerator_id=f"local-gpu-{i}",
            name=parts[0],
            vram_total_mb=total,
            vram_used_mb=used,
            vram_free_mb=free,
            source="nvidia-smi",
        ))
    return out


def _probe_gpus() -> tuple[list[GpuSnapshot], list[str]]:
    degraded: list[str] = []
    gpus = _probe_nvml()
    if gpus:
        return gpus, degraded
    gpus = _probe_nvidia_smi()
    if gpus:
        return gpus, degraded
    configured = _configured_vram_total_mb()
    if configured > 0:
        _log_missing_once("nvml", "NVML/nvidia-smi unavailable; using LOCAL_ACCEL_VRAM_TOTAL_MB")
        return [GpuSnapshot(
            accelerator_id="local-gpu-0",
            name="configured",
            vram_total_mb=configured,
            vram_used_mb=None,
            vram_free_mb=None,
            source="configured",
        )], ["vram_source=configured"]
    _log_missing_once("nvml", "NVML and nvidia-smi unavailable; VRAM fields omitted")
    return [], ["vram_unavailable"]


# ── RAM / swap ───────────────────────────────────────────────────────────────


def _ram_via_psutil() -> Optional[tuple[int, int, Optional[int], Optional[int]]]:
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        return (
            _bytes_to_mb(vm.total) or 0,
            _bytes_to_mb(vm.available) or 0,
            _bytes_to_mb(sm.total),
            _bytes_to_mb(sm.used),
        )
    except Exception:
        return None


def _ram_via_proc_meminfo() -> Optional[tuple[int, int, Optional[int], Optional[int]]]:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    kv: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        num = rest.strip().split()[0]
        try:
            kv[key] = int(num)  # kB
        except ValueError:
            continue
    total_kb = kv.get("MemTotal")
    avail_kb = kv.get("MemAvailable")
    if total_kb is None or avail_kb is None:
        return None
    swap_total = kv.get("SwapTotal")
    swap_free = kv.get("SwapFree")
    swap_used = (swap_total - swap_free) if swap_total is not None and swap_free is not None else None
    return total_kb // 1024, avail_kb // 1024, (swap_total // 1024 if swap_total is not None else None), swap_used


def _ram_via_windows() -> Optional[tuple[int, int, Optional[int], Optional[int]]]:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        total = _bytes_to_mb(stat.ullTotalPhys) or 0
        avail = _bytes_to_mb(stat.ullAvailPhys) or 0
        swap_total = _bytes_to_mb(stat.ullTotalPageFile)
        swap_avail = _bytes_to_mb(stat.ullAvailPageFile)
        swap_used = None
        if swap_total is not None and swap_avail is not None:
            swap_used = max(0, swap_total - swap_avail)
        return total, avail, swap_total, swap_used
    except Exception:
        return None


def _probe_ram() -> tuple[Optional[int], Optional[int], Optional[int], Optional[int], list[str]]:
    degraded: list[str] = []
    for fn in (_ram_via_psutil, _ram_via_windows, _ram_via_proc_meminfo):
        got = fn()
        if got:
            return (*got, degraded)
    _log_missing_once("ram", "RAM probe unavailable (no psutil / platform fallback)")
    return None, None, None, None, ["ram_unavailable"]


# ── Ollama residency ─────────────────────────────────────────────────────────


async def _probe_ollama_ps(base_url: str) -> tuple[list[OllamaResident], list[str], bool]:
    url = base_url.rstrip("/") + "/api/ps"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return [], ["ollama_unreachable"], False
    models: list[OllamaResident] = []
    for m in data.get("models") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "")
        if not name:
            continue
        size = m.get("size")
        size_vram = m.get("size_vram")
        models.append(OllamaResident(
            name=name,
            size_mb=_bytes_to_mb(size) if isinstance(size, (int, float)) else None,
            size_vram_mb=_bytes_to_mb(size_vram) if isinstance(size_vram, (int, float)) else None,
        ))
    return models, [], True


# ── Public API ───────────────────────────────────────────────────────────────


def _disabled_snapshot() -> HostSnapshot:
    return HostSnapshot(enabled=False, ts=time.time())


async def collect_snapshot(*, force: bool = False) -> HostSnapshot:
    """Return a cached (or freshly collected) host snapshot.

    When ``LOCAL_ACCEL_ENABLED`` is false, returns an empty disabled
    snapshot and does not touch NVML, nvidia-smi, or Ollama.
    """
    global _CACHE
    if not bool(getattr(settings, "local_accel_enabled", False)):
        return _disabled_snapshot()

    now = time.time()
    if not force and _CACHE is not None:
        cached_at, snap = _CACHE
        if now - cached_at < _cache_ttl_sec():
            return snap

    gpus, gpu_deg = _probe_gpus()
    ram_total, ram_avail, swap_total, swap_used, ram_deg = _probe_ram()
    models, ollama_deg, ollama_ok = await _probe_ollama_ps(_ollama_url())

    snap = HostSnapshot(
        enabled=True,
        ts=now,
        gpus=gpus,
        ram_total_mb=ram_total,
        ram_available_mb=ram_avail,
        swap_total_mb=swap_total,
        swap_used_mb=swap_used,
        swap_rate_known=False,  # Windows has no sin/sout; never pretend
        ollama_ok=ollama_ok,
        ollama_models=models,
        degraded=gpu_deg + ram_deg + ollama_deg,
    )
    _CACHE = (now, snap)
    _publish_metrics(snap)
    return snap


def _publish_metrics(snap: HostSnapshot) -> None:
    try:
        from app.observability.prometheus import observe_local_accelerator
        observe_local_accelerator(snap)
    except Exception:
        pass


def snapshot_as_api(snap: HostSnapshot) -> dict[str, Any]:
    """Shape returned by ``GET /api/local/accelerators``."""
    accelerators = []
    for gpu in snap.gpus:
        accelerators.append({
            "id": gpu.accelerator_id,
            "name": gpu.name,
            "state": "observed",
            "vram_total_mb": gpu.vram_total_mb,
            "vram_used_mb": gpu.vram_used_mb,
            "vram_free_mb": gpu.vram_free_mb,
            "source": gpu.source,
            "resident_models": [
                {"name": m.name, "size_mb": m.size_mb, "size_vram_mb": m.size_vram_mb}
                for m in snap.ollama_models
            ],
        })
    if snap.enabled and not accelerators:
        accelerators.append({
            "id": "local-gpu-0",
            "name": "unknown",
            "state": "unobserved",
            "vram_total_mb": None,
            "vram_used_mb": None,
            "vram_free_mb": None,
            "source": "none",
            "resident_models": [
                {"name": m.name, "size_mb": m.size_mb, "size_vram_mb": m.size_vram_mb}
                for m in snap.ollama_models
            ],
        })
    return {
        "enabled": snap.enabled,
        "ts": snap.ts,
        "accelerators": accelerators,
        "host": {
            "ram_total_mb": snap.ram_total_mb,
            "ram_available_mb": snap.ram_available_mb,
            "swap_total_mb": snap.swap_total_mb,
            "swap_used_mb": snap.swap_used_mb,
            "swap_rate_known": snap.swap_rate_known,
        },
        "ollama": {
            "ok": snap.ollama_ok,
            "url": _ollama_url() if snap.enabled else None,
            "models": [
                {"name": m.name, "size_mb": m.size_mb, "size_vram_mb": m.size_vram_mb}
                for m in snap.ollama_models
            ],
        },
        "degraded": list(snap.degraded),
        "note": (
            "Read-only telemetry. Resource admission is a later 5.23 slice. "
            "Not MCP capability-signalling back-pressure."
        ),
    }
