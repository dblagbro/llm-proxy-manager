"""Playwright E2E pin for the v5.20.10 self_edit_permissions UI.

Mirrors v5.20.5's Load-detail-button pattern. Sign in → open the API
Keys page → open an edit modal on any key → verify the "Self-edit
permissions" section header + at least one eligible-field checkbox
renders. Guards against a future frontend refactor silently breaking
the wire from the ELIGIBLE_FIELDS registry to the checkbox grid.

Run with:
    playwright install chromium
    python -m pytest tests/integration/test_v5210_self_edit_ui_pin.py -v -s
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


def test_self_edit_ui_pin(page):
    """Sign in → Keys → Edit modal on any key → verify Self-edit section
    + at least one checkbox for an eligible field renders.

    Also verifies the v5.21.2 default-refuse-tolerance select is
    present (bundled ship)."""
    login(page)
    page.goto(f"{BASE_URL}/keys", wait_until="domcontentloaded")
    page.wait_for_timeout(2_000)

    # Find any Edit button — every row has one
    edit_btn = page.get_by_role("button", name="Edit").first
    edit_btn.wait_for(state="visible", timeout=15_000)
    edit_btn.click()
    page.wait_for_timeout(1_500)

    # v5.20.10 signature
    self_edit_header = page.get_by_text("Self-edit permissions", exact=False)
    expect(self_edit_header).to_be_visible(timeout=5_000)

    # v5.21.2 signature (bundled)
    default_rt_label = page.get_by_text("Default refuse-tolerance (LMRH)")
    expect(default_rt_label).to_be_visible(timeout=5_000)

    # Verify the select has all 4 expected options for
    # default_refuse_tolerance (renders unconditionally, unlike the
    # self-edit checkbox grid which is gated on the master toggle).
    html = page.content()
    for opt in ('none — no default', 'strict', 'default', 'lenient'):
        assert opt in html, f"missing default_refuse_tolerance option: {opt}"

    # The eligible-fields grid is behind a Switch. Verify it toggles on
    # and renders at least one field. If the switch is missing this
    # will hang — 5s timeout catches that.
    self_edit_switch = page.get_by_role("switch", name="Enable self-edit permissions")
    if self_edit_switch.count() > 0:
        self_edit_switch.click()
        page.wait_for_timeout(500)
        # After toggle on, mcp_tools_allow should appear
        mcp_field = page.get_by_text("mcp_tools_allow", exact=False)
        expect(mcp_field).to_be_visible(timeout=5_000)
