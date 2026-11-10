"""Run only inside an isolated browser container against synthetic profiles."""
import asyncio
import json
import sys
from playwright.async_api import async_playwright


async def main():
    mode = sys.argv[1]
    assert mode in ('create', 'verify')
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context('/profile', headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'])
        await context.route('**/*', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<title>Offline profile fixture</title>'))
        page = await context.new_page()
        await page.goto('http://profile.example.test/')
        if mode == 'create':
            await context.add_cookies([{'name':'synthetic_marker','value':'fixture-not-a-secret',
                'domain':'profile.example.test','path':'/','expires':2000000000}])
            await page.evaluate("localStorage.setItem('synthetic_marker','fixture-not-a-secret')")
        cookies = await context.cookies('http://profile.example.test/')
        assert any(c['name']=='synthetic_marker' and c['value']=='fixture-not-a-secret' for c in cookies)
        assert await page.evaluate("localStorage.getItem('synthetic_marker')") == 'fixture-not-a-secret'
        version = context.browser.version
        await context.close()
    print(json.dumps({'mode':mode,'cookie_preserved':True,'local_storage_preserved':True,
        'closed_cleanly':True,'browser_version':version,'network':'none','real_account_used':False}))


asyncio.run(main())
