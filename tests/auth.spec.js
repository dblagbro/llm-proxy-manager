const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const USERNAME = 'dblagbro';
const PASSWORD = 'Super*120120';

test.describe('LLM Proxy Authentication', () => {
  test('should redirect to login when not authenticated', async ({ page }) => {
    await page.goto(BASE_URL + '/');

    // Should redirect to login page
    await expect(page).toHaveURL(/\/login\.html/);
    await expect(page.locator('h1')).toContainText('LLM Proxy');
  });

  test('should show error for invalid credentials', async ({ page }) => {
    await page.goto(BASE_URL + '/login.html');

    await page.fill('[name=username]', 'wronguser');
    await page.fill('[name=password]', 'wrongpass');
    await page.click('button[type=submit]');

    // Should show error message
    await expect(page.locator('#error')).toBeVisible();
    await expect(page.locator('#error')).toContainText('Invalid credentials');
  });

  test('should login successfully with valid credentials', async ({ page }) => {
    await page.goto(BASE_URL + '/login.html');

    await page.fill('[name=username]', USERNAME);
    await page.fill('[name=password]', PASSWORD);
    await page.click('button[type=submit]');

    // Should redirect to dashboard
    await page.waitForURL(/\/(?!login)/);
    await expect(page.locator('h1')).toContainText('LLM Proxy Manager');

    // Should show user display
    await expect(page.locator('#userDisplay')).toContainText(USERNAME);
  });

  test('should maintain session after login', async ({ page, context }) => {
    // Login first
    await page.goto(BASE_URL + '/login.html');
    await page.fill('[name=username]', USERNAME);
    await page.fill('[name=password]', PASSWORD);
    await page.click('button[type=submit]');
    await page.waitForURL(/\/(?!login)/);

    // Open new page with same context (shares cookies)
    const newPage = await context.newPage();
    await newPage.goto(BASE_URL + '/');

    // Should NOT redirect to login
    await expect(newPage.locator('h1')).toContainText('LLM Proxy Manager');
    await expect(newPage.locator('#userDisplay')).toContainText(USERNAME);

    await newPage.close();
  });

  test('should logout successfully', async ({ page }) => {
    // Login first
    await page.goto(BASE_URL + '/login.html');
    await page.fill('[name=username]', USERNAME);
    await page.fill('[name=password]', PASSWORD);
    await page.click('button[type=submit]');
    await page.waitForURL(/\/(?!login)/);

    // Open user dropdown then click logout
    await page.click('.user-dropdown-toggle');
    await page.click('.user-dropdown-menu .dropdown-item:has-text("Logout")');

    // Should redirect to login
    await expect(page).toHaveURL(/\/login\.html/);

    // Try to access main page again
    await page.goto(BASE_URL + '/');

    // Should redirect back to login
    await expect(page).toHaveURL(/\/login\.html/);
  });
});

test.describe('Provider Management', () => {
  test.beforeEach(async ({ page }) => {
    // Login before each test
    await page.goto(BASE_URL + '/login.html');
    await page.fill('[name=username]', USERNAME);
    await page.fill('[name=password]', PASSWORD);
    await page.click('button[type=submit]');
    await page.waitForURL(/\/(?!login)/);
    await page.waitForLoadState('networkidle');
  });

  test('should display existing providers', async ({ page }) => {
    // Should see providers listed
    const providers = page.locator('.provider');
    await expect(providers).not.toHaveCount(0);

    // Should see provider details
    await expect(page.locator('.provider-name').first()).toBeVisible();
    await expect(page.locator('.provider-type').first()).toBeVisible();
  });

  test('should open add provider modal', async ({ page }) => {
    await page.click('text=Add Provider');

    // Modal should be visible
    await expect(page.locator('#editModal')).toHaveClass(/show/);
    await expect(page.locator('#modalTitle')).toContainText('Add New Provider');

    // Form fields should be visible
    await expect(page.locator('#edit-name')).toBeVisible();
    await expect(page.locator('#edit-type')).toBeVisible();
    await expect(page.locator('#edit-apiKey')).toBeVisible();
    await expect(page.locator('#edit-priority')).toBeVisible();
  });

  test('should add a new provider', async ({ page }) => {
    await page.click('text=Add Provider');
    await page.waitForSelector('#editModal.show');

    // Fill form
    await page.fill('#edit-name', 'Test Provider');
    await page.selectOption('#edit-type', 'anthropic');
    await page.fill('#edit-apiKey', 'sk-ant-test-key-123456789');
    await page.fill('#edit-priority', '99');
    await page.check('#edit-enabled');

    // Submit form (button text is "Save Changes")
    await page.click('#editModal button[type=submit]');

    // Modal should close
    await expect(page.locator('#editModal')).not.toHaveClass(/show/);

    // New provider should appear in list
    await page.waitForTimeout(500);
    await expect(page.locator('.provider-name:has-text("Test Provider")').first()).toBeVisible();
  });

  test('should toggle provider enable/disable', async ({ page }) => {
    const firstToggle = page.locator('.provider').first().locator('.toggle input');
    const initialState = await firstToggle.isChecked();

    // Toggle the switch (checkbox is inside CSS toggle widget — use dispatchEvent to bypass viewport check)
    await firstToggle.dispatchEvent('click');

    // State should change
    if (initialState) {
      await expect(firstToggle).not.toBeChecked();
    } else {
      await expect(firstToggle).toBeChecked();
    }
  });

  test('should test provider connection', async ({ page }) => {
    await page.click('text=Add Provider');
    await page.waitForSelector('#editModal.show');

    // Fill with test data
    await page.fill('#edit-name', 'Test Provider');
    await page.selectOption('#edit-type', 'anthropic');
    await page.fill('#edit-apiKey', 'sk-ant-api03-invalid-key');

    // Click test button
    await page.click('text=Test This Provider');

    // Should show testing message, then result
    await expect(page.locator('#testResult')).toBeVisible();
    await page.waitForTimeout(3000); // Wait for test to complete

    // Should show some result (success or error)
    const result = page.locator('#testResult');
    await expect(result).toBeVisible();
  });

  test('should close modal on cancel', async ({ page }) => {
    await page.click('text=Add Provider');
    await page.waitForSelector('#editModal.show');

    // Click cancel
    await page.click('text=Cancel');

    // Modal should close
    await expect(page.locator('#editModal')).not.toHaveClass(/show/);
  });
});

test.describe('Settings and Configuration', () => {
  test.beforeEach(async ({ page }) => {
    // Login before each test
    await page.goto(BASE_URL + '/login.html');
    await page.fill('[name=username]', USERNAME);
    await page.fill('[name=password]', PASSWORD);
    await page.click('button[type=submit]');
    await page.waitForURL(/\/(?!login)/);
    await page.waitForLoadState('networkidle');
  });

  test('should open settings modal', async ({ page }) => {
    await page.click('button:has-text("Settings")');

    // Settings modal should be visible
    await expect(page.locator('#settingsModal')).toHaveClass(/show/);
    await expect(page.locator('#settingsModal h3').first()).toContainText('Proxy Settings');
  });

  test('should display API endpoint and configuration', async ({ page }) => {
    await page.click('button:has-text("Settings")');
    await expect(page.locator('#settingsModal')).toHaveClass(/show/);

    // Should show Claude Code config textarea
    await expect(page.locator('#settingsModal textarea').first()).toContainText('ANTHROPIC_BASE_URL');
  });

  test('should save configuration', async ({ page }) => {
    // Click save button
    page.on('dialog', dialog => dialog.accept());
    await page.click('text=Save Configuration');

    // Should show success message (in alert)
    await page.waitForTimeout(1000);
  });
});
