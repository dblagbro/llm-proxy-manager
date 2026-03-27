const { test, expect } = require('@playwright/test');

test.describe('Provider Testing - All Methods', () => {
  let page;

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();

    // Login first
    await page.goto('https://www.voipguru.org/llmProxy/login.html');
    await page.fill('#username', 'dblagbro');
    await page.fill('#password', 'Super*120120');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/llmProxy/', { timeout: 10000 });
    console.log('✅ Logged in successfully');
  });

  test.afterAll(async () => {
    await page.close();
  });

  test('Test all existing providers from the dashboard', async () => {
    console.log('\n=== TESTING ALL PROVIDERS FROM DASHBOARD ===\n');

    // Wait for providers to load
    await page.waitForSelector('#providers', { timeout: 5000 });

    // Get all provider cards
    const providerCards = await page.$$('.provider-item');
    console.log(`Found ${providerCards.length} providers`);

    for (let i = 0; i < providerCards.length; i++) {
      const card = providerCards[i];

      // Get provider name
      const nameElement = await card.$('.provider-name');
      const providerName = await nameElement?.innerText();

      // Get provider type
      const typeElement = await card.$('.provider-type');
      const providerType = await typeElement?.innerText();

      console.log(`\n--- Testing Provider ${i + 1}: ${providerName} (${providerType}) ---`);

      // Find and click the test button for this provider
      const testButton = await card.$('button.btn-test');
      if (!testButton) {
        console.log(`❌ No test button found for ${providerName}`);
        continue;
      }

      await testButton.click();
      console.log(`Clicked test button for ${providerName}`);

      // Wait for test result (up to 30 seconds for API call)
      await page.waitForTimeout(2000); // Give it time to start

      // Look for result in the card
      const resultElement = await card.$('.test-result');
      if (resultElement) {
        const resultText = await resultElement.innerText();
        console.log(`Result: ${resultText}`);

        if (resultText.includes('✅') || resultText.includes('Success')) {
          console.log(`✅ ${providerName} test PASSED`);
        } else if (resultText.includes('❌') || resultText.includes('Failed')) {
          console.log(`❌ ${providerName} test FAILED`);
          console.log(`   Error: ${resultText}`);
        }
      } else {
        console.log(`⚠️  No result element found for ${providerName}`);
      }

      // Wait a bit between tests
      await page.waitForTimeout(1000);
    }
  });

  test('Test provider via /api/test-provider endpoint directly', async () => {
    console.log('\n=== TESTING VIA API ENDPOINT DIRECTLY ===\n');

    // Get current config to extract providers
    const configResponse = await page.evaluate(async () => {
      const response = await fetch('./api/config');
      return await response.json();
    });

    console.log(`Found ${configResponse.providers.length} providers in config`);

    for (const provider of configResponse.providers) {
      console.log(`\n--- Testing ${provider.name} (${provider.type}) via API ---`);

      // Check if API key is masked
      const isMasked = provider.apiKey && provider.apiKey.includes('...');
      console.log(`API Key masked: ${isMasked}`);
      console.log(`API Key preview: ${provider.apiKey?.slice(0, 20)}...`);

      if (isMasked) {
        console.log(`⚠️  PROBLEM: API key is masked in config, cannot test directly`);
        continue;
      }

      // Call test endpoint
      const testResult = await page.evaluate(async (testProvider) => {
        const response = await fetch('./api/test-provider', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            type: testProvider.type,
            apiKey: testProvider.apiKey,
            projectId: testProvider.projectId,
            location: testProvider.location,
            baseUrl: testProvider.baseUrl,
            model: testProvider.model
          })
        });
        return await response.json();
      }, provider);

      if (testResult.success) {
        console.log(`✅ ${provider.name} test PASSED (${testResult.latency}ms)`);
        console.log(`   Response: ${testResult.response}`);
      } else {
        console.log(`❌ ${provider.name} test FAILED`);
        console.log(`   Error: ${testResult.error}`);
      }

      await page.waitForTimeout(1000);
    }
  });

  test('Check if API keys are being corrupted when saving', async () => {
    console.log('\n=== CHECKING API KEY PERSISTENCE ===\n');

    // Add a test provider with a known API key
    console.log('Adding a test provider...');

    await page.click('button:has-text("Add Provider")');
    await page.waitForSelector('#editModal.show', { timeout: 5000 });

    await page.fill('#edit-name', 'Test Provider - DO NOT USE');
    await page.selectOption('#edit-type', 'anthropic');
    await page.fill('#edit-apiKey', 'sk-ant-test-key-12345678901234567890');
    await page.fill('#edit-priority', '999');
    await page.check('#edit-enabled');

    // Submit form
    await page.click('#editForm button[type="submit"]');
    await page.waitForTimeout(1000);

    // Save config
    await page.click('button:has-text("Save Configuration")');
    await page.waitForTimeout(2000);

    console.log('Test provider added and config saved');

    // Now check what's in the config
    const configAfterSave = await page.evaluate(async () => {
      const response = await fetch('./api/config');
      return await response.json();
    });

    const testProvider = configAfterSave.providers.find(p => p.name === 'Test Provider - DO NOT USE');

    if (testProvider) {
      console.log('Test provider found in config:');
      console.log(`  Name: ${testProvider.name}`);
      console.log(`  Type: ${testProvider.type}`);
      console.log(`  API Key: ${testProvider.apiKey}`);
      console.log(`  API Key is masked: ${testProvider.apiKey.includes('...')}`);

      if (testProvider.apiKey.includes('...')) {
        console.log('❌ PROBLEM CONFIRMED: API key was masked in GET /api/config response');
      } else if (testProvider.apiKey === 'sk-ant-test-key-12345678901234567890') {
        console.log('✅ API key is intact in config');
      } else {
        console.log(`⚠️  API key was modified: ${testProvider.apiKey}`);
      }
    } else {
      console.log('❌ Test provider not found in config after save!');
    }

    // Clean up - delete the test provider
    console.log('\nCleaning up test provider...');
    const deleteButton = await page.$(`button[onclick*="deleteProvider"]:near(:text("Test Provider - DO NOT USE"))`);
    if (deleteButton) {
      await deleteButton.click();
      await page.waitForTimeout(500);
      // Confirm deletion
      page.on('dialog', dialog => dialog.accept());
      await page.waitForTimeout(500);
      console.log('Test provider deleted');
    }
  });

  test('Read actual API keys from server config file', async () => {
    console.log('\n=== CHECKING ACTUAL CONFIG FILE ON SERVER ===\n');

    // This test will show us if keys are properly stored on disk
    console.log('Note: This requires checking the server file system directly');
    console.log('Check /home/dblagbro/llm-proxy/config/providers.json on TMRwww01');
  });
});
