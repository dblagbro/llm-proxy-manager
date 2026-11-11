"""v5.22.47 — Daybreak Blue (OpenAI Trusted Access for Cyber) passthrough.

Covers:
  - the Codex body translator injects ``access_programs`` only when asked;
  - the daybreak_gate policy: Red hard-block, unauthorized-key block,
    missing-alias 503, normal-model passthrough, and the authorized Blue path.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.providers.codex_translate import chat_completions_to_responses
from app.providers.daybreak import (
    daybreak_gate,
    is_daybreak_blue_request,
    is_daybreak_red_request,
    key_daybreak_tier,
)

# ── body translator ─────────────────────────────────────────────────────────

def _body():
    return {"model": "codex-auto-review", "messages": [{"role": "user", "content": "hi"}]}


def test_access_programs_injected_when_supplied():
    out = chat_completions_to_responses(_body(), access_programs={"cyber": "daybreak_blue"})
    assert out["access_programs"] == {"cyber": "daybreak_blue"}
    # store:false stays hard-set regardless.
    assert out["store"] is False


def test_access_programs_absent_by_default():
    out = chat_completions_to_responses(_body())
    assert "access_programs" not in out


# ── alias / key helpers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("m", ["daybreak", "daybreak-blue", "DAYBREAK", " daybreak_blue "])
def test_is_blue(m):
    assert is_daybreak_blue_request(m)


@pytest.mark.parametrize("m", ["daybreak-red", "daybreak_red"])
def test_is_red(m):
    assert is_daybreak_red_request(m)


@pytest.mark.parametrize("m", ["gpt-5.5", "codex-auto-review", None, ""])
def test_not_daybreak(m):
    assert not is_daybreak_blue_request(m)
    assert not is_daybreak_red_request(m)


def test_key_tier_normalization():
    assert key_daybreak_tier(SimpleNamespace(daybreak_tier=" Blue ")) == "blue"
    assert key_daybreak_tier(SimpleNamespace(daybreak_tier=None)) is None
    assert key_daybreak_tier(SimpleNamespace(daybreak_tier="")) is None
    assert key_daybreak_tier(SimpleNamespace()) is None


# ── gate policy ──────────────────────────────────────────────────────────────

def _key(tier):
    return SimpleNamespace(id="k1", daybreak_tier=tier)


def _alias():
    return SimpleNamespace(provider_id="prov-gmail", model_id="codex-auto-review")


class _FakeDB:
    """Minimal stand-in; emit_event is best-effort inside the gate."""
    def add(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_normal_model_returns_none():
    assert await daybreak_gate(
        _FakeDB(), requested_model="gpt-5.5", key_record=_key("blue"), alias=None
    ) is None


@pytest.mark.asyncio
async def test_red_is_hard_blocked_even_for_blue_key():
    with pytest.raises(HTTPException) as ei:
        await daybreak_gate(
            _FakeDB(), requested_model="daybreak-red", key_record=_key("blue"), alias=_alias()
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_unauthorized_key_blocked():
    with pytest.raises(HTTPException) as ei:
        await daybreak_gate(
            _FakeDB(), requested_model="daybreak", key_record=_key(None), alias=_alias()
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_alias_is_503():
    with pytest.raises(HTTPException) as ei:
        await daybreak_gate(
            _FakeDB(), requested_model="daybreak", key_record=_key("blue"), alias=None
        )
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_authorized_blue_returns_access_programs():
    ap = await daybreak_gate(
        _FakeDB(), requested_model="daybreak", key_record=_key("blue"), alias=_alias()
    )
    assert ap == {"cyber": "daybreak_blue"}
