const { test, expect } = require('@playwright/test');

test('Verify all UI features', async ({ page }) => {
  await page.goto('http://localhost:3000');
  
  // Login
  await page.fill('input[name="username"]', 'dblagbro');
  await page.fill('input[name="password"]', 'Super*120120');
  await page.click('button:has-text("Login")');
  
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3000);
  
  // Screenshot after login
  await page.screenshot({ path: '/tmp/after-login.png', fullPage: true });
  
  console.log('\n=== VERIFICATION RESULTS ===\n');
  
  // 1. Check theme
  const themeAttr = await page.locator('html').getAttribute('data-theme');
  console.log('1. Theme attribute on <html>:', themeAttr || '❌ NOT SET');
  
  // 2. Check computed background color
  const bgColor = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  console.log('2. Body background:', bgColor);
  
  // 3. Check if user dropdown exists and is clickable
  const dropdownButton = page.locator('.user-dropdown-toggle');
  const dropdownExists = await dropdownButton.count() > 0;
  console.log('3. User dropdown button exists:', dropdownExists ? '✓ YES' : '❌ NO');
  
  if (dropdownExists) {
    const buttonText = await dropdownButton.textContent();
    console.log('   Button text:', buttonText.trim());
  }
  
  // 4. Try to click dropdown
  if (dropdownExists) {
    await dropdownButton.click();
    await page.waitForTimeout(500);
    const menuVisible = await page.locator('.user-dropdown-menu.open').isVisible().catch(() => false);
    console.log('4. Dropdown menu opens:', menuVisible ? '✓ YES' : '❌ NO');
    
    if (menuVisible) {
      const hasProfileSettings = await page.locator('.dropdown-item:has-text("Profile Settings")').isVisible();
      console.log('   Has "Profile Settings":', hasProfileSettings ? '✓ YES' : '❌ NO');
    }
  }
  
  // 5. Check for Last Status vs Last Error
  const hasLastStatus = await page.locator('text=Last Status').count();
  const hasLastError = await page.locator('text=Last Error').count();
  console.log('5. "Last Status" count:', hasLastStatus);
  console.log('   "Last Error" count:', hasLastError);
  
  // 6. Check CSS variables
  const cssVars = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      bg: style.getPropertyValue('--bg'),
      text: style.getPropertyValue('--text'),
      accent: style.getPropertyValue('--accent')
    };
  });
  console.log('6. CSS Variables:', JSON.stringify(cssVars, null, 2));
  
  console.log('\n=========================\n');
});
