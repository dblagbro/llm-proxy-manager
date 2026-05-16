"""Playwright audit of the manual-override flow.

Operator reported: with Devin-Anthropic-Max-VG showing "manual override",
they clicked the banner's release button, confirmed, then went to the
provider and saw an "Enable" button (suggesting the provider was disabled).
Expectation mismatch — they expected the click to re-enable.

This test walks through the cycle and captures what each UI surface
actually shows at each step, so we can diagnose what's "backwards".

Run with:
    playwright install chromium
    python -m pytest tests/integration/test_manual_override_flow.py -v -s
"""
from __future__ import annotations

import time

import pytest
from playwright.sync_api import sync_playwright, Page, expect


BASE_URL = "https://www.voipguru.org/llm-proxy2"
ADMIN_USER = "admin"
ADMIN_PASS = "REMOVED-CREDENTIAL-ROTATED-20260828"
TARGET_PROVIDER_NAME = "Devin-Anthropic-Max-VG"


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield br
        br.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(ignore_https_errors=True)
    pg = ctx.new_page()
    yield pg
    ctx.close()


def login(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.wait_for_load_state("networkidle")
    page.fill('input[autocomplete="username"]', ADMIN_USER)
    page.fill('input[autocomplete="current-password"]', ADMIN_PASS)
    page.click('button[type="submit"]')
    page.wait_for_function(
        "() => !window.location.href.includes('/login')",
        timeout=15_000,
    )
    page.wait_for_load_state("networkidle")


def _provider_state_via_api(page: Page, provider_id: str) -> dict:
    r = page.request.get(f"{BASE_URL}/api/providers")
    rows = r.json()
    return next(p for p in rows if p["id"] == provider_id)


def _find_provider_id(page: Page, name: str) -> str:
    r = page.request.get(f"{BASE_URL}/api/providers")
    for p in r.json():
        if p["name"] == name:
            return p["id"]
    raise RuntimeError(f"provider not found: {name}")


def test_release_now_also_enables_v386(page: Page):
    """Post-v3.8.6: clicking 'Release & re-enable all' should release
    the lock AND set enabled=True — matching the operator's intuition.

    v3.10.12 (BUG-027): this test now **self-stages** its own
    precondition and **restores the provider's original state** at the
    end. The earlier version asserted a pre-staged external state
    (`enabled=False, locked`) and, on success, left a live production
    provider disabled — both made it environmentally fragile (it failed
    deterministically once the canary drifted back to enabled).
    """
    login(page)
    pid = _find_provider_id(page, TARGET_PROVIDER_NAME)
    print(f"\n>>> target provider id = {pid}")

    # Capture the provider's real state so we can restore it afterward.
    orig = _provider_state_via_api(page, pid)
    print(f">>> original state: enabled={orig['enabled']} "
          f"manual_override_active={orig['manual_override_active']}")

    # Self-stage the precondition: the release banner only shows when the
    # provider is disabled + locked (manual override). The toggle
    # endpoint, applied to an enabled provider, sets enabled=False AND
    # manual_override_until=indefinite. Stage it only if not already there.
    staged = orig
    if staged["enabled"] or not staged["manual_override_active"]:
        page.request.patch(f"{BASE_URL}/api/providers/{pid}/toggle")
        time.sleep(1)
        staged = _provider_state_via_api(page, pid)
    assert staged["enabled"] is False and staged["manual_override_active"] is True, (
        "could not stage canary into disabled+locked — precondition setup failed"
    )

    page.goto(f"{BASE_URL}/providers")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    banner = page.locator("text=under manual override").first
    assert banner.count() == 1, "expected manual-override banner to be visible"

    # New button label
    release_button = page.get_by_role("button", name="Release & re-enable all")
    assert release_button.count() == 1, (
        f"expected new banner button 'Release & re-enable all' — "
        f"the v3.8.6 fix replaces the v3.7.29 'Release all to AI control' label"
    )

    release_button.first.click()
    confirm_button = page.get_by_role("button", name="Yes, release & enable")
    expect(confirm_button).to_be_visible(timeout=5_000)
    print(">>> click 'Yes, release & enable'")
    confirm_button.click()
    time.sleep(3)

    after_release = _provider_state_via_api(page, pid)
    print(f">>> after release: enabled={after_release['enabled']} "
          f"manual_override_active={after_release['manual_override_active']}")
    # The v3.8.6 fix: lock cleared AND provider re-enabled
    assert after_release["manual_override_active"] is False, "release should clear lock"
    assert after_release["enabled"] is True, (
        "v3.8.6: 'Release & re-enable all' should also set enabled=True — "
        "operator no longer sees a stuck-disabled provider after release"
    )
    print(">>> ✅ FIX CONFIRMED: provider re-enabled after release click")

    # Restore the provider to its ORIGINAL state — never leave a live
    # provider disabled as a test side-effect. After the release flow it
    # is enabled+unlocked; if it started disabled, toggle it back.
    if not orig["enabled"]:
        page.request.patch(f"{BASE_URL}/api/providers/{pid}/toggle")
        time.sleep(1)
    final = _provider_state_via_api(page, pid)
    print(f">>> restored state: enabled={final['enabled']} "
          f"manual_override_active={final['manual_override_active']}")
    assert final["enabled"] is orig["enabled"], (
        "provider was not restored to its original enabled state"
    )
