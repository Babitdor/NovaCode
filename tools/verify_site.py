"""Browser regressions for the static GitHub Pages site. Requires Python Playwright.

Run: python tools/verify_site.py
Tests the shipped files at /NovaCode/, without a bundler or a dev server.
"""
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
from pathlib import Path
import threading
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / '.shots' / 'repair'


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/NovaCode/'):
            self.path = self.path[len('/NovaCode'):]
        super().do_GET()

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        if code != 404:
            return super().send_error(code, message, explain)
        body = (ROOT / '404.html').read_bytes()
        self.send_response(404)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        for attr in ('href', 'src'):
            if attrs.get(attr):
                self.urls.append(attrs[attr])


def check_links():
    pages = {path: Links() for path in (ROOT / 'index.html', ROOT / 'docs/index.html', ROOT / 'changelog/index.html')}
    for path, parser in pages.items():
        parser.feed(path.read_text(encoding='utf-8'))
    checked = 0
    for path, parser in pages.items():
        for url in parser.urls:
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc:
                continue
            target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            if target.is_dir():
                target /= 'index.html'
            assert target.exists(), f'Broken link: {path.name} -> {url}'
            if parsed.fragment and target in pages:
                assert unquote(parsed.fragment) in pages[target].ids, f'Missing fragment: {path} -> {url}'
            checked += 1
    return checked


def main():
    archive_count = (ROOT / 'changelog/index.html').read_text(encoding='utf-8').count('class="changelog-entry"')
    SHOTS.mkdir(parents=True, exist_ok=True)
    results = {'internal_links_and_assets': check_links(), 'checks': [], 'screenshots': [], 'accessibility': {}}
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/NovaCode/'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            if '--live' in sys.argv:
                page.goto(base + 'changelog/')
                page.wait_for_function("!document.querySelector('#release-status').textContent.startsWith('Checking')", timeout=60000)
                message = page.locator('#release-status').inner_text()
                print(message, flush=True)
                print('Release count:', page.locator('.changelog-entry').count(), flush=True)
                assert message == 'Up to date with the repository.', message
                assert not errors, errors
                browser.close()
                return
            # Default network failure exercises the embedded archive without external dependencies.
            context.route('https://api.github.com/**', lambda route: route.fulfill(status=403, body='{}'))
            page.goto(base + 'docs/')
            page.locator('#page-overview').wait_for(state='visible')
            topics = page.locator('.sidebar [data-page]').evaluate_all('(links) => links.map(a => a.dataset.page)')
            for topic in topics:
                link = page.locator(f'.sidebar [data-page="{topic}"]')
                if not link.is_visible():
                    link.locator('xpath=ancestor::div[contains(@class,"sidebar-group")]').locator('.sidebar-group-title').click()
                link.click()
                assert page.locator('.page-section.active').get_attribute('id') == 'page-' + topic
                assert page.url.endswith('#page-' + topic)
            results['checks'].append(f'All {len(topics)} documentation topics navigate')
            page.goto(base + 'docs/#installation')
            assert page.locator('#page-installation').is_visible()
            page.locator('.sidebar [data-page="cli"]').locator('xpath=ancestor::div[contains(@class,"sidebar-group")]').locator('.sidebar-group-title').click()
            page.locator('.sidebar [data-page="cli"]').click()
            page.go_back()
            assert page.locator('#page-installation').is_visible()
            page.go_forward()
            assert page.locator('#page-cli').is_visible()
            page.goto(base + 'docs/')
            page.locator('.sidebar [data-page="installation"]').click()
            page.go_back()
            assert page.locator('#page-overview').is_visible()
            page.goto(base + 'docs/#%invalid')
            assert page.locator('#page-overview').is_visible()
            results['checks'].append('Documentation bookmarks, empty/malformed hashes, back and forward')
            page.goto(base + 'changelog/#v1-0-30')
            assert page.locator('#v1-0-30').get_attribute('open') is not None
            page.locator('#v1-0-30 summary').click()
            assert page.locator('#v1-0-30').get_attribute('open') is None
            page.locator('#v1-0-30 summary').focus()
            page.keyboard.press('Enter')
            assert page.locator('#v1-0-30').get_attribute('open') is not None
            page.goto(base + 'changelog/#v1.0.54')
            assert page.locator('#v1-0-54').get_attribute('open') is not None
            page.wait_for_function("document.querySelector('#release-status').textContent.includes('saved release archive')")
            assert page.locator('.changelog-entry').count() == archive_count
            results['checks'].append('Release mouse/keyboard controls, both permalink formats, API failure fallback')

            # Use GitHub-shaped responses to prove new releases and edited notes load dynamically.
            context.unroute('https://api.github.com/**')
            listing = page.locator('.changelog-entry').evaluate_all('entries => entries.map(e => ({name: "CHANGELOG-v" + e.dataset.version + ".md", sha:e.dataset.sha, type:"file"}))')
            listing.append({'name': 'CHANGELOG-v1.0.100.md', 'sha': 'new-test-release', 'type': 'file'})
            next(entry for entry in listing if entry['name'] == 'CHANGELOG-v1.0.54.md')['sha'] = 'edited-test-release'
            context.route('https://api.github.com/**', lambda route: route.fulfill(json=listing, headers={'Access-Control-Allow-Origin': '*'}))
            context.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(headers={'Access-Control-Allow-Origin': '*'}, body='# NovaCode v1.0.100\n\n## Fixed\n- A wrapped `regression`\n  continuation <img src=x onerror=alert(1)>\n- **Second item**\n'))
            page.goto(base + 'changelog/#v1-0-100')
            page.reload()
            page.wait_for_function("document.querySelector('#release-status').textContent === 'Up to date with the repository.'")
            assert page.locator('.changelog-entry').count() == archive_count + 1
            assert page.locator('.changelog-entry').first.get_attribute('data-version') == '1.0.100'
            assert page.locator('#v1-0-100').get_attribute('open') is not None
            assert page.locator('#v1-0-100 li').count() == 2
            assert page.locator('#v1-0-100 img').count() == 0
            assert 'continuation <img' in page.locator('#v1-0-100 li').first.inner_text()
            assert page.locator('#v1-0-54').get_attribute('data-sha') == 'edited-test-release'
            results['checks'].append('New and edited live releases, numeric sorting, wrapped bullets, HTML escaping')
            context.unroute('https://raw.githubusercontent.com/**')
            context.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(status=503, body='unavailable'))
            page.reload()
            page.wait_for_function("document.querySelector('#release-status').textContent.includes('Some updates could not load')")
            assert page.locator('.changelog-entry').count() == archive_count
            results['checks'].append('Partial update failures retain every saved release')
            context.unroute('https://api.github.com/**')
            context.route('https://api.github.com/**', lambda route: route.fulfill(status=403, body='{}'))

            # Two QA passes: normal desktop and phone with reduced motion and dark theme.
            for mode, viewport, theme, motion in [
                ('desktop', {'width': 1440, 'height': 1000}, 'light', 'no-preference'),
                ('mobile', {'width': 390, 'height': 844}, 'dark', 'reduce'),
            ]:
                page.set_viewport_size(viewport)
                page.emulate_media(color_scheme=theme, reduced_motion=motion)
                for name, url in [('home', ''), ('docs', 'docs/#page-installation'), ('changelog', 'changelog/#v1-0-54')]:
                    page.goto(base + url)
                    page.wait_for_timeout(1000)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow: {name} {mode}'
                    axe = ROOT / 'node_modules/axe-core/axe.min.js'
                    if axe.exists():
                        page.add_script_tag(path=str(axe))
                        violations = page.evaluate("async () => (await axe.run({runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa']}})).violations.map(v => ({id:v.id, impact:v.impact, nodes:v.nodes.map(n => n.target)}))")
                        results['accessibility'][mode + '-' + name] = violations
                        assert not [v for v in violations if v['impact'] in ('serious', 'critical')], violations
                    if name == 'changelog':
                        page.evaluate('scrollTo(0,0)')
                    output = SHOTS / f'{mode}-{name}.png'
                    page.screenshot(path=str(output), full_page=False)
                    results['screenshots'].append(output.name)
                if mode == 'mobile':
                    page.goto(base + 'docs/')
                    page.locator('#menu-btn').click()
                    assert page.locator('#sidebar').evaluate("e => e.classList.contains('open')")
                    page.keyboard.press('Escape')
                    assert page.locator('#menu-btn').get_attribute('aria-expanded') == 'false'
            results['checks'].append('Desktop and mobile layouts, reduced motion, mobile drawer Escape')
            nojs = browser.new_context(java_script_enabled=False)
            off = nojs.new_page()
            off.goto(base + 'docs/#page-installation')
            assert off.locator('#page-installation').is_visible()
            off.goto(base + 'changelog/')
            off.locator('.entry-toggle').first.click()
            assert off.locator('.release-content').first.is_visible()
            nojs.close()
            results['checks'].append('Documentation and release archive without JavaScript')
            response = page.goto(base + 'missing-page')
            assert response.status == 404
            assert page.locator('h1').inner_text() == 'This page is missing.'
            page.locator('nav a').first.click()
            assert page.url == base
            results['checks'].append('Designed 404 status and recovery navigation')
            assert not errors, errors
            results['console_errors'] = errors
            browser.close()
    finally:
        server.shutdown()
    (SHOTS / 'report.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
