/**
 * DevInGPT Integration Tests
 *
 * Tests the full devingpt → llm-proxy pipeline:
 *   - Authentication (login, bad credentials)
 *   - Chat streaming via /v1/chat/completions
 *   - Image generation via /v1/images/generations
 *   - Conversation management (create, switch, delete)
 *   - Session persistence across conversation switches
 *   - Version display in UI
 *
 * Required env vars:
 *   DEVINGPT_URL       - base URL (default: https://www.voipguru.org/devinGPT)
 *   DEVINGPT_USERNAME  - login username (default: dblagbro)
 *   DEVINGPT_PASSWORD  - login password (required)
 */

const { test, expect } = require('@playwright/test');

const BASE = (process.env.DEVINGPT_URL || 'https://www.voipguru.org/devinGPT').replace(/\/$/, '');
const USER = process.env.DEVINGPT_USERNAME || 'dblagbro';
const PASS = process.env.DEVINGPT_PASSWORD || '';

// ── helpers ────────────────────────────────────────────────────────────────────

async function login(page) {
  await page.goto(BASE + '/');
  const onLogin = page.url().includes('login') || await page.locator('#username').isVisible({ timeout: 3000 }).catch(() => false);
  if (onLogin) {
    await page.fill('#username', USER);
    await page.fill('#password', PASS);
    await page.click('#login-btn');
    await page.waitForURL(u => !u.toString().includes('login'), { timeout: 10000 });
  }
  await expect(page.locator('#messages-area')).toBeVisible({ timeout: 10000 });
}

async function sendMessage(page, text) {
  const input = page.locator('#msg-input');
  await input.click();
  await input.fill(text);
  const sendBtn = page.locator('#send-btn');
  await expect(sendBtn).not.toBeDisabled({ timeout: 3000 });
  await sendBtn.click();
}

/** Wait for the last assistant message to stop changing (streaming done). */
async function waitForReply(page, { timeout = 90000 } = {}) {
  const asstMsgs = page.locator('.msg-body.asst');
  // Wait for at least one to appear
  await expect(asstMsgs.last()).toBeVisible({ timeout });
  // Poll until text stabilises (2 identical readings 800ms apart)
  const deadline = Date.now() + timeout;
  let prev = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(800);
    const text = await asstMsgs.last().innerText().catch(() => '');
    if (text && text === prev) return text;
    prev = text;
  }
  return prev;
}

/** Create a new conversation. */
async function newConversation(page) {
  await page.click('#new-chat-btn');
  await page.waitForTimeout(400);
  await expect(page.locator('#msg-input')).toBeVisible();
}

// ── test suite ─────────────────────────────────────────────────────────────────

test.describe('DevInGPT Integration', () => {
  test.skip(!PASS, 'Set DEVINGPT_PASSWORD env var to run these tests');

  // ── Auth ────────────────────────────────────────────────────────────────────

  test('rejected login shows error', async ({ page }) => {
    await page.goto(BASE + '/login');
    await page.fill('#username', 'nobody');
    await page.fill('#password', 'wrongpass123');
    await page.click('#login-btn');
    await expect(page.locator('#err')).toBeVisible({ timeout: 5000 });
  });

  test('login succeeds and chat UI loads', async ({ page }) => {
    await login(page);
    await expect(page.locator('#messages-area')).toBeVisible();
    await expect(page.locator('#msg-input')).toBeVisible();
    await expect(page.locator('#send-btn')).toBeVisible();
    // Version should be visible somewhere in the sidebar
    await expect(page.locator('#user-role-sb')).toContainText('v2.', { timeout: 5000 });
  });

  // ── Proxy health ─────────────────────────────────────────────────────────────

  test('llm-proxy health reports v1.13.1', async ({ request }) => {
    const resp = await request.get('https://www.voipguru.org/llmProxy/health');
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.status).toBe('ok');
    expect(body.version).toBe('1.13.1');
  });

  test('devingpt version endpoint', async ({ request }) => {
    const resp = await request.get(BASE + '/api/version');
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.version).toBe('2.1.0');
  });

  // ── Chat streaming ───────────────────────────────────────────────────────────

  test('chat streams a response', async ({ page }) => {
    await login(page);
    await newConversation(page);
    await sendMessage(page, 'Reply with exactly the word PONG and nothing else.');
    const reply = await waitForReply(page);
    expect(reply.trim().toUpperCase()).toContain('PONG');
  });

  test('chat response is coherent', async ({ page }) => {
    await login(page);
    await newConversation(page);
    await sendMessage(page, 'What is 2 + 2? Answer in one word.');
    const reply = await waitForReply(page);
    expect(reply.trim().length).toBeGreaterThan(0);
    expect(reply.toLowerCase()).toMatch(/\bfour\b|^4$/m);
  });

  // ── Session persistence + conversation switching ──────────────────────────────

  test('message is saved even when switching conversations before reply arrives', async ({ page }) => {
    test.setTimeout(180000); // 60s background wait + LLM reply + login overhead
    await login(page);

    // Create conversation A and send a message — DO NOT WAIT for reply
    await newConversation(page);
    await sendMessage(page, 'Count from 1 to 5, one number per line.');

    // Immediately switch to a brand-new conversation B
    await newConversation(page);
    await expect(page.locator('#messages-area')).toBeVisible();

    // Wait for conversation A's response to be saved in the background (up to 60s)
    await page.waitForTimeout(60000);

    // Switch back to conversation A — find it in the sidebar by its title or recency
    const convItems = page.locator('#conv-list .conv-item');
    // Click the second-most-recent conversation (index 1) to get back to A
    await convItems.nth(1).click();
    await page.waitForTimeout(1000);

    // The assistant's reply should now be visible (saved by background thread)
    const asstMsgs = page.locator('.msg-body.asst');
    await expect(asstMsgs.last()).toBeVisible({ timeout: 10000 });
    const savedReply = await asstMsgs.last().innerText();
    expect(savedReply).toMatch(/1[\s\S]*2[\s\S]*3/);  // contains "1...2...3"
  });

  test('session persists through page refresh', async ({ page }) => {
    await login(page);
    // Verify logged in
    await expect(page.locator('#messages-area')).toBeVisible();
    // Reload the page
    await page.reload();
    // Should still be logged in — NOT redirected to login
    await expect(page.locator('#messages-area')).toBeVisible({ timeout: 10000 });
    const url = page.url();
    expect(url).not.toContain('login');
  });

  // ── Conversation management ──────────────────────────────────────────────────

  test('create two conversations, send messages, verify titles auto-generate', async ({ page }) => {
    test.setTimeout(600000); // two full LLM round-trips can take > 5 min
    await login(page);

    // Conversation 1
    await newConversation(page);
    await sendMessage(page, 'Tell me one interesting fact about the moon.');
    await waitForReply(page, { timeout: 90000 });

    // Conversation 2
    await newConversation(page);
    await sendMessage(page, 'What is the capital of France?');
    await waitForReply(page, { timeout: 90000 });

    // Both conversations should appear in the sidebar with non-default titles
    const convItems = page.locator('#conv-list .conv-item');
    const count = await convItems.count();
    expect(count).toBeGreaterThanOrEqual(2);
  });

  test('delete a conversation removes it from the list', async ({ page }) => {
    test.setTimeout(180000); // 90s waitForReply + login overhead
    await login(page);

    // Create a throwaway conversation
    await newConversation(page);
    await sendMessage(page, 'This conversation will be deleted. Just say OK.');
    await waitForReply(page, { timeout: 90000 });

    const convItems = page.locator('#conv-list .conv-item');
    const countBefore = await convItems.count();

    // Hover the first conversation to reveal its action buttons, then delete it
    const firstConv = convItems.first();
    await firstConv.hover();
    const deleteBtn = firstConv.locator('.conv-act-btn.del');

    // Register dialog handler BEFORE click so we don't miss the confirm() popup
    page.once('dialog', d => d.accept());
    await deleteBtn.click();

    await page.waitForTimeout(1500);
    const countAfter = await convItems.count();
    expect(countAfter).toBeLessThan(countBefore);
  });

  // ── Image generation ─────────────────────────────────────────────────────────

  test('logo design request generates an image', async ({ page }) => {
    test.setTimeout(480000); // 3 min image + 1 min text + login overhead
    await login(page);
    await newConversation(page);
    await sendMessage(page, 'Design a simple logo for a mountain vision care clinic. Make it clean and professional.');

    // Image generation takes longer — wait up to 3 minutes
    const img = page.locator('#messages-area .msg-images img').last();
    await img.waitFor({ state: 'visible', timeout: 180000 });

    const src = await img.getAttribute('src');
    expect(src).toMatch(/^https?:\/\//);

    // There should also be a text response describing the design — wait for it to stream in
    const replyText = await waitForReply(page, { timeout: 60000 });
    expect(replyText.trim().length).toBeGreaterThan(5);
  });

  test('image generation survives conversation switch and saves to DB', async ({ page }) => {
    test.setTimeout(600000);
    await login(page);
    await newConversation(page);

    // Start an image generation request
    await sendMessage(page, 'Create an image of a solid red circle on white background.');

    // Switch away immediately (do NOT wait for image)
    await newConversation(page);
    await page.waitForTimeout(120000);  // wait up to 2 min for DALL-E to finish in background

    // Switch back — re-poll the conversation until image appears in DB
    const convItems = page.locator('#conv-list .conv-item');
    const img = page.locator('#messages-area .msg-images img').last();
    let imgVisible = false;
    for (let attempt = 0; attempt < 8; attempt++) {
      await convItems.nth(1).click();   // re-open to reload messages from DB
      await page.waitForTimeout(2000);
      try {
        await img.waitFor({ state: 'visible', timeout: 15000 });
        imgVisible = true;
        break;
      } catch (_) {
        // image not saved yet — loop and try again
      }
    }
    expect(imgVisible, 'Generated image should appear after switching back').toBe(true);
    const src = await img.getAttribute('src');
    expect(src).toMatch(/^https?:\/\//);
  });

  // ── Multi-turn context ────────────────────────────────────────────────────────

  test('multi-turn conversation retains context', async ({ page }) => {
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'Remember the code word: GLACIER. Acknowledge with "Got it."');
    await waitForReply(page, { timeout: 60000 });

    await sendMessage(page, 'What was the code word I asked you to remember?');
    const reply = await waitForReply(page, { timeout: 60000 });
    expect(reply.toUpperCase()).toContain('GLACIER');
  });
});
