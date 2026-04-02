const { test, expect } = require('@playwright/test');

test('Check LLM Proxy UI', async ({ page }) => {
  await page.goto('https://www.voipguru.org/llmProxy/');
  
  // Login
  await page.fill('input[type="text"]', 'dblagbro');
  await page.fill('input[type="password"]', 'Super*120120');
  await page.click('button:has-text("Login")');
  
  await page.waitForTimeout(2000);
  
  // Take screenshot
  await page.screenshot({ path: '/tmp/llm-proxy-ui.png', fullPage: true });
  
  // Check theme
  const html = await page.locator('html').getAttribute('data-theme');
  console.log('1. Theme attribute:', html || 'NOT SET');
  
  // Check if it's actually dark
  const bgColor = await page.evaluate(() => {
    return window.getComputedStyle(document.body).backgroundColor;
  });
  console.log('2. Body background color:', bgColor);
  
  // Check for username element
  const usernameText = await page.locator('#userDisplay, .user-dropdown-toggle').textContent().catch(() => 'NOT FOUND');
  console.log('3. Username element text:', usernameText);
  
  // Check if dropdown exists
  const hasDropdown = await page.locator('.user-dropdown-menu').count();
  console.log('4. User dropdown menu count:', hasDropdown);
  
  // Check for Last Status or Last Error
  const statusText = await page.locator('text=Last Status, text=Last Error').first().textContent().catch(() => 'NOT FOUND');
  console.log('5. Status label:', statusText);
  
  // Check CSS variables
  const cssVars = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    return {
      bg: root.getPropertyValue('--bg').trim(),
      text: root.getPropertyValue('--text').trim(),
      accent: root.getPropertyValue('--accent').trim()
    };
  });
  console.log('6. CSS Variables:', JSON.stringify(cssVars));
  
  console.log('\nHTML snippet from header:');
  const headerHTML = await page.locator('.header-actions, .header').innerHTML();
  console.log(headerHTML.substring(0, 500));
});
