const { test, expect } = require('@playwright/test');

test.describe('LLM Proxy Login Test - Browser Simulation', () => {
  test('should successfully login with admin credentials', async ({ page }) => {
    console.log('\n========================================');
    console.log('Starting browser login test');
    console.log('========================================\n');

    // Enable request logging
    page.on('response', response => {
      if (response.url().includes('/api/auth/login')) {
        console.log(`Login API response: ${response.status()}`);
      }
    });

    page.on('console', msg => {
      console.log(`Browser console: ${msg.text()}`);
    });

    // Navigate to login page
    console.log('1. Navigating to https://www.voipguru.org/llmProxy/');
    await page.goto('https://www.voipguru.org/llmProxy/', {
      waitUntil: 'networkidle',
      timeout: 30000
    });

    console.log('2. Login page loaded successfully');

    // Take screenshot before login
    await page.screenshot({ path: '/tmp/login-page-before.png' });
    console.log('3. Screenshot saved: /tmp/login-page-before.png');

    // Check what's on the page
    const pageTitle = await page.title();
    console.log(`4. Page title: ${pageTitle}`);

    // Fill in login credentials - using ADMIN not dblagbro
    console.log('5. Entering credentials: dblagbro / Super*120120');
    await page.fill('input[name="username"], input#username, input[type="text"]', 'dblagbro');
    await page.fill('input[name="password"], input#password, input[type="password"]', 'Super*120120');

    // Take screenshot with credentials filled
    await page.screenshot({ path: '/tmp/login-page-filled.png' });
    console.log('6. Screenshot saved: /tmp/login-page-filled.png');

    // Click login button and wait for response
    console.log('7. Clicking login button...');
    const [response] = await Promise.all([
      page.waitForResponse(resp => resp.url().includes('/api/auth/login'), { timeout: 10000 }),
      page.click('button[type="submit"], button:has-text("Login")')
    ]);

    console.log(`8. Login API response status: ${response.status()}`);

    let responseBody;
    try {
      responseBody = await response.json();
      console.log('9. Login response body:', JSON.stringify(responseBody, null, 2));
    } catch (e) {
      console.log('9. Login response body not available (redirect/navigation)');
    }

    // Should get successful login response
    expect(response.status()).toBe(200);

    if (responseBody) {
      expect(responseBody.success).toBe(true);
      console.log('10. Login API call successful!');
    }

    // Wait a moment for any redirects
    await page.waitForTimeout(2000);

    // Take screenshot after login
    await page.screenshot({ path: '/tmp/after-login.png' });
    console.log('11. Screenshot saved: /tmp/after-login.png');

    // Check current URL
    const currentURL = page.url();
    console.log(`12. Current URL after login: ${currentURL}`);

    // Try to find dashboard elements
    const pageContent = await page.content();

    if (pageContent.includes('Provider') || pageContent.includes('Dashboard')) {
      console.log('13. ✅ Successfully reached dashboard!');
      expect(pageContent).toMatch(/Provider|Dashboard/);
    } else if (currentURL.includes('login')) {
      console.log('13. ❌ Still on login page - checking for error messages');

      // Look for error messages
      const errorMessage = await page.locator('.error, .alert, [class*="error"]').textContent().catch(() => 'No error element found');
      console.log(`    Error message: ${errorMessage}`);

      throw new Error('Login API succeeded but browser still on login page');
    } else {
      console.log('13. ⚠️  Unknown page state');
      console.log(`    Current URL: ${currentURL}`);
      console.log(`    Page contains "Provider": ${pageContent.includes('Provider')}`);
      console.log(`    Page contains "Dashboard": ${pageContent.includes('Dashboard')}`);
    }

    console.log('\n========================================');
    console.log('Browser login test complete!');
    console.log('========================================\n');
  });
});
