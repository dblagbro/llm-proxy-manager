"""v5.23 — read-only local accelerator telemetry (slice 1).

No admission. Feature-flag off is a complete no-op. Missing NVML /
nvidia-smi / Ollama degrade the snapshot instead of raising.
"""
from __future__ import annotations

import pytest

from app.resources.probe import (
    GpuSnapshot,
    HostSnapshot,
    OllamaResident,
    collect_snapshot,
    reset_probe_cache,
    snapshot_as_api,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    reset_probe_cache()
    yield
    reset_probe_cache()


class TestDisabledIsNoop:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty_and_skips_probes(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "local_accel_enabled", False)

        def _boom(*_a, **_k):
            raise AssertionError("probe must not run when LOCAL_ACCEL_ENABLED=false")

        monkeypatch.setattr("app.resources.probe._probe_nvml", _boom)
        monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", _boom)
        monkeypatch.setattr("app.resources.probe._probe_ram", _boom)
        monkeypatch.setattr("app.resources.probe._probe_ollama_ps", _boom)

        snap = await collect_snapshot(force=True)
        assert snap.enabled is False
        assert snap.gpus == []
        assert snap.ollama_models == []
        body = snapshot_as_api(snap)
        assert body["enabled"] is False
        assert body["accelerators"] == []
        assert body["ollama"]["ok"] is False


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_missing_nvidia_smi_degrades_not_raises(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "local_accel_enabled", True)
        monkeypatch.setattr(settings, "local_accel_vram_total_mb", 0)
        monkeypatch.setattr("app.resources.probe._probe_nvml", lambda: [])
        monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", lambda: [])
        monkeypatch.setattr(
            "app.resources.probe._probe_ram",
            lambda: (32768, 10240, 4096, 512, []),
        )

        async def _no_ollama(_url):
            return [], ["ollama_unreachable"], False

        monkeypatch.setattr("app.resources.probe._probe_ollama_ps", _no_ollama)

        snap = await collect_snapshot(force=True)
        assert snap.enabled is True
        assert "vram_unavailable" in snap.degraded
        assert "ollama_unreachable" in snap.degraded
        assert snap.ram_available_mb == 10240
        body = snapshot_as_api(snap)
        assert body["accelerators"][0]["id"] == "local-gpu-0"
        assert body["accelerators"][0]["source"] == "none"

    @pytest.mark.asyncio
    async def test_configured_vram_fallback(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "local_accel_enabled", True)
        monkeypatch.setattr(settings, "local_accel_vram_total_mb", 6113)
        monkeypatch.setattr("app.resources.probe._probe_nvml", lambda: [])
        monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", lambda: [])
        monkeypatch.setattr(
            "app.resources.probe._probe_ram",
            lambda: (32768, 9000, None, None, []),
        )

        async def _ok(_url):
            return [OllamaResident(name="coder", size_mb=4700, size_vram_mb=4500)], [], True

        monkeypatch.setattr("app.resources.probe._probe_ollama_ps", _ok)

        snap = await collect_snapshot(force=True)
        assert snap.gpus[0].source == "configured"
        assert snap.gpus[0].vram_total_mb == 6113
        assert snap.ollama_ok is True
        assert snap.ollama_models[0].name == "coder"
        body = snapshot_as_api(snap)
        assert body["accelerators"][0]["resident_models"][0]["name"] == "coder"

    @pytest.mark.asyncio
    async def test_nvml_wins_over_nvidia_smi(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "local_accel_enabled", True)
        monkeypatch.setattr(
            "app.resources.probe._probe_nvml",
            lambda: [GpuSnapshot(
                accelerator_id="local-gpu-0",
                name="RTX PRO 500",
                vram_total_mb=6113,
                vram_used_mb=5500,
                vram_free_mb=613,
                source="nvml",
            )],
        )

        def _smi_must_not_run():
            raise AssertionError("nvidia-smi is fallback only")

        monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", _smi_must_not_run)
        monkeypatch.setattr(
            "app.resources.probe._probe_ram",
            lambda: (32768, 8000, None, None, []),
        )

        async def _empty(_url):
            return [], [], True

        monkeypatch.setattr("app.resources.probe._probe_ollama_ps", _empty)

        snap = await collect_snapshot(force=True)
        assert snap.gpus[0].source == "nvml"
        assert snap.gpus[0].vram_used_mb == 5500


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_reuses_cache(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "local_accel_enabled", True)
        monkeypatch.setattr(settings, "local_accel_probe_cache_sec", 30.0)
        calls = {"n": 0}

        def _gpu():
            calls["n"] += 1
            return [GpuSnapshot(accelerator_id="local-gpu-0", name="x", source="nvml")]

        monkeypatch.setattr("app.resources.probe._probe_nvml", _gpu)
        monkeypatch.setattr("app.resources.probe._probe_nvidia_smi", lambda: [])
        monkeypatch.setattr(
            "app.resources.probe._probe_ram",
            lambda: (1000, 500, None, None, []),
        )

        async def _empty(_url):
            return [], [], True

        monkeypatch.setattr("app.resources.probe._probe_ollama_ps", _empty)

        a = await collect_snapshot()
        b = await collect_snapshot()
        assert calls["n"] == 1
        assert a is b

        c = await collect_snapshot(force=True)
        assert calls["n"] == 2
        assert c is not a


class TestApiShape:
    def test_disabled_payload_names_the_concern(self):
        body = snapshot_as_api(HostSnapshot(enabled=False, ts=1.0))
        assert "Resource admission" in body["note"]
        assert "MCP" in body["note"]

    def test_observe_local_accelerator_disabled_is_noop(self):
        from app.observability.prometheus import observe_local_accelerator
        observe_local_accelerator(HostSnapshot(enabled=False, ts=1.0))
