"""Prove the theme's printing pass, reverse pass, interruption and static fallbacks."""
from functools import partial
from http.server import ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright

from verify_site import ROOT, Handler


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/NovaCode/'
    shots = ROOT / '.shots/theme'
    shots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            for path in ('', 'docs/', 'changelog/'):
                context = browser.new_context(viewport={'width': 1200, 'height': 900}, color_scheme='light')
                context.route('https://api.github.com/**', lambda r: r.fulfill(status=403, body='{}'))
                context.add_init_script("""(() => {
                    const native = document.startViewTransition.bind(document);
                    window.__themeReady = false;
                    document.startViewTransition = cb => {
                        window.__themeReady = false;
                        const t = native(cb);
                        window.__themeTransition = t;
                        t.ready.then(() => window.__themeReady = true).catch(() => {});
                        return t;
                    };
                })();""")
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(base + path)
                page.wait_for_function("!document.querySelector('#boot')")
                for theme, direction in (('dark', 'down'), ('light', 'up')):
                    page.locator('[data-theme-toggle]').first.click()
                    page.wait_for_function('window.__themeReady')
                    assert page.evaluate('document.documentElement.dataset.theme') == theme
                    assert page.evaluate("getComputedStyle(document.documentElement,'::view-transition-new(root)').animationName") == 'theme-sheet-' + direction
                    assert page.locator('.theme-scan').count() == 1
                    page.evaluate("document.getAnimations().filter(a => a.animationName?.startsWith('theme-')).forEach(a => {a.pause(); a.currentTime = 310;})")
                    page.screenshot(path=str(shots / f'{path.strip("/") or "home"}-{theme}-mid.png'))
                    page.evaluate("document.getAnimations().filter(a => a.animationName?.startsWith('theme-')).forEach(a => a.play())")
                    page.evaluate('window.__themeTransition.finished')
                    assert not page.locator('.theme-scan').count()
                page.evaluate("document.querySelector('[data-theme-toggle]').click(); document.querySelector('[data-theme-toggle]').click(); document.querySelector('[data-theme-toggle]').click()")
                page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
                page.evaluate('window.__themeTransition.finished')
                assert not page.locator('.theme-scan').count()
                page.reload()
                assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
                assert not errors, errors
                context.close()
            for mode in ('reduced', 'agent', 'fallback'):
                context = browser.new_context(reduced_motion='reduce' if mode == 'reduced' else 'no-preference', color_scheme='light')
                if mode == 'fallback':
                    context.add_init_script('document.startViewTransition = undefined')
                page = context.new_page()
                page.goto(base + ('?view=agent' if mode == 'agent' else ''))
                page.wait_for_function("!document.querySelector('#boot')")
                page.locator('[data-theme-toggle]').first.click()
                assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
                assert not page.locator('.theme-scan').count()
                if mode == 'fallback':
                    page.wait_for_function("!document.documentElement.classList.contains('theme-fade')")
                context.close()
            browser.close()
    finally:
        server.shutdown()
    print('PASS: theme pass/reverse on all pages, rapid toggles, persistence, reduced motion, Agent mode and no-View-Transition fallback.')


if __name__ == '__main__':
    main()
