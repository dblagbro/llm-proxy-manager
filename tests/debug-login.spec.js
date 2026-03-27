const { test } = require('@playwright/test');

test('Debug login process', async ({ page }) => {
  console.log('\n=== LOGIN DEBUG ===\n');
  
  await page.goto('http://localhost:3000');
  console.log('1. Navigated to login page');
  
  const title1 = await page.title();
  console.log('2. Page title:', title1);
  
  await page.fill('input[name="username"]', 'dblagbro');
  await page.fill('input[name="password"]', 'Super*120120');
  console.log('3. Filled credentials');
  
  await page.screenshot({ path: '/tmp/before-login-click.png' });
  
  await page.click('button:has-text("Login")');
  console.log('4. Clicked login button');
  
  await page.waitForTimeout(2000);
  
  const title2 = await page.title();
  console.log('5. Page title after login:', title2);
  
  const url = page.url();
  console.log('6. Current URL:', url);
  
  await page.screenshot({ path: '/tmp/after-login-click.png', fullPage: true });
  
  const errorVisible = await page.locator('#error').isVisible().catch(() => false);
  if (errorVisible) {
    const errorText = await page.locator('#error').textContent();
    console.log('7. ERROR VISIBLE:', errorText);
  } else {
    console.log('7. No error visible');
  }
  
  const bodyText = await page.evaluate(() => document.body.textContent.substring(0, 200));
  console.log('8. Body text:', bodyText);
  
  console.log('\n==================\n');
});
