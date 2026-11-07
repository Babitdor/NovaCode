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


def parallax(browser, base, errors):
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='light')
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(base)
    page.wait_for_function("document.querySelector('.art').matches('.layered.art-ready')", timeout=15000)
    assert page.locator('.scene-layer').count() == 4
    assert float(page.locator('.plate').evaluate("e => e.style.getPropertyValue('--ps')")) == 0
    assert page.locator('.scene-layer img').evaluate_all(
        "es => es.every(e => e.naturalWidth > 0 && e.currentSrc.includes('-ink'))"
    )
    art = page.locator('.art')
    subject_source = page.locator('.scene-subject img').evaluate('e => e.currentSrc')
    box = art.bounding_box()
    page.mouse.move(box['x'] + box['width'] * .8, box['y'] + box['height'] * .3)
    page.wait_for_timeout(600)
    shifts = page.locator('.scene-layer').evaluate_all(
        "es => es.map(e => new DOMMatrix(getComputedStyle(e).transform).m41)"
    )
    assert len({round(shift, 1) for shift in shifts}) == 4, shifts
    pointer = art.evaluate("e => e.style.getPropertyValue('--mx')")
    art.dispatch_event('pointermove', {'pointerType': 'touch', 'clientX': 0, 'clientY': 0})
    assert art.evaluate("e => e.style.getPropertyValue('--mx')") == pointer
    page.evaluate('window.scrollTo(0, 350)')
    page.wait_for_function("parseFloat(document.querySelector('.plate').style.getPropertyValue('--ps')) > .1")
    page.get_by_role('button', name='Dark theme').click()
    page.wait_for_function("""() => document.documentElement.dataset.theme === 'dark'
        && document.querySelector('.art').classList.contains('layered')
        && [...document.querySelectorAll('.scene-layer img')].every(i =>
          (i.closest('.scene-subject') || i.currentSrc.includes('/dark-')) && i.naturalWidth > 0)""", timeout=15000)
    assert page.locator('.scene-subject img').evaluate('e => e.currentSrc') == subject_source
    context.close()

    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='light')
    context.add_init_script("""(() => {
      const decode = HTMLImageElement.prototype.decode;
      Object.defineProperty(HTMLImageElement.prototype, 'decode', {
        configurable: true, get() { return this.closest('.art') ? undefined : decode; }
      });
    })()""")
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(base)
    page.wait_for_function("document.querySelector('.art').matches('.layered.art-ready')", timeout=15000)
    context.close()

    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='light')
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/light-room-ink*.webp', lambda route: route.abort())
    page.goto(base)
    page.wait_for_function("""() => document.querySelector('.art').classList.contains('art-ready')
        && document.querySelector('.scene-flat img').naturalWidth > 0""", timeout=15000)
    page.wait_for_timeout(500)
    assert not page.locator('.art').evaluate("e => e.classList.contains('layered')")
    assert float(page.locator('.scene-flat').evaluate('e => getComputedStyle(e).opacity')) > .9
    context.close()
    print('  parallax: aligned start, pointer and scroll depth, touch, theme and image fallbacks')


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
                ('phone-small', {'viewport': {'width': 320, 'height': 740}, 'is_mobile': True, 'has_touch': True}),
                ('reduce', {'viewport': {'width': 1440, 'height': 900}, 'reduced_motion': 'reduce'}),
            ):
                context = browser.new_context(**options)
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                m = hero(page, label)
                if options.get('has_touch'):
                    for control in ('[data-theme-toggle]', '#menu-btn'):
                        box = page.locator(control).bounding_box()
                        assert box['width'] >= 44 and box['height'] >= 44, (control, box)
                if label == 'reduce':
                    assert m['ruleScale'] == 1, 'reduced motion: rule should be drawn statically'
                page.screenshot(path=str(shots / f'{label}-hero.png'))
                context.close()
            parallax(browser, f'http://127.0.0.1:{server.server_port}/NovaCode/', errors)
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
