const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.BASE_URL || 'https://www.voipguru.org/llmProxy';
const ADMIN_USER = process.env.ADMIN_USER || 'dblagbro';
const ADMIN_PASS = process.env.ADMIN_PASS || 'admin';

test.describe('Stability AI Image Generation', () => {
  let page;

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    page.on('console', msg => console.log('  [browser]', msg.type(), msg.text()));

    await page.goto(`${BASE_URL}/login.html`);
    await page.fill('#username', ADMIN_USER);
    await page.fill('#password', ADMIN_PASS);
    await page.click('button[type="submit"]');
    await page.waitForURL('**/' + BASE_URL.split('/').pop() + '/', { timeout: 15000 });
    console.log('✅ Logged in');
  });

  test.afterAll(async () => {
    await page.close();
  });

  test('Stability AI provider test button returns success', async () => {
    await page.goto(`${BASE_URL}/`);
    await page.waitForSelector('.provider-item', { timeout: 10000 });

    // Find the Stability AI provider card
    const allCards = await page.$$('.provider-item');
    let stabilityCard = null;
    for (const card of allCards) {
      const name = await card.$eval('.provider-name, h3, .name', el => el.textContent).catch(() => '');
      if (/stability/i.test(name)) {
        stabilityCard = card;
        console.log(`Found Stability AI card: "${name.trim()}"`);
        break;
      }
    }
    expect(stabilityCard, 'Stability AI provider card not found').toBeTruthy();

    // Intercept the test-provider API response
    const [response] = await Promise.all([
      page.waitForResponse(resp => resp.url().includes('/api/test-provider'), { timeout: 20000 }),
      stabilityCard.$eval('button.btn-test, button[data-action="test"], button:has-text("Test")', btn => btn.click()),
    ]);

    const status = response.status();
    const body = await response.json().catch(() => ({}));
    console.log(`  → HTTP ${status}:`, JSON.stringify(body));

    expect(status, `Expected 200, got ${status}: ${JSON.stringify(body)}`).toBe(200);
    expect(body.success).toBe(true);
  });

  test('POST /v1/images/generations routes to Stability AI', async () => {
    // Get a valid API key from the page (or use the coordinator hub key)
    const apiKey = await page.evaluate(async (base) => {
      const r = await fetch(`${base}/api/client-keys`, { credentials: 'include' });
      const d = await r.json();
      const keys = d.keys || d;
      return keys.find(k => k.enabled)?.key_value || null;
    }, BASE_URL);

    console.log(`  Using API key: ${apiKey ? apiKey.slice(0, 12) + '...' : 'none found'}`);
    expect(apiKey, 'No enabled API key found').toBeTruthy();

    const response = await page.evaluate(async ({ base, key }) => {
      const r = await fetch(`${base}/v1/images/generations`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${key}`,
          'X-LMRH': 'modality=image-generation',
        },
        body: JSON.stringify({ prompt: 'a red apple on a white table', n: 1, size: '512x512' }),
      });
      return { status: r.status, body: await r.text() };
    }, { base: BASE_URL, key: apiKey });

    console.log(`  → HTTP ${response.status}: ${response.body.slice(0, 300)}`);

    if (response.status === 403) {
      // Stability API key is invalid/expired — that's the root issue to surface
      let parsed = {};
      try { parsed = JSON.parse(response.body); } catch (_) {}
      console.error('  ❌ 403 from Stability AI — API key likely invalid or expired');
      console.error('  Stability error:', JSON.stringify(parsed));
      // Fail with a descriptive message
      expect(response.status, `Stability AI returned 403: ${response.body.slice(0, 200)}`).toBe(200);
    }

    expect(response.status).toBe(200);
    const parsed = JSON.parse(response.body);
    expect(parsed.data).toBeDefined();
    expect(parsed.data.length).toBeGreaterThan(0);
    // Should have either b64_json or url
    const img = parsed.data[0];
    expect(img.b64_json || img.url).toBeTruthy();
    console.log(`  ✅ Image generated, response type: ${img.b64_json ? 'b64_json' : 'url'}`);
  });
});
