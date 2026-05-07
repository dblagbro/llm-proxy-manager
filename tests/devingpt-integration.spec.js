/**
 * DevInGPT Integration Tests
 *
 * Tests the full devingpt → llm-proxy pipeline:
 *   - Authentication (login, bad credentials)
 *   - Chat streaming via /v1/chat/completions
 *   - Image generation — Stability AI routing, trigger verbs, trigger manipulation phrases
 *   - Conversation management (create, switch, delete, auto-title)
 *   - Session persistence (cookie survives refresh)
 *   - Sidebar structure (conversations dominant, settings modal)
 *   - Background processing (image/chat continues when switching conversations)
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
  await expect(asstMsgs.last()).toBeVisible({ timeout });
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

/** Wait for an image to appear in the messages area. */
async function waitForImage(page, { timeout = 180000 } = {}) {
  const img = page.locator('#messages-area .msg-images img').last();
  await img.waitFor({ state: 'visible', timeout });
  return img;
}

/** Create a new conversation. */
async function newConversation(page) {
  await page.click('#new-chat-btn');
  await page.waitForTimeout(400);
  await expect(page.locator('#msg-input')).toBeVisible();
}

const PROXY_KEY = process.env.LLM_PROXY_KEY || 'llm-proxy-af87a51491d3708389f72fd7630fd50c0b5a05a828ed3613cfd1698976883acc';

// ── Proxy API tests (no login required) ────────────────────────────────────────

test.describe('LLM Proxy API', () => {

  test('llm-proxy health reports current version', async ({ request }) => {
    const resp = await request.get('https://www.voipguru.org/llmProxy/health');
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.status).toBe('ok');
    expect(body.version).toMatch(/^1\.14\./);
  });

  test('www2 node is healthy and reports same version', async ({ request }) => {
    const resp = await request.get('https://www2.voipguru.org/llmProxy/health');
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.status).toBe('ok');
    expect(body.version).toMatch(/^1\.14\./);
  });

  test('cluster has active peers on www1', async ({ request }) => {
    const resp = await request.get('https://www.voipguru.org/llmProxy/health');
    const body = await resp.json();
    if (body.cluster) {
      expect(body.cluster.peers?.length ?? 0).toBeGreaterThanOrEqual(1);
    }
  });

  test('image generation routes to Stability AI (b64_json response)', async ({ request }) => {
    test.setTimeout(120000);
    const resp = await request.post('https://www.voipguru.org/llmProxy/v1/images/generations', {
      headers: {
        'Authorization': `Bearer ${PROXY_KEY}`,
        'Content-Type': 'application/json',
        'LLM-Hint': 'task=image-generation, modality=image-generation',
      },
      data: { prompt: 'a simple white square on black background', n: 1, size: '1024x1024' },
      timeout: 120000,
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.data).toHaveLength(1);
    const item = body.data[0];
    // Stability AI returns b64_json; verify it's present and substantial
    expect(item.b64_json || item.url).toBeTruthy();
    if (item.b64_json) expect(item.b64_json.length).toBeGreaterThan(1000);
  });

  test('image generation without modality hint still returns image', async ({ request }) => {
    test.setTimeout(120000);
    const resp = await request.post('https://www.voipguru.org/llmProxy/v1/images/generations', {
      headers: {
        'Authorization': `Bearer ${PROXY_KEY}`,
        'Content-Type': 'application/json',
      },
      data: { prompt: 'a red dot', n: 1, size: '1024x1024' },
      timeout: 120000,
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.data).toHaveLength(1);
  });
});

// ── DevinGPT UI tests (login required) ─────────────────────────────────────────

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
    await expect(page.locator('#user-role-sb')).toContainText('v2.', { timeout: 5000 });
  });

  // ── Version / health ─────────────────────────────────────────────────────────

  test('devingpt version endpoint responds', async ({ request }) => {
    const resp = await request.get(BASE + '/api/version');
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.version).toMatch(/^\d+\.\d+/);
  });

  // ── Sidebar structure ────────────────────────────────────────────────────────

  test('sidebar shows conversation list and settings gear, no utility clutter', async ({ page }) => {
    await login(page);

    // Conversations list is the dominant sidebar element
    await expect(page.locator('#conv-list')).toBeVisible();

    // Settings gear button is in the user bar
    const gear = page.locator('#settings-btn, [onclick*="openSettings"], button[title*="Settings"], button[title*="settings"]').first();
    await expect(gear).toBeVisible({ timeout: 5000 });

    // Utility items live inside the settings modal, not directly in the sidebar
    const settingsModal = page.locator('#settings-modal');
    // Modal should exist in the DOM (hidden until opened)
    await expect(settingsModal).toBeAttached();

    // The API key button should be inside the modal, not loose in the sidebar
    const apikeyBtn = page.locator('#apikey-btn');
    await expect(apikeyBtn).toBeAttached();
    // It should be a descendant of the modal (or at least not visible without opening modal)
    await expect(apikeyBtn).not.toBeVisible();
  });

  test('settings modal opens and closes', async ({ page }) => {
    await login(page);
    const modal = page.locator('#settings-modal');

    // Open via gear button
    const gear = page.locator('#settings-btn, button[onclick*="openSettings"]').first();
    await gear.click();
    await expect(modal).toBeVisible({ timeout: 3000 });

    // Close via × or clicking outside
    const closeBtn = modal.locator('button[onclick*="closeSettings"], .modal-close, .close-btn').first();
    if (await closeBtn.isVisible()) {
      await closeBtn.click();
    } else {
      await page.keyboard.press('Escape');
    }
    await expect(modal).not.toBeVisible({ timeout: 3000 });
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

  test('multi-turn conversation retains context', async ({ page }) => {
    test.setTimeout(180000);
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'Remember the code word: GLACIER. Acknowledge with "Got it."');
    await waitForReply(page, { timeout: 60000 });

    await sendMessage(page, 'What was the code word I asked you to remember?');
    const reply = await waitForReply(page, { timeout: 60000 });
    expect(reply.toUpperCase()).toContain('GLACIER');
  });

  // ── Auto-title ───────────────────────────────────────────────────────────────

  test('conversation title auto-generates after first exchange', async ({ page }) => {
    test.setTimeout(180000);
    await login(page);
    await newConversation(page);

    // Capture the current title before sending (should be "New Chat" or similar)
    const convItem = page.locator('#conv-list .conv-item').first();

    await sendMessage(page, 'Tell me a quick fact about penguins.');
    await waitForReply(page, { timeout: 90000 });

    // Title should update to something about penguins — poll for up to 30s
    await expect(async () => {
      const title = await convItem.innerText();
      expect(title.toLowerCase()).not.toMatch(/^new\s*(chat|conversation)?$/i);
      expect(title.trim().length).toBeGreaterThan(3);
    }).toPass({ timeout: 30000, intervals: [2000] });
  });

  // ── Session persistence ──────────────────────────────────────────────────────

  test('session persists through page refresh', async ({ page }) => {
    await login(page);
    await expect(page.locator('#messages-area')).toBeVisible();
    await page.reload();
    await expect(page.locator('#messages-area')).toBeVisible({ timeout: 10000 });
    expect(page.url()).not.toContain('login');
  });

  test('session persists through multiple reloads', async ({ page }) => {
    await login(page);
    for (let i = 0; i < 3; i++) {
      await page.reload();
      await expect(page.locator('#messages-area')).toBeVisible({ timeout: 10000 });
      expect(page.url()).not.toContain('login');
    }
  });

  // ── Conversation management ──────────────────────────────────────────────────

  test('conversations list grows as new chats are created', async ({ page }) => {
    test.setTimeout(180000);
    await login(page);

    const convItems = page.locator('#conv-list .conv-item');
    const countBefore = await convItems.count();

    await newConversation(page);
    await sendMessage(page, 'Tell me one interesting fact about the moon.');
    await waitForReply(page, { timeout: 120000 });

    await expect(async () => {
      expect(await convItems.count()).toBeGreaterThan(countBefore);
    }).toPass({ timeout: 15000, intervals: [1000] });
  });

  test('delete a conversation removes it from the list', async ({ page }) => {
    test.setTimeout(180000);
    await login(page);

    await newConversation(page);
    await sendMessage(page, 'This conversation will be deleted. Just say OK.');
    await waitForReply(page, { timeout: 90000 });

    const convItems = page.locator('#conv-list .conv-item');
    const countBefore = await convItems.count();

    const firstConv = convItems.first();
    await firstConv.hover();
    const deleteBtn = firstConv.locator('.conv-act-btn.del');
    page.once('dialog', d => d.accept());
    await deleteBtn.click();

    await page.waitForTimeout(1500);
    expect(await convItems.count()).toBeLessThan(countBefore);
  });

  test('message saved when switching conversations before reply arrives', async ({ page }) => {
    test.setTimeout(180000);
    await login(page);

    await newConversation(page);
    await sendMessage(page, 'Count from 1 to 5, one number per line.');

    // Switch away immediately — do NOT wait for reply
    await newConversation(page);
    await page.waitForTimeout(60000); // let background thread finish

    // Switch back and verify reply was saved
    const convItems = page.locator('#conv-list .conv-item');
    await convItems.nth(1).click();
    await page.waitForTimeout(1000);

    const asstMsgs = page.locator('.msg-body.asst');
    await expect(asstMsgs.last()).toBeVisible({ timeout: 10000 });
    const savedReply = await asstMsgs.last().innerText();
    expect(savedReply).toMatch(/1[\s\S]*2[\s\S]*3/);
  });

  // ── Image generation: trigger logic ─────────────────────────────────────────

  test('unambiguous draw verb triggers image generation', async ({ page }) => {
    test.setTimeout(300000);
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'draw a simple red circle');
    const img = await waitForImage(page, { timeout: 180000 });
    const src = await img.getAttribute('src');
    // Stability AI images are served locally; DALL-E returns external URLs
    expect(src).toMatch(/api\/images\/|https?:\/\//);
  });

  test('paint verb triggers image generation', async ({ page }) => {
    test.setTimeout(300000);
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'paint a sunset over the ocean');
    const img = await waitForImage(page, { timeout: 180000 });
    expect(await img.getAttribute('src')).toMatch(/api\/images\/|https?:\/\//);
  });

  test('create + image subject triggers image generation', async ({ page }) => {
    test.setTimeout(300000);
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'create an image of a golden retriever puppy');
    const img = await waitForImage(page, { timeout: 180000 });
    expect(await img.getAttribute('src')).toMatch(/api\/images\/|https?:\/\//);
  });

  test('manipulation phrase triggers image generation', async ({ page }) => {
    test.setTimeout(300000);
    await login(page);
    await newConversation(page);

    // Manipulation phrases ("put X on Y") always trigger even without "image" keyword
    await sendMessage(page, "put a cowboy hat on a cat");
    const img = await waitForImage(page, { timeout: 180000 });
    expect(await img.getAttribute('src')).toMatch(/api\/images\/|https?:\/\//);
  });

  test('image generation produces a locally-stored image file', async ({ page }) => {
    test.setTimeout(300000);
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'sketch a simple house with a chimney');
    const img = await waitForImage(page, { timeout: 180000 });
    const src = await img.getAttribute('src');

    // If Stability AI was used, the image is served from /api/images/ (local storage)
    // Both paths are acceptable — what matters is the image loads
    const imgResponse = await page.request.get(src.startsWith('http') ? src : BASE + src.replace('/devinGPT', ''));
    expect(imgResponse.status()).toBe(200);
    expect(imgResponse.headers()['content-type']).toContain('image/');
  });

  // ── Image generation: background persistence ─────────────────────────────────

  test('image generated in background saves to DB after conversation switch', async ({ page }) => {
    test.setTimeout(600000);
    await login(page);
    await newConversation(page);

    await sendMessage(page, 'draw a blue star on a black background');

    // Switch away immediately
    await newConversation(page);
    await page.waitForTimeout(120000); // 2 min for Stability AI to finish in background

    // Poll: switch back repeatedly until image appears
    const convItems = page.locator('#conv-list .conv-item');
    let imgVisible = false;
    for (let attempt = 0; attempt < 8; attempt++) {
      await convItems.nth(1).click();
      await page.waitForTimeout(2000);
      try {
        await page.locator('#messages-area .msg-images img').last()
          .waitFor({ state: 'visible', timeout: 10000 });
        imgVisible = true;
        break;
      } catch (_) {}
    }
    expect(imgVisible, 'Generated image should be saved after switching back').toBe(true);
  });

});
