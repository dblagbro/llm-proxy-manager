"""Playwright E2E pin for the v5.21.2 default-refuse-tolerance select.

Complements the v5.20.10 bundled pin (which also checks for this
element as a side-effect). This test verifies the select's full
contract:

  1. Select renders with all 4 expected options
  2. Changing the value + saving PATCHes ``default_refuse_tolerance``
     on the target key
  3. Reopening the edit modal reflects the persisted value

The full round-trip catches regressions the v5.20.10 pin can't —
e.g. the frontend rendering the select but the save wire being
broken, or the backend PATCH accepting the field but not persisting
it.

Run with:
    playwright install chromium
    python -m pytest tests/integration/test_v5212_default_refuse_tolerance_ui_pin.py -v -s
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright, Page, expect


BASE_URL = "https://www.voipguru.org/llm-proxy2-smoke"
ADMIN_USER = "admin"
ADMIN_PASS = "admin"


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
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
    page.wait_for_load_state("domcontentloaded")
    page.fill('input[autocomplete="username"]', ADMIN_USER)
    page.fill('input[autocomplete="current-password"]', ADMIN_PASS)
    page.click('button[type="submit"]')
    page.wait_for_function(
        "() => !window.location.href.includes('/login')",
        timeout=15_000,
    )
    page.wait_for_load_state("domcontentloaded")


def _open_first_key_edit_modal(page: Page):
    """Sign in → Keys page → click the first Edit button. Returns
    with the modal open."""
    page.goto(f"{BASE_URL}/keys", wait_until="domcontentloaded")
    page.wait_for_timeout(2_000)
    edit_btn = page.get_by_role("button", name="Edit").first
    edit_btn.wait_for(state="visible", timeout=15_000)
    edit_btn.click()
    page.wait_for_timeout(1_500)


def test_default_rt_select_renders_with_all_options(page):
    login(page)
    _open_first_key_edit_modal(page)

    # Label present
    expect(page.get_by_text("Default refuse-tolerance (LMRH)")).to_be_visible(timeout=5_000)

    # All 4 options present (including the "no default" clear option)
    html = page.content()
    for opt in ('(none — no default)', 'strict', 'default', 'lenient'):
        assert opt in html, f"missing default_refuse_tolerance option: {opt}"


def test_default_rt_end_to_end_round_trip(page):
    """FULL contract pin: change select to 'strict' → save → reopen →
    verify persisted. If the frontend save-wire breaks OR the backend
    doesn't persist the field, this fails."""
    login(page)
    _open_first_key_edit_modal(page)

    # Find the select (there's only one <select> in the compliance
    # editor panel that matches this label)
    select_locator = page.locator("select").filter(
        has=page.locator("option", has_text="strict")
    ).first
    select_locator.wait_for(state="visible", timeout=5_000)

    # Record current value so we can restore it
    original_value = select_locator.evaluate("el => el.value")

    # Pick a distinct value that differs from the current one
    new_value = "lenient" if original_value != "lenient" else "strict"
    select_locator.select_option(new_value)
    page.wait_for_timeout(300)

    # Save
    save_btn = page.get_by_role("button", name="Save").first
    save_btn.wait_for(state="visible", timeout=5_000)
    save_btn.click()

    # Wait for the modal to close (indicates PATCH succeeded)
    page.wait_for_timeout(2_500)

    # Reopen the SAME key's edit modal and verify the select shows
    # the new value.
    _open_first_key_edit_modal(page)
    reopened_select = page.locator("select").filter(
        has=page.locator("option", has_text="strict")
    ).first
    reopened_select.wait_for(state="visible", timeout=5_000)
    persisted_value = reopened_select.evaluate("el => el.value")
    assert persisted_value == new_value, (
        f"expected {new_value!r} to persist, got {persisted_value!r} "
        f"(original was {original_value!r})"
    )

    # Restore to original for test isolation
    reopened_select.select_option(original_value if original_value else '')
    page.wait_for_timeout(300)
    save_btn2 = page.get_by_role("button", name="Save").first
    save_btn2.click()
    page.wait_for_timeout(2_000)
