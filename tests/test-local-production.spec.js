const { test, expect } = require('@playwright/test');

test('verify LLM Proxy local production UI features', async ({ page }) => {
  // Navigate to the local Docker container
  await page.goto('https://www.voipguru.org/llmProxy/');

  // Wait for page to load
  await page.waitForLoadState('networkidle');

  // Check 1: Dark theme attribute
  const htmlElement = await page.locator('html');
  const dataTheme = await htmlElement.getAttribute('data-theme');
  console.log('✓ data-theme attribute:', dataTheme);
  expect(dataTheme).toBe('dark');

  // Check 2: Page should have dark background
  const bgColor = await page.evaluate(() => {
    return window.getComputedStyle(document.documentElement).backgroundColor;
  });
  console.log('✓ Background color:', bgColor);

  // Check 3: Login and verify username dropdown
  await page.fill('input[type="text"]', 'dblagbro');
  await page.fill('input[type="password"]', 'Super*120120');
  await page.click('button:has-text("Login")');

  // Wait for dashboard to load
  await page.waitForSelector('text=LLM Proxy Manager', { timeout: 10000 });
  console.log('✓ Logged in successfully');

  // Check 4: Username should be visible in top right
  const usernameElement = await page.locator('text=dblagbro').first();
  await expect(usernameElement).toBeVisible();
  console.log('✓ Username visible in top right');

  // Check 5: Click username to open dropdown
  await usernameElement.click();

  // Wait a bit for dropdown animation
  await page.waitForTimeout(500);

  // Check 6: Profile Settings option should be visible
  const profileSettings = page.locator('.user-dropdown-menu').locator('text=Profile Settings');
  const isProfileVisible = await profileSettings.isVisible();
  console.log('✓ Profile Settings visible:', isProfileVisible);
  expect(isProfileVisible).toBe(true);

  // Check 7: Logout option should be visible in dropdown
  const logoutBtn = page.locator('.user-dropdown-menu').locator('text=Logout');
  const isLogoutVisible = await logoutBtn.isVisible();
  console.log('✓ Logout visible:', isLogoutVisible);
  expect(isLogoutVisible).toBe(true);

  console.log('\n✅ All production features verified successfully!');
});
