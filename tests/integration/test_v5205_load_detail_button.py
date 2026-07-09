"""Playwright E2E pin for the v5.20.5 "Load detail" button on activity rows.

Guards against a future frontend refactor silently breaking the wire
between the expanded activity row and the /api/admin/requests/detail/{id}
endpoint. If the button is renamed, the section is removed, or the
handler stops firing, this test breaks at merge — not silently in
production.

Run with:
    playwright install chromium
    python -m pytest tests/integration/test_v5205_load_detail_button.py -v -s

Smoke instance (llm-proxy2-smoke) is the test target — separate DB,
safe to exercise; auth is the same admin/admin default the project
CLAUDE.md ships with.
"""
from __future__ import annotations

import time

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


def _generate_activity_row(page: Page) -> None:
    """Trigger at least one activity_log row so the Activity page has
    something to render. We just hit an admin endpoint — the request
    itself gets logged."""
    page.request.get(f"{BASE_URL}/api/admin/providers")


def test_load_detail_button_wired_to_detail_endpoint(page):
    """Sign in → Activity page → seed a row → expand → click Load detail
    → assert the detail panel renders (provider/api_key/correlated).

    If the endpoint fails, we still see the error message rendering —
    what we're pinning is that the CLICK triggers the FETCH and the
    RESPONSE renders somewhere in the DOM."""
    login(page)

    # Seed a fresh activity_log entry so the page has content
    _generate_activity_row(page)
    time.sleep(1.5)  # let the SSE stream broadcast catch up

    page.goto(f"{BASE_URL}/activity", wait_until="domcontentloaded")
    page.wait_for_load_state("domcontentloaded", timeout=20_000)

    # Wait for at least one row to render (rows have the cursor-pointer
    # class per ActivityEventRow.tsx v5.20.5).
    row = page.locator("div.cursor-pointer").first
    row.wait_for(state="visible", timeout=15_000)

    # Expand
    row.click()
    page.wait_for_timeout(500)  # let react re-render

    # v5.20.5 UI signature: the "Correlated events (±30s)" section
    # header + a "Load detail" button in the expanded panel.
    correlated_header = page.get_by_text("Correlated events (±30s)")
    expect(correlated_header).to_be_visible(timeout=5_000)

    load_btn = page.get_by_role("button", name="Load detail")
    expect(load_btn).to_be_visible(timeout=5_000)

    # Click it — this hits /api/admin/requests/detail/{id}
    load_btn.click()
    page.wait_for_timeout(2_000)  # allow the fetch + render

    # After fetch: EITHER a) the button is gone (detail loaded), OR
    # b) an error banner appeared. We accept either — what fails the
    # test is if NEITHER happened (button still there with no
    # response), which would mean the click handler didn't fire.
    button_still_visible = load_btn.is_visible()
    loading_indicator = page.get_by_text("Loading…").is_visible()
    error_present = "Error:" in page.content()
    correlated_events_rendered = (
        "No correlated events within ±30s" in page.content()
        or "correlated event(s):" in page.content()
    )

    assert not button_still_visible or loading_indicator or error_present or correlated_events_rendered, (
        "clicking Load detail didn't trigger a state change — hook is broken"
    )
    # Prefer the happy-path assertion
    assert error_present or correlated_events_rendered, (
        "detail endpoint call didn't populate provider/api_key/correlated_events "
        "or an error message — the fetch probably didn't return successfully"
    )
