"""v5.23 slice 1 — read-only local accelerator telemetry.

This is resource telemetry (VRAM/RAM/ollama ps), not MCP
capability-signalling back-pressure (docs/5.10).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.resources.probe import (
    GpuSnapshot,
    HostSnapshot,
    OllamaResident,
    _bytes_to_mb,
    _disabled_snapshot,
    _nvidia_smi_path,
    _probe_nvidia_smi,
    collect_snapshot,
    reset_probe_cache,
    snapshot_as_api,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    reset_probe_cache()
    yield
    reset_probe_cache()


def test_bytes_to_mb():
    assert _bytes_to_mb(None) is None
    assert _bytes_to_mb(0) == 0
    assert _bytes_to_mb(6113 * 1024 * 1024) == 6113


def test_disabled_snapshot_is_empty():
    snap = _disabled_snapshot()
    assert snap.enabled is False
    assert snap.gpus == []
    assert snap.ollama_ok is False
    body = snapshot_as_api(snap)
    assert body["enabled"] is False
    assert body["accelerators"] == []
    assert "Resource admission" in body["note"]


@pytest.mark.asyncio
async def test_collect_snapshot_is_noop_when_disabled(monkeypatch):
    """G6: LOCAL_ACCEL_ENABLED=false must not shell out or hit Ollama."""
    from app.config import settings

    monkeypatch.setattr(settings, "local_accel_enabled", False)

    def boom(*_a, **_k):
        raise AssertionError("probe must not run when disabled")

    monkeypatch.setattr("app.resources.probe._probe_gpus", boom)
    monkeypatch.setattr("app.resources.probe._probe_ram", boom)
    monkeypatch.setattr("app.resources.probe._probe_ollama_ps", boom)

    snap = await collect_snapshot(force=True)
    assert snap.enabled is False
    assert snap.gpus == []


@pytest.mark.asyncio
async def test_collect_snapshot_failsoft_without_nvidia(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "local_accel_enabled", True)
    monkeypatch.setattr(settings, "local_accel_vram_total_mb", 0)
    monkeypatch.setattr(settings, "local_accel_ollama_url", "http://127.0.0.1:9")
    monkeypatch.setattr("app.resources.probe._probe_nvml", lambda: [])
    monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", lambda: [])
    monkeypatch.setattr(
        "app.resources.probe._probe_ram",
        lambda: (32000, 9000, 4000, 500, []),
    )

    async def fake_ps(_url):
        return [], ["ollama_unreachable"], False

    monkeypatch.setattr("app.resources.probe._probe_ollama_ps", fake_ps)

    snap = await collect_snapshot(force=True)
    assert snap.enabled is True
    assert "vram_unavailable" in snap.degraded
    assert snap.ram_available_mb == 9000
    assert snap.ollama_ok is False
    body = snapshot_as_api(snap)
    assert body["accelerators"][0]["id"] == "local-gpu-0"
    assert body["accelerators"][0]["state"] == "unobserved"
    assert body["host"]["ram_available_mb"] == 9000


@pytest.mark.asyncio
async def test_collect_snapshot_uses_configured_vram_when_smi_missing(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "local_accel_enabled", True)
    monkeypatch.setattr(settings, "local_accel_vram_total_mb", 6113)
    monkeypatch.setattr("app.resources.probe._probe_nvml", lambda: [])
    monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", lambda: [])
    monkeypatch.setattr(
        "app.resources.probe._probe_ram",
        lambda: (32000, 12000, None, None, []),
    )

    async def fake_ps(_url):
        return [OllamaResident(name="coder", size_mb=4700, size_vram_mb=4500)], [], True

    monkeypatch.setattr("app.resources.probe._probe_ollama_ps", fake_ps)

    snap = await collect_snapshot(force=True)
    assert snap.gpus[0].source == "configured"
    assert snap.gpus[0].vram_total_mb == 6113
    assert snap.ollama_ok is True
    assert snap.ollama_models[0].name == "coder"
    body = snapshot_as_api(snap)
    assert body["accelerators"][0]["resident_models"][0]["name"] == "coder"


def test_nvidia_smi_missing_returns_empty(monkeypatch):
    monkeypatch.setattr("app.resources.probe._nvidia_smi_path", lambda: None)
    assert _probe_nvidia_smi() == []


def test_nvidia_smi_path_uses_which(monkeypatch, tmp_path):
    fake = tmp_path / "nvidia-smi.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr("app.resources.probe.shutil.which", lambda _n: str(fake))
    assert _nvidia_smi_path() == str(fake)


def test_snapshot_as_api_shape():
    snap = HostSnapshot(
        enabled=True,
        ts=1.0,
        gpus=[GpuSnapshot(
            accelerator_id="local-gpu-0",
            name="RTX PRO 500",
            vram_total_mb=6113,
            vram_used_mb=5500,
            vram_free_mb=613,
            source="nvml",
        )],
        ram_total_mb=32000,
        ram_available_mb=10000,
        ollama_ok=True,
        ollama_models=[OllamaResident(name="qwen3-coder:30b", size_mb=18600, size_vram_mb=2800)],
    )
    body = snapshot_as_api(snap)
    assert body["enabled"] is True
    assert body["accelerators"][0]["vram_used_mb"] == 5500
    assert body["ollama"]["models"][0]["name"] == "qwen3-coder:30b"
    assert body["host"]["swap_rate_known"] is False


def test_observe_local_accelerator_never_raises():
    from app.observability.prometheus import observe_local_accelerator

    observe_local_accelerator(SimpleNamespace(enabled=False))
    observe_local_accelerator(HostSnapshot(
        enabled=True,
        ts=0,
        gpus=[GpuSnapshot("local-gpu-0", "x", vram_used_mb=100)],
        ram_available_mb=8000,
    ))


def test_router_mounted_in_main():
    src = Path("app/main.py").read_text(encoding="utf-8")
    assert "local_accelerators_router" in src
    assert "app.api.local_accelerators" in src


def test_settings_exist():
    from app.config import settings
    from app.config_runtime import SCHEMA

    assert settings.local_accel_enabled is False
    assert settings.local_accel_ollama_url.startswith("http://")
    assert settings.local_accel_vram_total_mb == 0
    for key in (
        "local_accel_enabled",
        "local_accel_backend",
        "local_accel_ollama_url",
        "local_accel_vram_total_mb",
        "local_accel_probe_cache_sec",
        "local_accel_probe_interval_sec",
    ):
        assert key in SCHEMA, key
