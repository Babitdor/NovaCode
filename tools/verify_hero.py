"""Browser proof for the hero's signature element, the measure. Run: python -m tools.verify_hero.

The hero claims a width in pixels, so the check recomputes that width from the
rendered type instead of trusting the page's own number.
"""
from functools import partial
from http.server import ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright

from tools.verify_site import Handler, ROOT

# Independent measurement: per-line left/right bounds of the claim's text.
PROBE = """(() => {
  const m = document.querySelector('[data-measure]');
  const h1 = m.querySelector('h1');
  const range = document.createRange();
  range.selectNodeContents(h1);
  const lines = new Map();
  for (const r of range.getClientRects()) {
    if (r.width < 1) continue;
    const key = Math.round(r.top);
    const box = lines.get(key) || {left: r.left, right: r.right};
    lines.set(key, {left: Math.min(box.left, r.left), right: Math.max(box.right, r.right)});
  }
  const widths = [...lines.values()].map(b => b.right - b.left);
  const line = getComputedStyle(m.querySelector('.measure-line'));
  const cs = getComputedStyle(h1);
  return {
    lines: widths.length,
    widest: Math.max(...widths),
    css: parseFloat(m.style.getPropertyValue('--w')),
    number: m.querySelector('.measure-num').textContent.trim(),
    ruleScale: new DOMMatrix(line.transform).a,
    ruleWidth: m.querySelector('.measure-line').getBoundingClientRect().width,
    family: cs.fontFamily,
    stretch: cs.fontStretch,
    overflowX: document.documentElement.scrollWidth - window.innerWidth,
    boot: (() => { const b = document.querySelector('#boot');
      return !!b && getComputedStyle(b).display !== 'none'; })(),
    hidden: [...document.querySelectorAll('[data-fade]')]
      .filter(e => e.getBoundingClientRect().top < window.innerHeight
        && getComputedStyle(e).opacity === '0').length,
  };
})()"""


def hero(page, label):
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_timeout(900)
    m = page.evaluate(PROBE)
    assert not m['boot'], f'{label}: boot overlay still covering the page'
    assert 'Archivo' in m['family'], f'{label}: display face not loaded, {m["family"]}'
    assert abs(float(m['stretch'].rstrip('%')) - 112) < 1, f'{label}: stretch {m["stretch"]}'
    assert m['lines'] >= 2, f'{label}: claim collapsed to {m["lines"]} line(s)'
    assert abs(m['widest'] - m['css']) <= 1.5, f'{label}: rule {m["css"]} vs widest line {m["widest"]}'
    assert m['number'] == f'{round(m["css"])} px', f'{label}: printed {m["number"]!r}'
    assert abs(m['ruleWidth'] - m['css']) <= 1.5, f'{label}: rule drew {m["ruleWidth"]}'
    assert m['overflowX'] <= 0, f'{label}: {m["overflowX"]}px of horizontal overflow'
    assert m['hidden'] == 0, f'{label}: {m["hidden"]} element(s) left invisible'
    print(f'  {label}: {m["lines"]} lines, widest {m["widest"]:.1f}, rule {m["number"]}')
    return m


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shots = ROOT / '.shots' / 'hero'
    shots.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            for label, options in (
                ('desk-light', {'viewport': {'width': 1440, 'height': 900}, 'color_scheme': 'light'}),
                ('desk-dark', {'viewport': {'width': 1440, 'height': 900}, 'color_scheme': 'dark'}),
                ('phone', {'viewport': {'width': 390, 'height': 844}, 'is_mobile': True, 'has_touch': True}),
                ('reduce', {'viewport': {'width': 1440, 'height': 900}, 'reduced_motion': 'reduce'}),
            ):
                context = browser.new_context(**options)
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                m = hero(page, label)
                if label == 'reduce':
                    assert m['ruleScale'] == 1, 'reduced motion: rule should be drawn statically'
                page.screenshot(path=str(shots / f'{label}-hero.png'))
                context.close()
            # The saved choice must also restyle the browser chrome it paints.
            context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='light')
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
            page.wait_for_timeout(600)
            light = page.get_attribute('meta[name="theme-color"]', 'content')
            page.get_by_role('button', name='Dark theme').click()
            page.wait_for_timeout(700)
            dark = page.get_attribute('meta[name="theme-color"]', 'content')
            assert page.evaluate("document.documentElement.dataset.theme") == 'dark'
            assert (light, dark) == ('#EFEDE7', '#121316'), (light, dark)
            page.reload()
            page.wait_for_timeout(600)
            assert page.evaluate("document.documentElement.dataset.theme") == 'dark', 'choice not remembered'
            print(f'  theme: {light} -> {dark}, remembered across reload')
            context.close()
            browser.close()
    finally:
        server.shutdown()
    assert not errors, errors
    print('PASS: measure, overflow, reduced motion, theme chrome')


if __name__ == '__main__':
    main()
