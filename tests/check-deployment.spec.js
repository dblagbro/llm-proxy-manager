const { test, expect } = require('@playwright/test');

test.describe('LLM Proxy Deployment Check', () => {
  test('Check www.voipguru.org/llmProxy deployment', async ({ page }) => {
    // Navigate to the site
    await page.goto('https://www.voipguru.org/llmProxy/', {
      waitUntil: 'networkidle',
      timeout: 30000
    });

    // Login
    console.log('Attempting login...');
    await page.fill('input[type="text"]', 'dblagbro');
    await page.fill('input[type="password"]', 'Super*120120');
    await page.click('button:has-text("Login")');

    // Wait for login to complete
    await page.waitForSelector('.header', { timeout: 10000 });
    console.log('✅ Login successful');

    // Check version number
    const versionText = await page.locator('h1 span').textContent();
    console.log('Version:', versionText);
    expect(versionText).toContain('v1.4');

    // Check subtitle
    const subtitle = await page.locator('.subtitle').textContent();
    console.log('Subtitle:', subtitle);
    expect(subtitle).toContain('cluster synchronization');
    expect(subtitle).not.toContain('SSE Streaming');

    // Scroll down to check all sections
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(1000);

    // Check API Keys section styling - CONTENT BOXES should be dark
    console.log('\n=== Checking API Keys section ===');
    const apiKeysCard = await page.locator('h2:has-text("API Keys")').locator('..').first();
    const apiKeysBg = await apiKeysCard.evaluate(el => window.getComputedStyle(el).backgroundColor);
    console.log('API Keys card background:', apiKeysBg);

    // Check API key content box background (the individual key items)
    const apiKeyItem = await page.locator('#api-keys-container > div').first();
    if (await apiKeyItem.count() > 0) {
      const apiKeyItemBg = await apiKeyItem.evaluate(el => window.getComputedStyle(el).backgroundColor);
      console.log('API Key item background:', apiKeyItemBg);
      // Should be dark: rgb(22, 27, 34) or similar, NOT white rgb(249, 249, 249)
      expect(apiKeyItemBg).not.toContain('249');
    }

    // Check Activity Log section styling - CONTENT ITEMS should be dark
    console.log('\n=== Checking Activity Log section ===');
    const activityLogCard = await page.locator('h2:has-text("Activity Log")').locator('..').first();
    const activityLogBg = await activityLogCard.evaluate(el => window.getComputedStyle(el).backgroundColor);
    console.log('Activity Log card background:', activityLogBg);

    // Check Activity Log item background
    const activityLogItem = await page.locator('#activity-log > div').first();
    if (await activityLogItem.count() > 0) {
      const activityLogItemBg = await activityLogItem.evaluate(el => window.getComputedStyle(el).backgroundColor);
      console.log('Activity Log item background:', activityLogItemBg);
      // Should be dark, NOT white rgb(248, 249, 250)
      expect(activityLogItemBg).not.toContain('248');
      expect(activityLogItemBg).not.toContain('249');
    }

    // Check Cluster Status section
    console.log('\n=== Checking Cluster Status section ===');
    const clusterSection = await page.locator('#cluster-status');
    const clusterText = await clusterSection.textContent();
    console.log('Cluster Status text:', clusterText.substring(0, 200));

    // Check if Reset Statistics button exists
    const resetButton = await page.locator('button:has-text("Reset Statistics")');
    expect(await resetButton.count()).toBe(1);
    console.log('✅ Reset Statistics button found');

    // Take a screenshot
    await page.screenshot({ path: '/tmp/llm-proxy-www1.png', fullPage: true });
    console.log('Screenshot saved to /tmp/llm-proxy-www1.png');
  });

  test('Check www2.voipguru.org/llmProxy deployment', async ({ page }) => {
    // Navigate to www2
    await page.goto('https://www2.voipguru.org/llmProxy/', {
      waitUntil: 'networkidle',
      timeout: 30000
    });

    // Login
    console.log('Attempting login to www2...');
    await page.fill('input[type="text"]', 'dblagbro');
    await page.fill('input[type="password"]', 'Super*120120');
    await page.click('button:has-text("Login")');

    // Wait for login to complete
    await page.waitForSelector('.header', { timeout: 10000 });
    console.log('✅ Login successful on www2');

    // Check version number
    const versionText = await page.locator('h1 span').textContent();
    console.log('www2 Version:', versionText);

    // Scroll down to check cluster status
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(2000);

    // Check Cluster Status section
    console.log('\n=== Checking www2 Cluster Status ===');
    const clusterSection = await page.locator('#cluster-status');
    const clusterText = await clusterSection.textContent();
    console.log('www2 Cluster Status:', clusterText.substring(0, 300));

    // Take a screenshot
    await page.screenshot({ path: '/tmp/llm-proxy-www2.png', fullPage: true });
    console.log('Screenshot saved to /tmp/llm-proxy-www2.png');
  });
});
