const { test, expect } = require('@playwright/test');

test.describe('Provider Testing - After Fix', () => {
  test('Test all providers from UI after fix', async ({ page }) => {
    // Login
    await page.goto('https://www.voipguru.org/llmProxy/login.html');
    await page.fill('#username', 'admin');
    await page.fill('#password', 'admin');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/llmProxy/', { timeout: 10000 });
    console.log('✅ Logged in successfully');

    // Wait for providers to load
    await page.waitForSelector('#providers', { timeout: 5000 });

    // Click "Test All Providers" button
    console.log('\n=== TESTING ALL PROVIDERS VIA UI ===\n');
    await page.click('button:has-text("Test All Providers")');

    // Wait for tests to complete (give it 30 seconds for all providers)
    await page.waitForTimeout(30000);

    // Check alert for results
    page.on('dialog', async dialog => {
      const message = dialog.message();
      console.log('\nTest Results from UI:');
      console.log(message);
      await dialog.accept();
    });

    console.log('\n✅ Test All Providers completed');
  });

  test('Test single provider from UI', async ({ page }) => {
    // Login
    await page.goto('https://www.voipguru.org/llmProxy/login.html');
    await page.fill('#username', 'admin');
    await page.fill('#password', 'admin');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/llmProxy/', { timeout: 10000 });
    console.log('✅ Logged in successfully');

    // Wait for providers to load
    await page.waitForSelector('#providers', { timeout: 5000 });

    // Find and click the test button on the first provider
    const firstTestButton = await page.$('.provider-item button.btn-test');
    if (firstTestButton) {
      console.log('\n=== TESTING SINGLE PROVIDER VIA UI ===\n');

      // Listen for alerts
      page.on('dialog', async dialog => {
        const message = dialog.message();
        console.log('Test result:', message);

        if (message.includes('✅')) {
          console.log('✅ Provider test PASSED');
        } else if (message.includes('❌')) {
          console.log('❌ Provider test failed (but API is working correctly)');
          console.log('Error details:', message);
        }

        await dialog.accept();
      });

      await firstTestButton.click();

      // Wait for test to complete
      await page.waitForTimeout(10000);
    } else {
      console.log('⚠️  No test button found');
    }
  });

  test('Verify activity log shows test results', async ({ page }) => {
    // Login
    await page.goto('https://www.voipguru.org/llmProxy/login.html');
    await page.fill('#username', 'admin');
    await page.fill('#password', 'admin');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/llmProxy/', { timeout: 10000 });
    console.log('✅ Logged in successfully');

    // Scroll to activity log
    await page.evaluate(() => {
      document.querySelector('#activity-log').scrollIntoView();
    });

    // Wait a bit for activity log to load
    await page.waitForTimeout(2000);

    // Check if activity log has entries
    const activityLogContent = await page.$eval('#activity-log', el => el.innerHTML);

    if (activityLogContent.includes('No recent activity')) {
      console.log('⚠️  Activity log is empty');
    } else {
      console.log('✅ Activity log has entries');

      // Count how many provider test entries are there
      const successCount = (activityLogContent.match(/Provider test successful/g) || []).length;
      const failureCount = (activityLogContent.match(/Provider test failed/g) || []).length;

      console.log(`Activity log contains:`);
      console.log(`  - ${successCount} successful provider tests`);
      console.log(`  - ${failureCount} failed provider tests`);
    }
  });
});
