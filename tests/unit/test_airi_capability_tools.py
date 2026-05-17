"""AIRI capability-grounding tools (v4.0.2).

explain_routing now describes the adaptation layer, and get_model_capabilities
exposes per-provider native-vs-emulated tool/reasoning/vision support — so AIRI
stops guessing 'a request will fail on a non-native provider'.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.airi import tools
from app.models.db import Provider, ModelCapability


@pytest_asyncio.fixture
async def cap_env():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(ModelCapability))
        await c.execute(delete(Provider).where(Provider.name.like("cap-%")))
        await c.commit()
    async with AsyncSessionLocal() as c:
        native = Provider(name="cap-anthropic", provider_type="anthropic",
                          priority=5, enabled=True)
        emul = Provider(name="cap-grokweb", provider_type="grok-web",
                        priority=1, enabled=True)
        c.add_all([native, emul])
        await c.commit()
        await c.refresh(native)
        await c.refresh(emul)
        c.add(ModelCapability(provider_id=native.id, model_id="claude-opus-4-7",
                              native_tools=True, native_reasoning=True,
                              native_vision=True, tool_call_success_rate=0.99))
        c.add(ModelCapability(provider_id=emul.id, model_id="grok-3",
                              native_tools=False, native_reasoning=False,
                              native_vision=False, tool_call_success_rate=0.71))
        await c.commit()
    yield


# ── get_model_capabilities ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capabilities_report_native_vs_emulated(cap_env):
    out = await tools.run_tool("get_model_capabilities", {})
    by_model = {m["model"]: m for m in out["models"]}
    assert by_model["claude-opus-4-7"]["native_tools"] is True
    assert by_model["grok-3"]["native_tools"] is False
    assert "EMULATED" in by_model["grok-3"]["adaptation"]
    assert "native" in by_model["claude-opus-4-7"]["adaptation"]


@pytest.mark.asyncio
async def test_capabilities_surface_vision_strip(cap_env):
    out = await tools.run_tool("get_model_capabilities", {})
    grok = next(m for m in out["models"] if m["model"] == "grok-3")
    assert "STRIPPED" in grok["adaptation"]          # vision is the lossy path
    assert grok["tool_call_success_rate"] == 0.71
    assert "adapted, not failed" in out["note"]


# ── explain_routing now covers the adaptation layer ──────────────────────────

def test_explain_routing_describes_adaptation():
    import json
    info = tools._explain_routing()
    assert "adaptation_layer" in info
    al_text = json.dumps(info["adaptation_layer"]).upper()
    # tool emulation incl. streaming SSE is described
    assert "EMULAT" in al_text and "SSE" in al_text
    # the honest residual gaps are present, including vision stripping
    gaps = " ".join(info["residual_gaps"]).lower()
    assert "vision" in gaps and "stripped" in gaps


def test_capability_tools_registered_read_only():
    names = {t["name"] for t in tools.TOOL_SCHEMAS}
    assert "get_model_capabilities" in names
    assert "get_model_capabilities" in tools.READ_ONLY_TOOLS
