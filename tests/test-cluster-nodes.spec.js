const { test, expect } = require('@playwright/test');

test.describe('Cluster Node Tests', () => {
  test('Test tmrwww01 (www1) - should load and show providers', async ({ page }) => {
    console.log('Testing tmrwww01...');

    // Go to www1
    await page.goto('https://www.voipguru.org/llmProxy/', {
      waitUntil: 'networkidle',
      timeout: 30000
    });

    // Wait for page to be stable
    await page.waitForTimeout(2000);

    // Check if login page or main page
    const hasLoginForm = await page.locator('input[type="password"]').count() > 0;

    if (hasLoginForm) {
      console.log('www1: Login required, logging in...');
      await page.fill('input[type="text"]', 'dblagbro');
      await page.fill('input[type="password"]', 'Super*120120');
      await page.click('button:has-text("Login")');
      await page.waitForSelector('.header', { timeout: 10000 });
      await page.waitForTimeout(3000); // wait for async provider load
    }

    // Check for providers
    const providerCards = await page.locator('.provider-card, .card').count();
    console.log(`www1: Found ${providerCards} provider cards`);

    // Check for title
    const title = await page.textContent('h1');
    console.log(`www1: Title = "${title}"`);

    // Get provider names
    const providers = await page.locator('#providers .card h3, #providers .card .card-title').allTextContents();
    console.log(`www1: Providers = ${JSON.stringify(providers)}`);

    expect(providerCards).toBeGreaterThan(0);
  });

  test('Test tmrwww02 (www2) - should load and sync from www1', async ({ page }) => {
    console.log('Testing tmrwww02...');

    // Set longer timeout
    page.setDefaultTimeout(30000);

    // Go to www2
    try {
      await page.goto('https://www2.voipguru.org/llmProxy/', {
        waitUntil: 'networkidle',
        timeout: 30000
      });
    } catch (err) {
      console.error(`www2: Failed to load page: ${err.message}`);
      throw err;
    }

    // Wait for page to be stable
    await page.waitForTimeout(3000);

    // Check if page is reloading repeatedly
    let reloadCount = 0;
    page.on('load', () => {
      reloadCount++;
      console.log(`www2: Page reload detected (count: ${reloadCount})`);
    });

    await page.waitForTimeout(5000);

    if (reloadCount > 2) {
      console.error(`www2: Page is reloading repeatedly! (${reloadCount} reloads)`);
    }

    // Check if login page or main page
    const hasLoginForm = await page.locator('input[type="password"]').count() > 0;

    if (hasLoginForm) {
      console.log('www2: Login required, logging in...');
      await page.fill('input[type="text"]', 'dblagbro');
      await page.fill('input[type="password"]', 'Super*120120');
      await page.click('button:has-text("Login")');
      await page.waitForSelector('.header', { timeout: 10000 });
      await page.waitForTimeout(3000); // wait for async provider load
    }

    // Check for providers
    const providerCards = await page.locator('.provider-card, .card').count();
    console.log(`www2: Found ${providerCards} provider cards`);

    // Check for title
    const title = await page.textContent('h1');
    console.log(`www2: Title = "${title}"`);

    // Get provider names
    const providers = await page.locator('#providers .card h3, #providers .card .card-title').allTextContents();
    console.log(`www2: Providers = ${JSON.stringify(providers)}`);

    // Check console errors
    const errors = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
        console.error(`www2 Console Error: ${msg.text()}`);
      }
    });

    if (providerCards === 0) {
      console.error('www2: NO PROVIDERS FOUND - Sync failed!');
    }

    expect(providerCards).toBeGreaterThan(0);
  });
});
