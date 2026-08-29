"""v5.23 slice 4 skeleton — accelerator admission 429/503 contract.

No residency machine. Pins the wire shape so later slices do not fork
a second refusal format. This is resource back-pressure, not MCP
capability-signalling (docs/5.10).
"""
from __future__ import annotations

import pytest

from app.resources.admission import Admit, AdmissionRequest, evaluate
from app.resources.errors import ADMISSION_CONTRACT, resource_unavailable


def test_unknown_code_is_rejected():
    with pytest.raises(ValueError, match="unknown accelerator admission code"):
        resource_unavailable("not_a_real_code", "x")


def test_queue_full_is_429():
    exc = resource_unavailable("queue_full", "Local accelerator queue is full.")
    assert exc.status == 429
    assert exc.headers()["Retry-After"] == "15"
    body = exc.body()
    assert body["error"]["type"] == "resource_unavailable"
    assert body["error"]["code"] == "queue_full"
    assert body["llmp"]["retry_after_sec"] == 15


@pytest.mark.parametrize(
    "code,status,retry",
    [
        ("model_loading", 503, 30),
        ("vram_exhausted", 503, 90),
        ("ram_watermark", 503, 30),
        ("host_swapping", 503, 60),
        ("thrash_guard", 503, 600),
        ("queue_timeout", 503, 0),
        ("residency_not_warm", 503, 30),
    ],
)
def test_host_shaped_codes_are_503(code, status, retry):
    exc = resource_unavailable(code, f"refused: {code}")
    assert exc.status == status
    assert exc.headers()["Retry-After"] == str(retry)
    assert exc.body()["error"]["code"] == code
    resp = exc.to_response()
    assert resp.status_code == status
    assert resp.headers["retry-after"] == str(retry)


def test_explicit_retry_after_overrides_default():
    exc = resource_unavailable(
        "ram_watermark",
        "18.6 GB model needs 21.0 GB RAM, 9.2 GB available.",
        retry_after_sec=45,
        requested_model="qwen3-coder:30b",
        resident_model="coder",
        remedy="Close memory-heavy applications.",
    )
    assert exc.status == 503
    assert exc.retry_after_sec == 45
    llmp = exc.body()["llmp"]
    assert llmp["requested_model"] == "qwen3-coder:30b"
    assert llmp["resident_model"] == "coder"
    assert llmp["remedy"] == "Close memory-heavy applications."


def test_http_exception_carries_retry_after():
    exc = resource_unavailable("host_swapping", "Host is paging.")
    http = exc.to_http_exception()
    assert http.status_code == 503
    assert http.headers["Retry-After"] == "60"
    assert http.detail["error"]["type"] == "resource_unavailable"


def test_evaluate_disabled_is_admit(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "local_accel_enabled", False, raising=False)
    decision = evaluate(AdmissionRequest(model="qwen3-coder:30b"))
    assert isinstance(decision, Admit)


def test_evaluate_enabled_still_admits_without_residency(monkeypatch):
    """Skeleton: flipping the flag must not start refusing traffic."""
    from app.config import settings

    monkeypatch.setattr(settings, "local_accel_enabled", True, raising=False)
    decision = evaluate(AdmissionRequest(model="coder"))
    assert isinstance(decision, Admit)


def test_contract_table_matches_spec_status_rule():
    """429 is client-shaped (queue_full only); everything else is 503."""
    for code, (status, _retry) in ADMISSION_CONTRACT.items():
        if code == "queue_full":
            assert status == 429
        else:
            assert status == 503, code
