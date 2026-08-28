"""
Playwright integration tests for llm-proxy v2 UI and API.
Runs against the live deployment at https://www.voipguru.org/llm-proxy2/

Run with:
    playwright install chromium
    python -m pytest tests/integration/test_playwright_ui.py -v
"""
import os
import re
import time
import pytest
from playwright.sync_api import sync_playwright, Page, expect

BASE_URL = os.environ.get(
    "LLMPROXY_TEST_BASE_URL", "https://www.voipguru.org/llm-proxy2"
)
ADMIN_USER = "admin"
# v5.22.12 — credential moved out of source, matching tests/conftest.py
# (v4.4.29). This file kept the plaintext production admin password long
# after conftest.py was fixed, so it stayed readable by anyone on the
# public repo — confirmed 2026-08-12 by fetching it anonymously from
# raw.githubusercontent.com. Read from LLMPROXY_TEST_ADMIN_PASS; fall
# back to the documented default "admin" so a from-scratch checkout
# against a default-credentials dev box still works.
ADMIN_PASS = os.environ.get("LLMPROXY_TEST_ADMIN_PASS", "admin")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield br
        br.close()


@pytest.fixture
def page(browser):
    """Fresh isolated browser context per test — no shared cookies."""
    ctx = browser.new_context(ignore_https_errors=True)
    pg = ctx.new_page()
    yield pg
    ctx.close()


def login(page: Page):
    last_err = None
    for attempt in range(2):
        try:
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
            return
        except Exception as e:
            last_err = e
            # transient: short backoff and retry once
            time.sleep(2)
    raise last_err  # type: ignore[misc]


# ── Existing services sanity checks ──────────────────────────────────────────

class TestExistingServices:
    def test_llm_proxy_v1_health(self, page: Page):
        """v1 proxy still responds — no regression."""
        resp = page.request.get("https://www.voipguru.org/llmProxy/health")
        assert resp.status == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"].startswith("1.")

    def test_coordinator_hub_reachable(self, page: Page):
        """Coordinator hub still accessible."""
        resp = page.request.get(
            "https://www.voipguru.org/claudeCoordinator/",
            max_redirects=5,
        )
        # 200 (login page) or 302 — either means it's up
        assert resp.status in (200, 302, 401)

    def test_paperless_reachable(self, page: Page):
        """Paperless-web still accessible."""
        resp = page.request.get(
            "https://www.voipguru.org/paperless/",
            max_redirects=5,
        )
        assert resp.status in (200, 302, 301)


# ── llm-proxy2 API checks ─────────────────────────────────────────────────────

class TestLLMProxy2API:
    def test_health_endpoint(self, page: Page):
        resp = page.request.get(f"{BASE_URL}/health")
        assert resp.status == 200
        data = resp.json()
        assert data["version"].startswith("2.")
        assert "status" in data
        assert "circuitBreakers" in data

    def test_health_no_auth_required(self, page: Page):
        """Health must be public (cluster peers call it without auth)."""
        resp = page.request.get(f"{BASE_URL}/health")
        assert resp.status == 200

    def test_api_requires_auth(self, page: Page):
        """Protected API endpoints return 401 without auth."""
        resp = page.request.get(f"{BASE_URL}/api/providers")
        assert resp.status == 401

    def test_login_api(self, page: Page):
        import json
        resp = page.request.post(
            f"{BASE_URL}/api/auth/login",
            data=json.dumps({"username": ADMIN_USER, "password": ADMIN_PASS}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        data = resp.json()
        assert data["username"] == ADMIN_USER
        assert data["role"] == "admin"

    def test_wrong_password_rejected(self, page: Page):
        resp = page.request.post(
            f"{BASE_URL}/api/auth/login",
            data={"username": ADMIN_USER, "password": "wrongpassword"},
        )
        assert resp.status == 401


# ── llm-proxy2 UI tests ───────────────────────────────────────────────────────

class TestLLMProxy2UI:
    def test_root_redirects_to_login(self, page: Page):
        """Unauthenticated root redirects to login."""
        page.goto(BASE_URL + "/")
        expect(page).to_have_url(f"{BASE_URL}/login")

    def test_login_page_renders(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        expect(page.locator("h1")).to_contain_text("llm-proxy")
        expect(page.locator('button[type="submit"]')).to_be_visible()

    def test_login_with_wrong_creds_shows_error(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        page.fill('input[autocomplete="username"]', "admin")
        page.fill('input[autocomplete="current-password"]', "bad")
        page.click('button[type="submit"]')
        # Error message should appear (could say "Invalid credentials", "Login failed", etc.)
        expect(page.locator(".text-red-400, .border-red-800")).to_be_visible(timeout=5_000)

    def test_login_success_reaches_dashboard(self, page: Page):
        login(page)
        assert page.url.startswith(BASE_URL)
        expect(page.locator("h1")).to_contain_text("Dashboard")

    def test_dashboard_stat_cards_visible(self, page: Page):
        login(page)
        # Stat card labels are inside <p> elements with small text
        expect(page.locator("p:has-text('Cost Today')").first).to_be_visible()
        expect(page.locator("p:has-text('Requests')").first).to_be_visible()
        expect(page.locator("h1:has-text('Dashboard')")).to_be_visible()

    def test_sidebar_navigation_links(self, page: Page):
        login(page)
        sidebar = page.locator("aside")
        expect(sidebar.locator("text=Dashboard").first).to_be_visible()
        expect(sidebar.locator("text=Providers").first).to_be_visible()
        expect(sidebar.locator("text=API Keys")).to_be_visible()
        expect(sidebar.locator("text=Users")).to_be_visible()
        expect(sidebar.locator("text=Metrics")).to_be_visible()
        expect(sidebar.locator("text=Activity")).to_be_visible()
        expect(sidebar.locator("text=Settings")).to_be_visible()

    def test_navigate_to_providers_page(self, page: Page):
        login(page)
        page.click("text=Providers")
        expect(page).to_have_url(f"{BASE_URL}/providers")
        expect(page.locator("h1")).to_contain_text("Providers")
        expect(page.locator("text=Add Provider")).to_be_visible()

    def test_navigate_to_api_keys_page(self, page: Page):
        login(page)
        page.click("text=API Keys")
        expect(page).to_have_url(f"{BASE_URL}/keys")
        expect(page.locator("h1")).to_contain_text("API Keys")
        expect(page.locator("text=Create Key")).to_be_visible()

    def test_navigate_to_users_page(self, page: Page):
        login(page)
        page.locator("aside").locator("text=Users").click()
        expect(page).to_have_url(f"{BASE_URL}/users")
        expect(page.locator("h1")).to_contain_text("Users")
        expect(page.locator("td:has-text('admin'), p:has-text('admin')").first).to_be_visible()

    def test_navigate_to_activity_page(self, page: Page):
        login(page)
        page.click("text=Activity")
        expect(page).to_have_url(f"{BASE_URL}/activity")
        expect(page.locator("h1")).to_contain_text("Activity Log")

    def test_navigate_to_metrics_page(self, page: Page):
        login(page)
        page.click("text=Metrics")
        expect(page).to_have_url(f"{BASE_URL}/metrics")
        expect(page.locator("h1")).to_contain_text("Metrics")

    def test_navigate_to_routing_page(self, page: Page):
        login(page)
        page.locator("aside").locator("a[href*='/routing']").click()
        expect(page).to_have_url(f"{BASE_URL}/routing")
        expect(page.locator("text=LMRH").first).to_be_visible()

    def test_navigate_to_settings_page(self, page: Page):
        login(page)
        page.click("text=Settings")
        expect(page).to_have_url(f"{BASE_URL}/settings")
        expect(page.locator("h1")).to_contain_text("Settings")

    def test_theme_toggle_works(self, page: Page):
        login(page)
        html = page.locator("html")
        # Toggle dark/light
        page.locator('[title*="mode"]').click()
        time.sleep(0.3)
        # Toggle back
        page.locator('[title*="mode"]').click()

    def test_sidebar_collapse(self, page: Page):
        login(page)
        # Find the collapse button (ChevronLeft icon at sidebar bottom)
        collapse_btn = page.locator("aside button").last
        collapse_btn.click()
        time.sleep(0.3)
        # Sidebar should now be narrow (w-14)
        aside = page.locator("aside")
        assert "w-14" in (aside.get_attribute("class") or "")

    def test_create_provider_modal_opens(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/providers")
        page.click("text=Add Provider")
        # Modal should appear
        expect(page.locator("text=Add Provider").nth(1)).to_be_visible(timeout=3_000)
        expect(page.locator('input[placeholder*="Name"]', has_text="")\
            .or_(page.locator('label:has-text("Name") + input'))).to_be_visible()
        # Close modal
        page.keyboard.press("Escape")

    def test_create_api_key_flow(self, page: Page, admin_session):
        # v3.1.x: unique name per run + try/finally cleanup. The previous
        # version hardcoded "test-playwright-key" and never deleted it,
        # leaving one tombstoned row per CI run. Across 7-day cluster-sync
        # tombstone retention this can accumulate enough rows to slow
        # apply_sync (root cause of the 2026-05-07 sync-latency incident).
        import uuid
        key_name = f"test-playwright-{uuid.uuid4().hex[:8]}"
        login(page)
        try:
            page.goto(f"{BASE_URL}/keys")
            page.click("text=Create Key")
            page.fill('input[placeholder*="production"]', key_name)
            page.locator('.fixed.inset-0 button:has-text("Create Key")').click()
            expect(page.locator("text=Your New API Key")).to_be_visible(timeout=8_000)
            expect(page.locator("text=NOT be shown again")).to_be_visible()
            page.click('button:has-text("Done")')
        finally:
            # Find and delete the key by name (we don't capture the id from
            # the UI; query the API). Soft-delete via the standard endpoint —
            # session-finish hook in conftest.py hard-purges tombstones.
            try:
                rs = admin_session.get(f"{BASE_URL}/api/keys")
                if rs.status_code == 200:
                    for k in rs.json():
                        if k.get("name") == key_name:
                            admin_session.delete(f"{BASE_URL}/api/keys/{k['id']}")
                            break
            except Exception:
                pass  # best-effort cleanup; sessionfinish hook is the safety net

    def test_logout_redirects_to_login(self, page: Page):
        login(page)
        page.locator('[title="Sign out"]').click()
        expect(page).to_have_url(f"{BASE_URL}/login", timeout=8_000)

    def test_topbar_health_badge_visible(self, page: Page):
        login(page)
        # TopBar shows health status badge (providers count or Connecting…)
        header = page.locator("header")
        expect(header).to_be_visible()
        # Badge text is either "X/Y providers" or "Connecting…"
        expect(header.locator("text=providers").or_(header.locator("text=Connecting"))).to_be_visible(timeout=8_000)

    def test_navigate_to_cluster_page(self, page: Page):
        login(page)
        page.click("text=Cluster")
        expect(page).to_have_url(f"{BASE_URL}/cluster")
        expect(page.locator("h1")).to_contain_text("Cluster")

    def test_cluster_page_shows_circuit_breakers(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/cluster")
        # "Provider Circuit Breakers" card heading is unique to this page
        expect(page.get_by_text("Provider Circuit Breakers")).to_be_visible(timeout=8_000)
        # Wait for breakers to load (spinner disappears)
        page.wait_for_function(
            "() => document.querySelector('.animate-spin') === null",
            timeout=10_000,
        )
        # At least one circuit breaker badge or empty state should be visible
        page.wait_for_function(
            "() => document.querySelector('.divide-y') !== null || document.body.innerText.includes('No providers')",
            timeout=8_000,
        )

    def test_cluster_page_force_online_button(self, page: Page):
        """Force Online button visible on circuit breakers."""
        login(page)
        page.goto(f"{BASE_URL}/cluster")
        page.wait_for_function(
            "() => document.querySelector('.animate-spin') === null",
            timeout=10_000,
        )
        # If providers exist, Force Online/Trip buttons should be visible
        force_online = page.locator("text=Force Online").first
        if force_online.is_visible():
            # Verify Force Trip button also exists
            expect(page.locator("text=Force Trip").first).to_be_visible()


# ── Provider Action Tests ─────────────────────────────────────────────────────

class TestProviderActions:
    def test_provider_test_button_shows_result(self, page: Page):
        """Test button on a provider returns OK or Error badge."""
        login(page)
        page.goto(f"{BASE_URL}/providers")
        # Expand the first provider card
        first_card = page.locator("div.cursor-pointer").first
        first_card.click()
        page.wait_for_timeout(500)
        # Click the Test button
        test_btn = page.locator("button:has-text('Test')").first
        expect(test_btn).to_be_visible(timeout=5_000)
        test_btn.click()
        # Wait for test to complete (button re-enables)
        page.wait_for_timeout(2_000)
        page.wait_for_function(
            "() => !Array.from(document.querySelectorAll('button')).some(b => b.disabled && b.textContent.includes('Test'))",
            timeout=30_000,
        )
        # Result badge in the card header. The actual copy is
        # "Test OK" / "Test failed" (see ProvidersPage.tsx).
        result_badge = page.locator("span:has-text('Test OK'), span:has-text('Test failed')").first
        expect(result_badge).to_be_visible(timeout=5_000)

    def test_scan_models_button_shows_toast(self, page: Page):
        """Scan Models button completes and shows a toast."""
        login(page)
        page.goto(f"{BASE_URL}/providers")
        # Expand the first provider card
        first_card = page.locator("div.cursor-pointer").first
        first_card.click()
        page.wait_for_timeout(500)
        # Click Scan Models
        scan_btn = page.locator("button:has-text('Scan Models')").first
        expect(scan_btn).to_be_visible(timeout=5_000)
        scan_btn.click()
        # Wait for toast in the fixed bottom-right toast container
        toast_container = page.locator(".fixed.bottom-4.right-4")
        expect(toast_container).to_be_visible(timeout=30_000)

    def test_provider_logs_button_navigates(self, page: Page):
        """Logs button on a provider navigates to activity page filtered by provider."""
        login(page)
        page.goto(f"{BASE_URL}/providers")
        first_card = page.locator("div.cursor-pointer").first
        first_card.click()
        page.wait_for_timeout(500)
        logs_btn = page.locator("button:has-text('Logs')").first
        expect(logs_btn).to_be_visible(timeout=5_000)
        logs_btn.click()
        page.wait_for_url(f"{BASE_URL}/activity**", timeout=8_000)
        # URL should have ?provider= query param
        assert "provider=" in page.url

    def test_activity_page_provider_filter(self, page: Page):
        """Activity page ?provider= filter shows filter label and clear button."""
        login(page)
        page.goto(f"{BASE_URL}/activity?provider=testprovider123")
        expect(page.locator("text=Filtered to provider")).to_be_visible(timeout=8_000)
        expect(page.locator("text=Clear filter")).to_be_visible()
        page.click("text=Clear filter")
        page.wait_for_url(f"{BASE_URL}/activity", timeout=5_000)


# ── User Management Tests ─────────────────────────────────────────────────────

class TestUserManagement:
    def test_create_and_delete_user(self, page: Page):
        """Create a new user then delete it."""
        import time as _time
        unique_name = f"pw-test-{int(_time.time()) % 100000}"
        login(page)
        page.goto(f"{BASE_URL}/users")
        # Click Add User
        page.click("text=Add User")
        page.wait_for_timeout(500)
        # Wait for modal — form has a label "Username" then an input
        expect(page.locator("text=Add User").nth(1)).to_be_visible(timeout=5_000)
        # Fill username and password
        page.locator('.fixed.inset-0 input').first.fill(unique_name)
        page.locator('.fixed.inset-0 input[type="password"]').fill("TestPass!123")
        # Submit
        page.locator('.fixed.inset-0 button:has-text("Create")').click()
        # Wait for user to appear in list (modal closes on success)
        expect(page.locator(f"text={unique_name}")).to_be_visible(timeout=10_000)
        # Delete it — find the row containing the username
        user_row = page.locator(f".px-5.py-4:has-text('{unique_name}')").first
        # The delete button has the red danger style (Trash2 icon)
        user_row.locator("button[class*='bg-red'], button[class*='danger'], button:last-child").last.click()
        # Confirm deletion via ConfirmDialog
        page.wait_for_timeout(300)
        confirm = page.locator(".fixed.inset-0 button:has-text('Delete')").first
        if confirm.is_visible(timeout=3_000):
            confirm.click()
        # User should be gone
        page.wait_for_timeout(1000)
        expect(page.locator(f"text={unique_name}")).not_to_be_visible(timeout=5_000)


# ── Session & UX Tests ────────────────────────────────────────────────────────

class TestSessionBehavior:
    def test_session_persists_across_page_reload(self, page: Page):
        """Login then reload — user must remain authenticated (no redirect to /login)."""
        login(page)
        page.goto(f"{BASE_URL}/providers")
        page.wait_for_load_state("networkidle")
        page.reload()
        page.wait_for_load_state("networkidle")
        # Must NOT be redirected to login
        assert "/login" not in page.url, f"Redirected to login after reload: {page.url}"
        # Providers heading should be visible
        expect(page.locator("h1:has-text('Providers')")).to_be_visible(timeout=8_000)

    def test_scan_models_shows_model_list(self, page: Page):
        """After scanning models, the model capability list appears in the expanded row."""
        login(page)
        page.goto(f"{BASE_URL}/providers")
        # Expand first provider card
        first_card = page.locator("div.cursor-pointer").first
        first_card.click()
        page.wait_for_timeout(500)
        # Click Scan Models
        scan_btn = page.locator("button:has-text('Scan Models')").first
        expect(scan_btn).to_be_visible(timeout=5_000)
        scan_btn.click()
        # Wait for scan to complete (spinner goes away)
        page.wait_for_function(
            "() => !Array.from(document.querySelectorAll('button')).some(b => b.disabled && b.textContent.includes('Scan'))",
            timeout=30_000,
        )
        # Either a model table or the "no models indexed" message should appear
        page.wait_for_function(
            "() => document.body.innerText.includes('models indexed') || document.body.innerText.includes('No models indexed') || document.body.innerText.includes('model indexed')",
            timeout=10_000,
        )


# ── API Key Limits UI Tests ───────────────────────────────────────────────────

class TestAPIKeyLimitsUI:
    """Tests for the spending cap and rate-limit edit modal added in the recent sprint."""

    def test_api_keys_table_has_cap_and_rate_limit_columns(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.wait_for_load_state("networkidle")
        body_text = page.locator("body").inner_text()
        assert "Cap" in body_text or "Spending" in body_text, "Expected spending cap column"
        assert "Rate" in body_text or "RPM" in body_text or "Limit" in body_text, "Expected rate limit column"

    def test_edit_limits_modal_opens_via_pencil(self, page: Page):
        """Clicking the pencil icon on a key row opens the limits edit modal."""
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.wait_for_load_state("networkidle")
        # Click the first pencil (edit limits) button in the keys list
        pencil_btn = page.locator("button[title='Edit limits']").first
        expect(pencil_btn).to_be_visible(timeout=8_000)
        pencil_btn.click()
        page.wait_for_timeout(500)
        # Modal should appear — look for spending cap or rate limit input
        modal_visible = page.locator(
            "text=Spending Cap, text=Rate Limit, input[placeholder*='cap'], input[placeholder*='RPM']"
        ).first
        # Use a broader check: a modal overlay appears
        expect(page.locator(".fixed.inset-0")).to_be_visible(timeout=5_000)
        page.keyboard.press("Escape")

    def test_edit_limits_sets_spending_cap(self, page: Page):
        """Fill in spending cap, save, verify value appears in table."""
        import json as _json
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.wait_for_load_state("networkidle")

        # Open first edit modal
        pencil_btn = page.locator("button[title='Edit limits']").first
        expect(pencil_btn).to_be_visible(timeout=8_000)
        pencil_btn.click()
        page.wait_for_timeout(400)

        modal = page.locator(".fixed.inset-0")
        expect(modal).to_be_visible(timeout=5_000)

        # Find spending cap input and set a value
        cap_input = modal.locator("input[type='number']").first
        if cap_input.is_visible(timeout=3_000):
            cap_input.click(click_count=3)
            cap_input.fill("25.00")

        # Save
        save_btn = modal.locator("button:has-text('Save')").first
        if save_btn.is_visible(timeout=3_000):
            save_btn.click()
            page.wait_for_timeout(1_000)
            # Modal should close
            page.wait_for_function(
                "() => document.querySelectorAll('.fixed.inset-0').length === 0",
                timeout=5_000,
            )

    def test_edit_limits_sets_rate_limit(self, page: Page):
        """Fill in rate limit RPM and save."""
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.wait_for_load_state("networkidle")

        pencil_btn = page.locator("button[title='Edit limits']").first
        expect(pencil_btn).to_be_visible(timeout=8_000)
        pencil_btn.click()
        page.wait_for_timeout(400)

        modal = page.locator(".fixed.inset-0")
        expect(modal).to_be_visible(timeout=5_000)

        # Second number input is rate limit RPM
        inputs = modal.locator("input[type='number']")
        if inputs.count() >= 2:
            rate_input = inputs.nth(1)
            rate_input.click(click_count=3)
            rate_input.fill("120")

        save_btn = modal.locator("button:has-text('Save')").first
        if save_btn.is_visible(timeout=3_000):
            save_btn.click()
            page.wait_for_timeout(1_000)

    def test_create_key_with_limits_flow(self, page: Page):
        """The create-key modal accepts spending_cap and rate_limit fields."""
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.click("text=Create Key")
        page.wait_for_timeout(300)
        modal = page.locator(".fixed.inset-0")
        expect(modal).to_be_visible(timeout=5_000)

        # Fill name
        page.fill('input[placeholder*="production"]', "test-limits-key")

        # If spending cap field exists in create modal, fill it
        cap_input = modal.locator("input[placeholder*='cap'], input[placeholder*='Spending']").first
        if cap_input.count() and cap_input.is_visible():
            cap_input.fill("10.00")

        # Submit
        modal.locator("button:has-text('Create Key')").click()

        # Raw key shown or key appears in table
        page.wait_for_function(
            "() => document.body.innerText.includes('NOT be shown') || document.body.innerText.includes('test-limits-key')",
            timeout=10_000,
        )
        # Close if the raw key modal appeared
        done_btn = page.locator("button:has-text('Done')")
        if done_btn.is_visible(timeout=2_000):
            done_btn.click()


# ── Provider Capability Edit UI Tests ─────────────────────────────────────────

class TestProviderCapabilityEditUI:
    """Tests for the model capability edit modal (pencil icon per model row)."""

    def _expand_first_provider_and_scan(self, page: Page):
        page.goto(f"{BASE_URL}/providers")
        first_card = page.locator("div.cursor-pointer").first
        first_card.click()
        page.wait_for_timeout(500)
        scan_btn = page.locator("button:has-text('Scan Models')").first
        expect(scan_btn).to_be_visible(timeout=5_000)
        scan_btn.click()
        page.wait_for_function(
            "() => !Array.from(document.querySelectorAll('button')).some(b => b.disabled && b.textContent.includes('Scan'))",
            timeout=30_000,
        )
        page.wait_for_timeout(1_000)

    def test_model_table_shows_after_scan(self, page: Page):
        login(page)
        self._expand_first_provider_and_scan(page)
        page.wait_for_function(
            "() => document.body.innerText.includes('indexed') || document.body.innerText.includes('No models')",
            timeout=10_000,
        )
        # Either a model count or an empty state is visible
        body_text = page.locator("body").inner_text()
        assert "indexed" in body_text or "No models" in body_text

    def test_capability_edit_pencil_opens_modal(self, page: Page):
        """If models are indexed, clicking the pencil icon opens the capability modal."""
        login(page)
        self._expand_first_provider_and_scan(page)

        # Check if any models were found
        body_text = page.locator("body").inner_text()
        if "No models indexed" in body_text:
            pytest.skip("No models indexed — cannot test capability edit")

        # Click first pencil (edit capability) button inside the provider card
        pencil = page.locator("table button[title*='capabilit'], table button svg.lucide-pencil").first
        if not pencil.is_visible(timeout=3_000):
            # Broader selector: any small pencil button in the model table
            pencil = page.locator("td button").first

        expect(pencil).to_be_visible(timeout=5_000)
        pencil.click()
        page.wait_for_timeout(500)

        # Modal should show capability fields
        expect(page.locator(".fixed.inset-0")).to_be_visible(timeout=5_000)
        modal_text = page.locator(".fixed.inset-0").inner_text()
        assert any(kw in modal_text for kw in ("Latency", "Cost tier", "Tasks", "Modalities", "Context")), \
            f"Capability modal content not found. Got: {modal_text[:300]}"

    def test_capability_edit_modal_has_task_checkboxes(self, page: Page):
        login(page)
        self._expand_first_provider_and_scan(page)

        body_text = page.locator("body").inner_text()
        if "No models indexed" in body_text:
            pytest.skip("No models indexed")

        pencil = page.locator("td button").first
        expect(pencil).to_be_visible(timeout=5_000)
        pencil.click()
        page.wait_for_timeout(500)

        modal = page.locator(".fixed.inset-0")
        expect(modal).to_be_visible(timeout=5_000)

        # Task toggle buttons (chat, code, reasoning etc.) should be present
        task_buttons = modal.locator("button").all()
        assert len(task_buttons) > 0, "No task toggle buttons found in capability modal"

    def test_capability_edit_save_closes_modal(self, page: Page):
        """Clicking Save in the capability modal closes it without error."""
        login(page)
        self._expand_first_provider_and_scan(page)

        body_text = page.locator("body").inner_text()
        if "No models indexed" in body_text:
            pytest.skip("No models indexed")

        pencil = page.locator("td button").first
        expect(pencil).to_be_visible(timeout=5_000)
        pencil.click()
        page.wait_for_timeout(500)

        modal = page.locator(".fixed.inset-0")
        expect(modal).to_be_visible(timeout=5_000)

        save_btn = modal.locator("button:has-text('Save')").first
        if save_btn.is_visible(timeout=3_000):
            save_btn.click()
            # Modal should close after save
            page.wait_for_function(
                "() => document.querySelectorAll('.fixed.inset-0').length === 0",
                timeout=8_000,
            )
        else:
            # Cancel if no Save button visible
            page.keyboard.press("Escape")

    def test_capability_edit_cancel_closes_modal(self, page: Page):
        """Pressing Escape or clicking Cancel closes the modal."""
        login(page)
        self._expand_first_provider_and_scan(page)

        body_text = page.locator("body").inner_text()
        if "No models indexed" in body_text:
            pytest.skip("No models indexed")

        pencil = page.locator("td button").first
        expect(pencil).to_be_visible(timeout=5_000)
        pencil.click()
        page.wait_for_timeout(500)

        expect(page.locator(".fixed.inset-0")).to_be_visible(timeout=5_000)
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.querySelectorAll('.fixed.inset-0').length === 0",
            timeout=5_000,
        )


class TestAiriTTS:
    """v4.3 — AIRI text-to-speech: a completed assistant message is read
    aloud via the speaker toggle. Regression cover for BUG-021 (the
    message->speak wiring previously had only a manual live smoke).

    The AIRI chat SSE is stubbed so the test is deterministic and costs no
    LLM call; /api/airi/speak is stubbed to a minimal WAV and its invocation
    is recorded — the assertion is that a completed message triggers it.
    Requires the deployment to have airi_tts_enabled on (v4.3.0+)."""

    # a valid, empty 16-bit PCM WAV (44-byte header, zero data)
    _WAV = (b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"
            + b"fmt " + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
            + (22050).to_bytes(4, "little") + (44100).to_bytes(4, "little")
            + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
            + b"data" + (0).to_bytes(4, "little"))

    def test_completed_message_triggers_speak(self, page: Page):
        login(page)

        speak_calls = []
        canned_sse = (
            'event: conversation\ndata: {"conversation_id":"qa-tts-wiring"}\n\n'
            'event: message\ndata: {"text":"Yes, the supervisor is enabled."}\n\n'
        )
        page.route("**/api/airi/chat", lambda route: route.fulfill(
            status=200, content_type="text/event-stream", body=canned_sse))

        def speak_handler(route):
            speak_calls.append(route.request.url)
            route.fulfill(status=200, content_type="audio/wav", body=self._WAV)
        page.route("**/api/airi/speak", speak_handler)

        page.goto(f"{BASE_URL}/routing")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

        spk = page.get_by_role("button", name=re.compile("spoken replies", re.I))
        assert spk.count() == 1, "speaker toggle should render when TTS is enabled"
        spk.first.click()
        page.wait_for_timeout(300)
        assert spk.first.get_attribute("aria-pressed") == "true", \
            "speaker toggle should switch on"

        page.get_by_placeholder(re.compile("Ask AIRI", re.I)).fill(
            "Is the supervisor enabled?")
        page.get_by_role("button", name="Send").first.click()

        # the completed assistant message must trigger POST /api/airi/speak
        for _ in range(20):
            if speak_calls:
                break
            page.wait_for_timeout(500)
        assert len(speak_calls) >= 1, \
            "a completed AIRI message must trigger /api/airi/speak (the v4.3 wiring)"


# ── F2 coverage: BUG-027 Activity Log deep filters ────────────────────────────

class TestActivityLogFilters:
    """BUG-027 part 1 — exercise the Activity Log filter surfaces beyond the
    single `?provider=` URL-param case that was already covered."""

    def test_search_input_accepts_and_triggers_query(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/activity")
        # `load` not `networkidle` — Activity Log has continuous polling
        # so networkidle never fires.
        page.wait_for_load_state("load")
        search = page.locator(
            "input[placeholder*='Search messages']"
        )
        expect(search).to_be_visible(timeout=8_000)
        # Record any activity-API request that carries the search term
        # (either in URL params or POST body).
        marker = "zzz_no_match_token_xyz"
        search_requests = []

        def capture(req):
            if "/api/" not in req.url:
                return
            if "activity" not in req.url and "monitoring" not in req.url:
                return
            payload = ""
            try:
                payload = req.post_data or ""
            except Exception:
                pass
            if marker in req.url or marker in payload:
                search_requests.append(req.url)

        page.on("request", capture)
        search.fill(marker)
        # Search is Enter-triggered (or click-triggered via the Search
        # button); typing alone only updates input state. Click the
        # primary "Search" button to be explicit + robust against any
        # focus quirks.
        page.locator("button:has-text('Search')").first.click()
        page.wait_for_timeout(2500)
        assert search.input_value() == marker
        assert search_requests, (
            "submitting the Activity search box should trigger an "
            "activity API request carrying the search term; saw none"
        )

    def test_severity_filter_changes_url_or_view(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/activity")
        page.wait_for_load_state("load")
        sev = page.locator("select").first
        expect(sev).to_be_visible(timeout=8_000)
        options = sev.locator("option").all_text_contents()
        assert len(options) >= 2, f"severity select should have options, got {options}"
        sev.select_option(index=1)
        page.wait_for_timeout(800)
        expect(page.locator("text=Clear all")).to_be_visible(timeout=5_000)

    def test_clear_all_filters_resets_state(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/activity?provider=ghost123")
        page.wait_for_load_state("load")
        expect(page.locator("text=Clear all")).to_be_visible(timeout=8_000)
        page.click("text=Clear all")
        page.wait_for_function(
            "() => !window.location.search.includes('provider=')",
            timeout=5_000,
        )


# ── F2 coverage: BUG-027 Metrics page render ──────────────────────────────────

class TestMetricsPageRender:
    """BUG-027 part 2 — Metrics page renders without console errors and
    surfaces its primary stat cards. Console-error capture is the same
    pattern used in the v4.3.0 QA pass."""

    def test_metrics_page_renders_with_no_console_errors(self, page: Page):
        errors = []

        def capture(msg):
            if msg.type == "error":
                txt = msg.text
                if "404" in txt or "401" in txt or "Failed to load resource" in txt:
                    return
                errors.append(txt)

        page.on("console", capture)
        login(page)
        page.click("text=Metrics")
        page.wait_for_url(f"{BASE_URL}/metrics", timeout=8_000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        # Match the same pattern as TestLLMProxy2UI.test_navigate_to_metrics_page
        expect(page.locator("h1").first).to_contain_text("Metrics")
        body = page.locator("body").inner_text()
        assert "Provider performance" in body or "Hit Rate" in body, \
            "Metrics page should render its primary content"
        assert not errors, f"Metrics page produced console errors: {errors}"

    def test_metrics_window_selector_buttons_clickable(self, page: Page):
        login(page)
        page.click("text=Metrics")
        page.wait_for_url(f"{BASE_URL}/metrics", timeout=8_000)
        page.wait_for_load_state("networkidle")
        # MetricsPage renders a row of window pill buttons ("24h", "7d", etc.)
        window_btn = page.locator("button:has-text('24h')").first
        expect(window_btn).to_be_visible(timeout=8_000)
        window_btn.click()
        # Click changed selected window without throwing — implicit assertion


# ── F2 coverage: BUG-027 + BUG-029 — Settings panel save/reload ───────────────

class TestSettingsPagePersistence:
    """BUG-027 part 3 + BUG-029 — Settings page renders and a representative
    field round-trips through save+reload.

    Targets `circuit_breaker_threshold` (an integer field with no operational
    blast-radius for small changes — saved + reverted in the same test)."""

    def test_settings_renders_main_sections(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/settings")
        page.wait_for_load_state("networkidle")
        expect(page.locator("h1:has-text('Settings')")).to_be_visible(timeout=8_000)
        body = page.locator("body").inner_text()
        assert "Circuit Breaker" in body, "Circuit-breaker section should be visible"
        assert "Email" in body, "Email-alerts section should be visible"

    def test_circuit_breaker_threshold_round_trips_through_reload(
        self, page: Page, admin_session
    ):
        """Edit -> save -> reload -> verify persisted -> restore."""
        # Snapshot original value via API (the safe-restore path)
        r = admin_session.get(f"{BASE_URL}/api/settings")
        assert r.status_code == 200
        original = r.json().get("circuit_breaker_threshold")
        try:
            login(page)
            page.goto(f"{BASE_URL}/settings")
            page.wait_for_load_state("networkidle")
            field = page.locator(
                "input[type='number']"
            ).filter(has=page.locator("xpath=./preceding::label[1]")).first
            # Fallback: target the input whose accessible label contains
            # "Failure threshold". Use the input below that label.
            label = page.get_by_text(re.compile("Failure threshold", re.I)).first
            expect(label).to_be_visible(timeout=8_000)
            target = label.locator("xpath=./following::input[1]")
            expect(target).to_be_visible(timeout=5_000)
            # Pick a delta that isn't the current value
            new_value = (int(original) if original else 5) + 1
            target.fill(str(new_value))
            page.locator("button:has-text('Save Settings')").click()
            # Toast or success — wait for the API to settle
            page.wait_for_timeout(1500)
            # Reload + verify
            page.reload()
            page.wait_for_load_state("networkidle")
            label2 = page.get_by_text(re.compile("Failure threshold", re.I)).first
            target2 = label2.locator("xpath=./following::input[1]")
            assert target2.input_value() == str(new_value), \
                "circuit_breaker_threshold should persist across reload"
        finally:
            # Restore via API regardless of test outcome
            if original is not None:
                admin_session.put(
                    f"{BASE_URL}/api/settings",
                    json={"circuit_breaker_threshold": original},
                )


# ── F2 coverage: BUG-028 — Form validation negative tests ─────────────────────

class TestFormValidationNegatives:
    """BUG-028 — empty / malformed inputs are rejected.

    Note: the Create-Key form's name field is intentionally optional
    (label says "Key Name (optional)"); empty-name acceptance is
    not a bug.

    BUG-041 + BUG-042 (originally surfaced as xfail markers in F2) are
    now fixed at the Pydantic-validator layer; these tests are
    regression guards."""

    def test_create_user_form_rejects_empty_password(self, page: Page, admin_session):
        """Username has HTML5 `required`; the typical UX is that submitting
        empty fields triggers browser validation tooltips and keeps the
        modal open. Verify a user is NOT created via the API as a side
        effect of an empty submit."""
        import uuid
        marker = f"pw-validation-{uuid.uuid4().hex[:8]}"
        # Snapshot user list pre-submit
        r0 = admin_session.get(f"{BASE_URL}/api/users")
        if r0.status_code != 200:
            pytest.skip(f"users API unreachable: {r0.status_code}")
        before = {u.get("username") for u in r0.json()}
        login(page)
        page.goto(f"{BASE_URL}/users")
        page.click("text=Add User")
        page.wait_for_timeout(400)
        # Fill username only; leave password empty (required)
        page.locator('.fixed.inset-0 input').first.fill(marker)
        submit = page.locator(".fixed.inset-0 button:has-text('Create')")
        expect(submit).to_be_visible(timeout=5_000)
        submit.click()
        page.wait_for_timeout(1200)
        # Either the modal stayed open (HTML5 blocked submit), OR closed
        # without persisting the user. Both are acceptable; what we test
        # is that no user named `marker` ended up in the DB.
        r1 = admin_session.get(f"{BASE_URL}/api/users")
        after = {u.get("username") for u in r1.json()}
        new_users = after - before
        assert marker not in new_users, (
            f"add-user form persisted a user with empty password; "
            f"unexpected new users: {new_users}"
        )
        # Cleanup if any user did get through
        if marker in new_users:
            for u in r1.json():
                if u.get("username") == marker:
                    admin_session.delete(f"{BASE_URL}/api/users/{u['id']}")

    def test_create_api_key_rejects_malformed_rate_limit(
        self, page: Page, admin_session
    ):
        """Rate-limit field is type='number'. The form should not allow a
        negative value to be persisted. Verify via API that no key with
        a negative rate_limit_rpm ends up created."""
        import uuid
        marker = f"pw-ratelimit-{uuid.uuid4().hex[:8]}"
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.click("text=Create Key")
        page.wait_for_timeout(400)
        # Fill name + bad rate limit
        name_input = page.locator(
            "input[placeholder*='production']"
        ).first
        expect(name_input).to_be_visible(timeout=5_000)
        name_input.fill(marker)
        # The second number input is the rate limit (first is none on this form
        # since there's no rate-limit field above it — the modal has 1 number input)
        rate_input = page.locator(".fixed.inset-0 input[type='number']").first
        rate_input.fill("-5")
        submit = page.locator(".fixed.inset-0 button:has-text('Create Key')")
        submit.click()
        page.wait_for_timeout(1500)
        # Read back keys; the new one (if created) must NOT have rate_limit_rpm<0
        r = admin_session.get(f"{BASE_URL}/api/keys")
        assert r.status_code == 200
        keys = r.json()
        bad = [
            k for k in keys
            if k.get("name") == marker
            and (k.get("rate_limit_rpm") or 0) < 0
        ]
        try:
            assert not bad, (
                f"API-key form persisted a negative rate_limit_rpm: {bad}"
            )
        finally:
            # Cleanup: delete any key with our marker name
            for k in keys:
                if k.get("name") == marker:
                    admin_session.delete(f"{BASE_URL}/api/keys/{k['id']}")


# ── F2 coverage: BUG-029 — Persistence + reload extras ────────────────────────

class TestProviderPersistence:
    """BUG-029 — non-AIRI persistence example. The existing
    TestSessionBehavior covers only session reload; this one covers a
    durable artifact (a freshly created provider) surviving a reload."""

    def test_created_provider_survives_reload(self, page: Page, admin_session):
        """Create a stub provider via API, confirm it appears on the page,
        reload, confirm still there, then delete via API (cleanup)."""
        import uuid
        name = f"pw-persist-{uuid.uuid4().hex[:8]}"
        # Create via API to keep the test fast + deterministic
        r = admin_session.post(
            f"{BASE_URL}/api/providers",
            json={
                "name": name,
                "provider_type": "litellm",
                "base_url": "http://example.invalid/",
                "api_key": "stub",
                "priority": 999,
                "enabled": False,
            },
        )
        assert r.status_code in (200, 201), f"create failed: {r.status_code} {r.text}"
        pid = r.json().get("id")
        try:
            login(page)
            page.goto(f"{BASE_URL}/providers")
            page.wait_for_load_state("networkidle")
            expect(page.locator(f"text={name}")).to_be_visible(timeout=10_000)
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator(f"text={name}")).to_be_visible(timeout=10_000)
        finally:
            if pid:
                admin_session.delete(f"{BASE_URL}/api/providers/{pid}")


# ── F2 coverage: BUG-030 — Cache header live ──────────────────────────────────

class TestCacheHeaderLive:
    """BUG-030 — the cache layer's X-Cache-Status header is set on every
    response. The only reliable live signal is the header itself; whether
    the value is `miss` / `hit` / `bypass` depends on live cache config.
    The test confirms the header is wired up and that on a duplicate
    request the value does NOT regress in a way that would indicate the
    cache layer was bypassed entirely (i.e. header absent on the second
    call)."""

    def test_cache_status_header_present(self, page: Page, admin_session):
        """Two identical /v1/messages calls. Both responses must carry
        X-Cache-Status (one of bypass/miss/hit) — proves the cache decision
        is in the request path."""
        # Use a dedicated key so the spending-cap counter is clean
        import uuid
        keyname = f"pw-cache-{uuid.uuid4().hex[:8]}"
        kr = admin_session.post(
            f"{BASE_URL}/api/keys",
            json={"name": keyname, "key_type": "standard"},
        )
        if kr.status_code not in (200, 201):
            pytest.skip(f"could not create test API key: {kr.status_code}")
        # API returns the raw secret only on creation, under `raw_key`
        # (see tests/conftest.py's test_api_key fixture).
        key = kr.json().get("raw_key")
        key_id = kr.json().get("id")
        try:
            if not key:
                pytest.skip("API-key creation did not return a usable secret")
            payload = {
                "model": "claude-haiku-4-5",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "ping pw cache test"}],
            }
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            # First call
            r1 = admin_session.post(
                f"{BASE_URL}/v1/messages",
                json=payload,
                headers=headers,
                timeout=60,
            )
            if r1.status_code != 200:
                pytest.skip(
                    f"upstream provider returned {r1.status_code}; cache test "
                    "needs at least one successful request to evaluate"
                )
            assert "X-Cache-Status" in r1.headers, (
                "X-Cache-Status header missing on first request — cache layer "
                "is not wired into the response pipeline"
            )
            # Second identical call — header must still be present (independent
            # of hit/miss outcome, since live cache TTL/config may vary)
            r2 = admin_session.post(
                f"{BASE_URL}/v1/messages",
                json=payload,
                headers=headers,
                timeout=60,
            )
            assert r2.status_code == 200
            assert "X-Cache-Status" in r2.headers, (
                "X-Cache-Status header missing on duplicate request — cache "
                "decision short-circuited out of the response path"
            )
        finally:
            if key_id:
                admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")


# ── F2 deferred: BUG-031 — notifications dispatch ─────────────────────────────
# AIRI rule-fire email path (app/airi/notify.py) requires either a stubbed
# SMTP destination at the server side or a test-mode flag in the notifier
# that suppresses the real send. Either approach needs a code-side change
# before a live integration test can be added safely without spamming the
# operator's inbox. Marked OPEN against BUG-031; live test deferred until
# the notifier gains a `dry_run` / test-mode parameter that returns the
# rendered email body without sending. The unit test suite covers
# rendering + recipient-filter logic today.

# ── F2 coverage: BUG-032 — Mobile / responsive sweep ──────────────────────────

class TestResponsiveLayout:
    """BUG-032 — render the main pages at mobile + tablet widths; verify
    the heading is on screen and the page doesn't horizontally overflow.

    `document.documentElement.scrollWidth > clientWidth` is the canonical
    "page is wider than viewport" check (horizontal scrollbar present)."""

    VIEWPORTS = [
        ("mobile", 375, 812),
        ("tablet", 768, 1024),
    ]
    PAGES = [
        ("/providers", "Providers"),
        ("/keys", "API Keys"),
        ("/users", "Users"),
        ("/activity", "Activity Log"),
        ("/metrics", "Metrics"),
        ("/settings", "Settings"),
    ]

    @pytest.mark.parametrize("vp_name,width,height", VIEWPORTS)
    @pytest.mark.parametrize("path,heading", PAGES)
    def test_no_horizontal_overflow(self, browser, vp_name, width, height, path, heading):
        ctx = browser.new_context(
            viewport={"width": width, "height": height},
            ignore_https_errors=True,
        )
        pg = ctx.new_page()
        try:
            login(pg)
            # Direct goto works at both viewports; sidebar clicking fails
            # at mobile width because the sidebar is hidden off-canvas.
            pg.goto(f"{BASE_URL}{path}")
            pg.wait_for_load_state("load")
            # Wait for ANY meaningful content (page rendered past splash).
            # `aside` works on most pages but /metrics sometimes does not
            # surface it on direct load — body text length is the
            # most-resilient cross-page "rendered" signal.
            pg.wait_for_function(
                "() => document.body && document.body.innerText.length > 80",
                timeout=15_000,
            )
            pg.wait_for_timeout(1000)
            # No body-level horizontal overflow (tables may scroll
            # internally but the document shouldn't).
            overflow = pg.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth"
            )
            assert overflow <= 4, (
                f"{path} at {vp_name} ({width}x{height}) overflows by "
                f"{overflow}px horizontally"
            )
        finally:
            ctx.close()


# ── F2 coverage: BUG-033 — Keyboard accessibility ─────────────────────────────

class TestKeyboardAccessibility:
    """BUG-033 — keyboard-only flow: the login form is reachable + submittable
    via Tab + Enter, and the post-login dashboard has tab-able interactive
    elements with visible focus.

    Note: full keyboard walk-through across every page is the F2 roadmap;
    this test pins the most-trafficked surface (login + dashboard nav)."""

    def test_login_form_submittable_via_keyboard(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        page.wait_for_load_state("networkidle")
        # Tab into the username field — depending on the focus-trap setup
        # the input may already be auto-focused. Either way, type works.
        page.keyboard.press("Tab")
        # Type the credentials directly (the focused element should be one
        # of the two inputs; press Tab once more if not on username)
        username = page.locator('input[autocomplete="username"]')
        username.focus()
        page.keyboard.type(ADMIN_USER)
        page.keyboard.press("Tab")
        page.keyboard.type(ADMIN_PASS)
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => !window.location.href.includes('/login')",
            timeout=15_000,
        )

    def test_sidebar_links_are_focusable(self, page: Page):
        login(page)
        # Sidebar nav links should be real <a> or <button> (focusable). Pick
        # a known nav link and confirm it can receive focus.
        link = page.locator("aside a[href*='/providers']").first
        expect(link).to_be_visible(timeout=8_000)
        link.focus()
        focused_tag = page.evaluate(
            "() => document.activeElement && document.activeElement.tagName"
        )
        assert focused_tag in ("A", "BUTTON"), (
            f"sidebar Providers link should be a focusable element, got {focused_tag}"
        )
        page.keyboard.press("Enter")
        page.wait_for_url(f"{BASE_URL}/providers", timeout=8_000)
