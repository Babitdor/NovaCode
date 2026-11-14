"""Regressions for linear Agent mode and switching back to Human mode."""
from functools import partial
from http.server import ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright

from verify_site import ROOT, Handler


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/NovaCode/'
    shots = ROOT / '.shots/agent'
    shots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            for width in (1440, 390):
                context = browser.new_context(
                    viewport={'width': width, 'height': 1000},
                    color_scheme='light' if width == 1440 else 'dark',
                    reduced_motion='no-preference' if width == 1440 else 'reduce',
                )
                context.route('https://api.github.com/**', lambda r: r.fulfill(status=403, body='{}'))
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                for direct in (False, True):
                    page.goto(base + '?view=' + ('agent' if direct else 'human'))
                    page.wait_for_function("!document.querySelector('#boot')")
                    if not direct:
                        page.wait_for_function("!document.documentElement.classList.contains('enter')")
                        page.locator('#faq details').first.evaluate('e => e.open = true')
                        page.locator('[data-view-toggle]').first.click()
                    page.wait_for_function("document.documentElement.dataset.view === 'agent'")
                    assert page.locator('.hero > .wrap').evaluate('e => e.clientWidth >= Math.min(innerWidth, 1400) * .9')
                    assert page.locator('h1:visible').count() == 1
                    assert not page.locator('.pin-spacer').count()
                    assert not page.locator('.tutorial img:visible').count()
                    assert not page.locator('#faq details:not([open])').count()
                    assert page.evaluate("[...document.querySelectorAll('.mo-word')].every(e => getComputedStyle(e).opacity === '1' && getComputedStyle(e).visibility === 'visible')")
                    assert page.evaluate("[...document.querySelectorAll('[data-image-reveal]')].every(e => getComputedStyle(e).clipPath === 'none')")
                    assert not page.evaluate("document.documentElement.classList.contains('lenis')")
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(shots / f'home-{width}-{direct}.png'))
                    page.locator('[data-view-toggle]').first.click()
                    page.wait_for_function("document.documentElement.dataset.view === 'human'")
                    if not direct:
                        assert page.locator('#faq details[open]').count() == 1
                    assert page.locator('.tutorial img:visible').count() == 5
                    if width == 1440:
                        assert page.locator('.rail-pinned').count() == 1
                    page.reload()
                    assert page.evaluate('NovaView.get()') == 'human'
                # A stored choice follows navigation to documentation and releases.
                page.evaluate("NovaView.set('agent')")
                page.goto(base + 'docs/')
                assert page.locator('.page-section:visible').count() == 30
                assert page.locator('.agent-docs-note').evaluate('e => e.getBoundingClientRect().top < innerHeight')
                assert page.locator('[role="tabpanel"]:visible').count() == 3
                page.locator('.agent-docs-note a[href="#page-memory"]').click()
                page.wait_for_function("Math.abs(document.querySelector('#page-memory').getBoundingClientRect().top) < 180")
                page.screenshot(path=str(shots / f'docs-{width}.png'))
                page.locator('[data-view-toggle]').first.click()
                assert page.locator('.page-section:visible').count() == 1
                assert page.locator('#page-memory').is_visible()
                # New releases loaded after startup must also be expanded.
                page.route('https://api.github.com/**', lambda r: r.fulfill(json=[{
                    'type': 'file', 'name': 'CHANGELOG-v9.9.9.md', 'sha': 'agent-fixture',
                }]))
                page.route('https://raw.githubusercontent.com/**', lambda r: r.fulfill(body='# v9.9.9\n\n## Example\n- Agent view fixture.'))
                page.goto(base + 'changelog/?view=agent')
                page.wait_for_selector('#v9-9-9')
                assert page.locator('#v9-9-9').evaluate('e => e.open')
                assert not page.locator('.changelog-entry:not([open])').count()
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown()
    print('PASS: Agent entry/toggle, visible text, no pins or smooth scroll, FAQ restoration, docs anchors, live releases, desktop/mobile.')


if __name__ == '__main__':
    main()
