const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

test('debug login flow with console logs', async ({ page }) => {
  // Capture console messages
  page.on('console', msg => console.log('BROWSER CONSOLE:', msg.type(), msg.text()));

  // Capture network failures
  page.on('requestfailed', request => {
    console.log('REQUEST FAILED:', request.url(), request.failure().errorText);
  });

  // Capture network responses
  page.on('response', response => {
    if (response.url().includes('/api/auth/login')) {
      console.log('LOGIN RESPONSE:', response.status(), response.url());
    }
  });

  await page.goto(BASE_URL + '/login.html');

  console.log('Current URL:', page.url());

  await page.fill('[name=username]', 'admin');
  await page.fill('[name=password]', 'admin');

  console.log('Clicking login button...');
  await page.click('button[type=submit]');

  // Wait a bit to see what happens
  await page.waitForTimeout(3000);

  console.log('Final URL:', page.url());

  // Check what's on the page
  const bodyText = await page.textContent('body');
  console.log('Page contains:', bodyText.substring(0, 200));
});
