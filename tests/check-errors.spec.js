const { test } = require('@playwright/test');

test('Check for JavaScript errors', async ({ page }) => {
  const errors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') {
      errors.push(msg.text());
      console.log('❌ Console Error:', msg.text());
    }
  });
  
  page.on('pageerror', err => {
    errors.push(err.message);
    console.log('❌ Page Error:', err.message);
  });
  
  await page.goto('http://localhost:3000');
  
  // Login
  await page.fill('input[name="username"]', 'dblagbro');
  await page.fill('input[name="password"]', 'Super*120120');
  await page.click('button:has-text("Login")');
  
  await page.waitForTimeout(3000);
  
  console.log('\n=== JavaScript Errors Found:', errors.length, '===');
  if (errors.length === 0) {
    console.log('✓ No JavaScript errors');
  }
  
  // Check if page loaded
  const title = await page.title();
  console.log('Page title:', title);
  
  // Check what's actually in the DOM
  const bodyHTML = await page.evaluate(() => document.body.innerHTML.substring(0, 500));
  console.log('\nFirst 500 chars of body:', bodyHTML);
});
