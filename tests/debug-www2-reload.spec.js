const { test } = require('@playwright/test');

test('Debug www2 reload issue', async ({ page }) => {
  // Track navigation events
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) {
      console.log(`[NAVIGATE] ${new Date().toISOString()} - ${frame.url()}`);
    }
  });

  // Track console messages
  page.on('console', msg => {
    console.log(`[CONSOLE ${msg.type()}] ${msg.text()}`);
  });

  console.log('[START] Loading www2.voipguru.org/llmProxy/');

  // Go to www2
  await page.goto('https://www2.voipguru.org/llmProxy/login.html');

  console.log('[LOGIN] Entering credentials');
  await page.fill('input[name="username"]', 'dblagbro');
  await page.fill('input[name="password"]', 'Super*120120');
  await page.click('button[type="submit"]');

  console.log('[WAIT] Waiting for main page to load');
  await page.waitForURL('**/llmProxy/', { timeout: 10000 });

  console.log('[OBSERVE] Watching for reloads for 30 seconds...');

  // Wait and watch for reloads
  await page.waitForTimeout(30000);

  console.log('[DONE] Observation complete');
});
