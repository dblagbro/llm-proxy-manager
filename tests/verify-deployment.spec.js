const { test, expect } = require('@playwright/test');

test.describe('LLM Proxy Deployment Verification', () => {
  test('should load the login page without 502 errors', async ({ page }) => {
    // Test the production endpoint
    const response = await page.goto('https://www.voipguru.org/llmProxy/', {
      waitUntil: 'networkidle',
      timeout: 30000
    });

    // Should not be a 502 error
    expect(response.status()).not.toBe(502);

    // Should be a successful response or redirect
    expect(response.status()).toBeLessThan(500);

    // Page should contain login elements
    const pageTitle = await page.title();
    expect(pageTitle).toMatch(/LLM Proxy/i);

    // Should have login form
    const usernameInput = page.locator('input#username').first();
    await expect(usernameInput).toBeVisible();
  });

  test('should handle multiple requests without 502 errors', async ({ page }) => {
    const attempts = 20;
    let successCount = 0;
    let errorCount = 0;
    const errors = [];

    for (let i = 0; i < attempts; i++) {
      try {
        const response = await page.goto('https://www.voipguru.org/llmProxy/', {
          waitUntil: 'domcontentloaded',
          timeout: 10000
        });

        if (response.status() === 502) {
          errorCount++;
          errors.push(`Attempt ${i + 1}: 502 Bad Gateway`);
        } else if (response.status() < 500) {
          successCount++;
        }
      } catch (error) {
        errorCount++;
        errors.push(`Attempt ${i + 1}: ${error.message}`);
      }

      // Small delay between requests
      await page.waitForTimeout(200);
    }

    console.log(`Success: ${successCount}/${attempts}, Errors: ${errorCount}/${attempts}`);
    if (errors.length > 0) {
      console.log('Errors encountered:', errors);
    }

    // Allow up to 1 error out of 20 attempts (95% success rate)
    expect(errorCount).toBeLessThanOrEqual(1);
    expect(successCount).toBeGreaterThanOrEqual(19);
  });

  test('should successfully login and access dashboard', async ({ page }) => {
    await page.goto('https://www.voipguru.org/llmProxy/');

    // Fill in login credentials
    await page.fill('input[name="username"], input#username', 'dblagbro');
    await page.fill('input[name="password"], input#password', 'Super*120120');

    // Click login button
    await page.click('button[type="submit"], button:has-text("Login")');

    // SPA - wait for dashboard to load (URL doesn't change)
    await page.waitForSelector('.header', { timeout: 10000 });

    // Should see dashboard elements
    const pageContent = await page.content();
    expect(pageContent).toContain('Provider');
  });
});
