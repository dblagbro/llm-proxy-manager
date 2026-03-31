const { test, expect } = require('@playwright/test');

test('Configure SMTP settings and test email', async ({ page }) => {
  // Navigate to the application
  await page.goto('http://localhost:3000');

  // Login
  await page.fill('input[name="username"]', 'dblagbro');
  await page.fill('input[name="password"]', 'Super*120120');
  await page.click('button[type="submit"]');

  // Wait for dashboard to load
  await page.waitForSelector('text=Dashboard', { timeout: 10000 });
  console.log('✓ Logged in successfully');

  // Open Settings modal
  await page.click('button:has-text("Settings")');
  await page.waitForSelector('text=Email Notifications', { timeout: 5000 });
  console.log('✓ Settings modal opened');

  // Navigate to Email Notifications tab
  const emailTab = page.locator('button:has-text("Email Notifications")');
  await emailTab.click();
  await page.waitForTimeout(500);
  console.log('✓ Email Notifications tab opened');

  // Fill in SMTP settings (using Gmail SMTP which is commonly used)
  await page.fill('#smtp-host', 'smtp.gmail.com');
  await page.fill('#smtp-port', '587');
  await page.fill('#smtp-user', 'dblagbro@gmail.com');
  await page.fill('#smtp-pass', ''); // Will need actual app password
  await page.fill('#smtp-from', 'dblagbro@gmail.com');
  await page.fill('#smtp-to', 'dblagbro@voipguru.org');

  // Check secure for TLS (port 587 uses STARTTLS)
  const secureCheckbox = page.locator('#smtp-secure');
  if (await secureCheckbox.isVisible()) {
    await secureCheckbox.check();
  }

  // Enable SMTP
  const enableCheckbox = page.locator('#smtp-enabled');
  if (!(await enableCheckbox.isChecked())) {
    await enableCheckbox.check();
  }

  console.log('✓ SMTP settings filled');

  // Save settings
  await page.click('button:has-text("Save")');
  await page.waitForTimeout(1000);
  console.log('✓ Settings saved');

  // Test email
  console.log('Testing email send...');
  const testButton = page.locator('button:has-text("Test Email")');
  await testButton.click();

  // Wait for response
  await page.waitForTimeout(3000);

  // Check for success or error message
  const successMessage = page.locator('text=/success|sent/i');
  const errorMessage = page.locator('text=/error|fail/i');

  if (await successMessage.isVisible({ timeout: 2000 }).catch(() => false)) {
    console.log('✓ Test email sent successfully');
  } else if (await errorMessage.isVisible({ timeout: 2000 }).catch(() => false)) {
    const errorText = await errorMessage.textContent();
    console.log('✗ Test email failed:', errorText);
    throw new Error('Test email failed: ' + errorText);
  } else {
    console.log('⚠ No clear success/error message found');
  }
});
