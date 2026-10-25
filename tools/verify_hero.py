"""Browser proof for image-plane parallax. Run: python -m tools.verify_hero."""
from functools import partial
from http.server import ThreadingHTTPServer
from io import BytesIO
import threading

from PIL import Image
from playwright.sync_api import sync_playwright

from tools.verify_site import Handler, ROOT, check_links


SENSOR = """(() => {
  window.tiltPermission = 'granted';
  window.tiltAttempts = [];
  window.lastInput = null;
  for (const type of ['pointerdown', 'pointerup', 'click'])
    document.addEventListener(type, () => { window.lastInput = type; }, true);
  DeviceOrientationEvent.requestPermission = async () => {
    const active = navigator.userActivation.isActive;
    tiltAttempts.push({active, event: window.lastInput});
    if (!active || tiltPermission === 'error') throw new Error('User activation required');
    return tiltPermission;
  };
  window.sendTilt = (beta, gamma) => window.dispatchEvent(
    new DeviceOrientationEvent('deviceorientation', {beta, gamma}));
})();"""


def target(page):
    return page.locator('.art').evaluate("e => ['--mx', '--my'].map(k => Number(e.style.getPropertyValue(k)))")


def check_no_glow(page):
    assert page.locator('.art').evaluate("e => getComputedStyle(e, '::before').opacity") == '0'
    assert page.locator('.art canvas').count() == 0


def planes(page):
    values = page.locator('.scene-layer').evaluate_all("""nodes => nodes.map(n => {
      const m = new DOMMatrix(getComputedStyle(n).transform);
      return {x:m.e, y:m.f, a:m.a, b:m.b, c:m.c, d:m.d,
        scroll:getComputedStyle(n.querySelector('img')).translate};
    })""")
    assert len(values) == 4
    for m in values:
        assert m['a'] == m['d'] == 1.08 and m['b'] == m['c'] == 0, values
    return values


def main():
    print('Internal links/assets:', check_links())
    for theme in ('light', 'dark'):
        for part in ('sky', 'room', 'subject', 'plants'):
            image = Image.open(ROOT / 'assets/hero-layers' / f'{theme}-{part}.webp')
            assert image.size == (1672, 941)
            if part != 'sky':
                assert image.mode == 'RGBA' and image.getchannel('A').getextrema()[0] == 0
                assert image.getchannel('A').getpixel((1200, 200)) == 0
    print('PASS: aligned full-frame planes with actual transparent alpha')
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shots = ROOT / '.shots' / 'tilt'
    shots.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            # Model the browser's actual transient-activation requirement.
            for permission in ('granted', 'denied', 'error'):
                context = browser.new_context(viewport={'width': 412, 'height': 915},
                                              is_mobile=True, has_touch=True)
                context.add_init_script(SENSOR)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                page.wait_for_function("document.querySelector('.art').classList.contains('layered')")
                assert page.get_by_role('button', name='tilt', exact=False).count() == 0
                assert page.evaluate('tiltAttempts') == []
                page.evaluate('(v) => { tiltPermission = v; }', permission)
                page.touchscreen.tap(200, 300)
                page.wait_for_timeout(100)
                assert page.evaluate('tiltAttempts') == [{'active': True, 'event': 'click'}]
                if permission == 'granted':
                    page.evaluate('sendTilt(null, null); sendTilt(45, 0); sendTilt(55, 10)')
                    assert target(page) == [0.5, 0.5]
                    page.wait_for_timeout(550)
                    pose = planes(page)
                    assert len({round(m['x'], 2) for m in pose}) == 4, pose
                    check_no_glow(page)
                    page.evaluate('sendTilt(100, 80)')
                    assert target(page) == [1, 1]
                    # Real touch scrolling must work across the moving scene.
                    cdp = context.new_cdp_session(page)
                    cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 200, 'y': 340}]})
                    for y in (300, 250, 200, 150):
                        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': 200, 'y': y}]})
                        page.wait_for_timeout(30)
                    cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
                    page.wait_for_timeout(1600)
                    assert page.evaluate('scrollY') > 0 and target(page) == [1, 1]
                    pose = planes(page)
                    assert len({m['scroll'] for m in pose}) == 4, pose
                    page.screenshot(path=str(shots / 'mobile-layered-max-scroll.png'))
                    page.emulate_media(reduced_motion='reduce')
                    page.wait_for_timeout(100)
                    assert not page.locator('.art').evaluate("e => e.classList.contains('layered')")
                    assert target(page) == [0, 0]
                    assert page.locator('.scene-subject').evaluate('e => getComputedStyle(e).transform') == 'none'
                    page.evaluate('sendTilt(75, 20)')
                    assert target(page) == [0, 0]
                    page.emulate_media(reduced_motion='no-preference')
                    page.wait_for_function("document.querySelector('.art').classList.contains('layered')")
                else:
                    page.evaluate('sendTilt(45, 0); sendTilt(55, 10)')
                    assert target(page) == [0, 0]
                    # A blocked/unavailable sensor must not disable the touch fallback.
                    page.touchscreen.tap(300, 250)
                    assert target(page) != [0, 0]
                    assert len(page.evaluate('tiltAttempts')) == 1
                context.close()
            print('PASS: activated tap permission, granted/denied/error, usable touch fallback, scroll, reduced motion')

            # Native browser-generated sensor events, rather than dispatchEvent mocks.
            context = browser.new_context(viewport={'width': 412, 'height': 915},
                                          is_mobile=True, has_touch=True, device_scale_factor=2.625)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
            page.wait_for_function("document.querySelector('.art').classList.contains('layered')")
            page.evaluate("window.trustedTilt = false; addEventListener('deviceorientation', e => { trustedTilt = e.isTrusted; })")
            cdp = context.new_cdp_session(page)
            cdp.send('DeviceOrientation.setDeviceOrientationOverride', {'alpha': 0, 'beta': 45, 'gamma': 0})
            page.wait_for_function("document.querySelector('.art').classList.contains('tilting')")
            cdp.send('DeviceOrientation.setDeviceOrientationOverride', {'alpha': 0, 'beta': 60, 'gamma': 15})
            page.wait_for_function("document.querySelector('.art').style.getPropertyValue('--mx') === '0.750'")
            page.wait_for_timeout(400)
            assert page.evaluate('trustedTilt')
            pose = planes(page)
            assert abs(pose[-1]['x']) > abs(pose[0]['x']) + 6, pose
            page.screenshot(path=str(shots / 'pixel7-native-tilt.png'))
            check_no_glow(page)
            page.evaluate("Object.defineProperty(screen.orientation, 'angle', {configurable:true, value:90})")
            cdp.send('DeviceOrientation.setDeviceOrientationOverride', {'alpha': 0, 'beta': 61, 'gamma': 15})
            page.wait_for_function("document.querySelector('.art').style.getPropertyValue('--mx') === '0.000'")
            cdp.send('DeviceOrientation.setDeviceOrientationOverride', {'alpha': 0, 'beta': 71, 'gamma': 15})
            page.wait_for_function("document.querySelector('.art').style.getPropertyValue('--mx') === '0.500'")
            assert target(page)[1] == 0
            context.close()
            print('PASS: native sensor pipeline at Pixel 7 dimensions, distinct rigid-plane movement, rotation, no canvas/warping')

            for fallback in (False, True):
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme='light')
                if fallback:
                    context.route('**/hero-layers/**', lambda route: route.fulfill(status=404, body='missing'))
                themed = context.new_page()
                themed.on('pageerror', lambda error: errors.append(str(error)))
                themed.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                themed.wait_for_timeout(500)
                colors = {}
                for theme in ('light', 'dark', 'light', 'dark'):
                    if themed.locator('[data-theme-toggle]').get_attribute('aria-pressed') != str(theme == 'dark').lower():
                        themed.locator('[data-theme-toggle]').click()
                    expected = 'hero-light.webp' if theme == 'light' else 'hero.webp'
                    themed.wait_for_function("name => document.querySelector('.scene-flat img').currentSrc.endsWith('/' + name)", arg=expected)
                    themed.wait_for_function("name => document.querySelector('.brand img').currentSrc.endsWith('/avatar-' + name + '.webp')", arg=theme)
                    if not fallback:
                        themed.wait_for_function("document.querySelector('.art').classList.contains('layered')")
                        themed.wait_for_function("t => [...document.querySelectorAll('.scene-layer img')].every(n => n.currentSrc.includes('/' + t + '-'))", arg=theme)
                    else:
                        assert not themed.locator('.art').evaluate("e => e.classList.contains('layered')")
                    themed.mouse.move(1200, 230)
                    themed.wait_for_timeout(850)
                    picture = Image.open(BytesIO(themed.screenshot(path=str(shots / f'theme-{theme}-{"fallback" if fallback else "layers"}.png')))).convert('RGB')
                    colors[theme] = sum(picture.getpixel((1080, 200))) / 3
                    assert themed.locator('.theme-btn .i-sun').is_visible() == (theme == 'dark')
                    assert themed.locator('.art canvas').count() == 0
                    if not fallback:
                        planes(themed)
                    themed.evaluate('scrollTo(0, 180)')
                    themed.wait_for_timeout(400)
                    themed.screenshot(path=str(shots / f'scroll-{theme}-{"fallback" if fallback else "layers"}.png'))
                    themed.evaluate('scrollTo(0, 0)')
                    themed.wait_for_timeout(100)
                assert colors['light'] > colors['dark'] + 70, colors
                themed.locator('[data-theme-toggle]').click()
                themed.wait_for_function("document.getAnimations().some(a => a.animationName === 'hero-theme-in')")
                themed.screenshot(path=str(shots / f'theme-transition-{"fallback" if fallback else "layers"}.png'))
                themed.wait_for_timeout(850)
                themed.evaluate("() => { const b = document.querySelector('[data-theme-toggle]'); b.click(); b.click(); b.click(); }")
                themed.wait_for_function("document.documentElement.dataset.theme === 'dark'")
                themed.wait_for_timeout(1000)
                themed.reload()
                themed.wait_for_function("document.querySelector('.scene-flat img').currentSrc.endsWith('/hero.webp')")
                context.close()
            print('PASS: both layer sets, photo fallback on asset failure, theme transitions, rapid toggles, saved theme, rigid geometry')
            for theme in ('light', 'dark'):
                context = browser.new_context(java_script_enabled=False, color_scheme=theme)
                page = context.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                expected = 'hero-light.webp' if theme == 'light' else 'hero.webp'
                assert page.locator('.scene-flat img').evaluate('(e) => e.currentSrc').endswith('/' + expected)
                assert page.locator('.scene-flat').evaluate('e => getComputedStyle(e).opacity') == '1'
                assert page.locator('.scene-layers').evaluate('e => getComputedStyle(e).opacity') == '0'
                context.close()
            print('PASS: complete neutral scene without JavaScript')
            for width, height in ((320, 568), (360, 640), (390, 844), (430, 932),
                                  (768, 1024), (844, 390), (1024, 768), (1440, 1000), (1920, 1080)):
                context = browser.new_context(viewport={'width': width, 'height': height},
                                              is_mobile=width <= 1024, has_touch=width <= 1024,
                                              reduced_motion='reduce', color_scheme='light')
                phone = context.new_page()
                phone.on('pageerror', lambda error: errors.append(str(error)))
                phone.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/?auto')
                for theme in ('light', 'dark'):
                    if theme == 'dark':
                        phone.locator('[data-theme-toggle]').click()
                    assert phone.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), (width, height, theme)
                    assert phone.evaluate('innerWidth') == width, (width, height, 'mobile viewport expanded by overflow')
                    assert phone.locator('#nav').evaluate("e => getComputedStyle(e).backgroundColor") == 'rgba(0, 0, 0, 0)'
                    boxes = phone.locator('.nav-row > a, .nav-row > button').evaluate_all("""nodes => nodes
                        .filter(n => getComputedStyle(n).display !== 'none')
                        .map(n => ({name:n.textContent.trim(), ...n.getBoundingClientRect().toJSON()}))""")
                    for i, box in enumerate(boxes):
                        assert box['x'] >= 0 and box['right'] <= width, (width, box)
                        if width <= 900:
                            assert box['height'] >= 44, (width, box)
                        for other in boxes[i + 1:]:
                            assert box['right'] <= other['left'] or other['right'] <= box['left'], (width, box, other)
                    assert phone.get_by_role('button', name='tilt', exact=False).count() == 0
                    if width <= 1080:
                        phone.locator('#menu-btn').click()
                        assert phone.locator('#menu-btn').get_attribute('aria-expanded') == 'true'
                        assert phone.locator('#nav-links a:visible').count() == 9
                        panel = phone.locator('#nav-links').bounding_box()
                        assert panel['x'] >= 0 and panel['x'] + panel['width'] <= width
                        assert panel['y'] + panel['height'] <= height - 34, (width, height, panel)
                        phone.locator('#nav-links a[href="#faq"]').scroll_into_view_if_needed()
                        phone.locator('#nav-links a[href="#faq"]').click()
                        assert phone.locator('#menu-btn').get_attribute('aria-expanded') == 'false'
                        phone.evaluate('scrollTo(0, 0)')
                        phone.wait_for_timeout(50)
                        phone.locator('#menu-btn').click()
                        phone.keyboard.press('Escape')
                        assert phone.locator('#menu-btn').get_attribute('aria-expanded') == 'false'
                        assert phone.locator('#menu-btn').evaluate('e => e === document.activeElement')
                    phone.screenshot(path=str(shots / f'responsive-{width}-{height}-{theme}.png'))
                context.close()
            print('PASS: 18 light/dark layouts from 320 to 1920px, transparent header, touch targets, no overlap/overflow, mobile menu and keyboard Escape')
            for route, brand in (('docs/', '.sidebar-brand'), ('changelog/', '.tn-brand')):
                for width in (320, 390, 844, 1440):
                    context = browser.new_context(viewport={'width': width, 'height': 844},
                                                  is_mobile=width < 1000, has_touch=width < 1000,
                                                  reduced_motion='reduce', color_scheme='light')
                    context.route('https://api.github.com/**', lambda route: route.fulfill(status=403, body='{}'))
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/{route}')
                    for theme in ('light', 'dark'):
                        if theme == 'dark':
                            page.locator('[data-theme-toggle]').click()
                        page.wait_for_function("args => document.querySelector(args.brand + ' img').currentSrc.endsWith('/avatar-' + args.theme + '.webp')", arg={'brand': brand, 'theme': theme})
                        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), (route, width, theme)
                        assert page.evaluate('innerWidth') == width, (route, width, theme)
                        assert page.locator(brand + ' img').evaluate('e => e.getBoundingClientRect().width') == 32
                        header = '.docs-topnav' if route == 'docs/' else '.topnav'
                        controls = page.locator(header + ' > a, ' + header + ' > button').evaluate_all("""nodes => nodes
                            .filter(n => getComputedStyle(n).display !== 'none')
                            .map(n => n.getBoundingClientRect().toJSON())""")
                        for i, box in enumerate(controls):
                            assert box['x'] >= 0 and box['right'] <= width, (route, width, box)
                            if width <= 620:
                                assert box['height'] >= 44, (route, width, box)
                            for other in controls[i + 1:]:
                                assert box['right'] <= other['left'] or other['right'] <= box['left'], (route, width, box, other)
                    context.close()
            print('PASS: Docs and Changelog theme-aware portraits and layouts at 320, 390, 844 and 1440px')
            context = browser.new_context(color_scheme='light')
            context.add_init_script('document.startViewTransition = undefined')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
            page.locator('[data-theme-toggle]').click()
            page.wait_for_function("document.querySelector('.art img').currentSrc.endsWith('/hero.webp')")
            page.wait_for_timeout(800)
            assert page.locator('[data-theme-toggle]').get_attribute('aria-pressed') == 'true'
            context.close()
            print('PASS: theme transition fallback without native View Transitions')
            assert not errors, errors
            browser.close()
            print('PASS: desktop pointer preserved; no mobile JavaScript errors')
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
