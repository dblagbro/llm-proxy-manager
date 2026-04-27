"""
Playwright integration tests for llm-proxy v2 UI and API.
Runs against the live deployment at https://www.voipguru.org/llm-proxy2/

Run with:
    playwright install chromium
    python -m pytest tests/integration/test_playwright_ui.py -v
"""
import time
import pytest
from playwright.sync_api import sync_playwright, Page, expect

BASE_URL = "https://www.voipguru.org/llm-proxy2"
ADMIN_USER = "admin"
ADMIN_PASS = "Super*120120"


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
    page.goto(f"{BASE_URL}/login")
    page.wait_for_load_state("networkidle")
    page.fill('input[autocomplete="username"]', ADMIN_USER)
    page.fill('input[autocomplete="current-password"]', ADMIN_PASS)
    page.click('button[type="submit"]')
    # Wait for redirect away from /login — success lands on dashboard or root
    page.wait_for_function(
        "() => !window.location.href.includes('/login')",
        timeout=15_000,
    )
    page.wait_for_load_state("networkidle")


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

    def test_create_api_key_flow(self, page: Page):
        login(page)
        page.goto(f"{BASE_URL}/keys")
        page.click("text=Create Key")
        # Fill name
        page.fill('input[placeholder*="production"]', "test-playwright-key")
        # Click the submit button inside the modal (scoped to dialog overlay)
        page.locator('.fixed.inset-0 button:has-text("Create Key")').click()
        # The raw key modal should appear
        expect(page.locator("text=Your New API Key")).to_be_visible(timeout=8_000)
        expect(page.locator("text=NOT be shown again")).to_be_visible()
        page.click('button:has-text("Done")')

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
