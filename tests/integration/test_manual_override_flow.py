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
ADMIN_PASS = "Super*120120"
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
    Leaves the canary in disabled+locked state at the end so the
    operator's UI state is preserved.
    """
    login(page)
    pid = _find_provider_id(page, TARGET_PROVIDER_NAME)
    print(f"\n>>> target provider id = {pid}")

    initial = _provider_state_via_api(page, pid)
    print(f">>> STEP 1 initial: enabled={initial['enabled']} "
          f"manual_override_active={initial['manual_override_active']}")
    assert initial["enabled"] is False, "expected disabled at start"
    assert initial["manual_override_active"] is True, "expected lock at start"

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
    print(">>> STEP 3 click 'Yes, release & enable'")
    confirm_button.click()
    time.sleep(3)

    after_release = _provider_state_via_api(page, pid)
    print(f">>> STEP 4 after release: enabled={after_release['enabled']} "
          f"manual_override_active={after_release['manual_override_active']}")
    # The v3.8.6 fix: lock cleared AND provider re-enabled
    assert after_release["manual_override_active"] is False, "release should clear lock"
    assert after_release["enabled"] is True, (
        "v3.8.6: 'Release & re-enable all' should also set enabled=True — "
        "operator no longer sees a stuck-disabled provider after release"
    )
    print(">>> ✅ FIX CONFIRMED: provider re-enabled after release click")

    # Restore canary state: disabled+locked. The toggle endpoint sets
    # enabled=False AND manual_override_until=indefinite on Disable click.
    r = page.request.patch(f"{BASE_URL}/api/providers/{pid}/toggle")
    print(f">>> STEP 5 disable toggle: {r.json()}")
    assert r.json()["enabled"] is False
    assert r.json()["manual_override_active"] is True

    final = _provider_state_via_api(page, pid)
    print(f">>> FINAL canary state: enabled={final['enabled']} "
          f"manual_override_active={final['manual_override_active']}")
    assert final["enabled"] is False
    assert final["manual_override_active"] is True
