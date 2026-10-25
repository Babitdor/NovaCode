"""Focused phone-tilt regressions. Run: python -m tools.verify_hero."""
from functools import partial
from http.server import ThreadingHTTPServer
from io import BytesIO
import threading

from PIL import Image
from playwright.sync_api import sync_playwright

from tools.verify_site import Handler, ROOT, check_links


SENSOR = """(() => {
  window.tiltPermission = 'granted';
  window.tiltRequests = 0;
  if (!location.search.includes('auto')) DeviceOrientationEvent.requestPermission = async () => {
    window.tiltRequests++;
    if (window.tiltPermission === 'error') throw new Error('blocked');
    return window.tiltPermission;
  };
  else delete DeviceOrientationEvent.requestPermission;
  window.sendTilt = (beta, gamma) => window.dispatchEvent(
    new DeviceOrientationEvent('deviceorientation', {beta, gamma}));
})();"""


def target(page):
    return page.locator('.art').evaluate("e => ['--mx', '--my'].map(k => Number(e.style.getPropertyValue(k)))")


def check_no_glow(page):
    assert page.locator('.art').evaluate("e => getComputedStyle(e, '::before').opacity") == '0'
    canvas = page.locator('.art canvas')
    if canvas.count():
        assert canvas.evaluate("""e => {
          const gl = e.getContext('webgl'), program = gl.getParameter(gl.CURRENT_PROGRAM);
          return gl.getUniform(program, gl.getUniformLocation(program, 'uLight'))[2];
        }""") == 0


def main():
    print('Internal links/assets:', check_links())
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shots = ROOT / '.shots' / 'tilt'
    shots.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            for fallback in (False, True):
                context = browser.new_context(
                    viewport={'width': 390, 'height': 844},
                    is_mobile=True, has_touch=True, device_scale_factor=2)
                context.add_init_script(SENSOR)
                if fallback:
                    context.add_init_script("""const getContext = HTMLCanvasElement.prototype.getContext;
                        HTMLCanvasElement.prototype.getContext = function(type, ...args) {
                          return type === 'webgl' ? null : getContext.call(this, type, ...args);
                        };""")
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                page.wait_for_timeout(2100)
                assert page.locator('.art').evaluate("e => e.classList.contains('gl')") != fallback
                assert page.get_by_role('button', name='tilt', exact=False).count() == 0
                assert page.evaluate('tiltRequests') == 0
                page.evaluate("tiltPermission = 'denied'")
                page.touchscreen.tap(200, 300)
                page.wait_for_timeout(100)
                assert page.evaluate('tiltRequests') == 1
                page.evaluate('sendTilt(45, 15)')
                assert target(page) == [0, 0]
                page.reload()
                page.evaluate("tiltPermission = 'error'")
                page.touchscreen.tap(200, 300)
                page.wait_for_timeout(100)
                page.evaluate('sendTilt(45, 15)')
                assert target(page) == [0, 0]
                page.reload()
                page.touchscreen.tap(200, 300)
                page.wait_for_timeout(100)
                page.evaluate('sendTilt(null, null); sendTilt(45, 0)')
                assert target(page) == [0, 0]  # Current holding position is neutral.
                page.evaluate('sendTilt(55, 10)')
                assert target(page) == [0.5, 0.5]
                page.wait_for_timeout(1100)
                check_no_glow(page)
                page.screenshot(path=str(shots / f'mobile-{"css" if fallback else "webgl"}.png'))
                page.evaluate('sendTilt(100, 80)')
                assert target(page) == [1, 1]
                # The browser scroll gesture must still scroll across the photo.
                cdp = context.new_cdp_session(page)
                cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 200, 'y': 340}]})
                for y in (300, 250, 200, 150):
                    cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': 200, 'y': y}]})
                    page.wait_for_timeout(30)
                cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
                page.wait_for_timeout(1600)
                assert page.evaluate('scrollY') > 0
                assert target(page) == [1, 1]  # Touch release cannot reset the sensor.
                page.evaluate('scrollTo(0, 0)')
                page.wait_for_timeout(400)
                # Android-style API: orientation events require no permission method.
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/?auto')
                assert page.get_by_role('button', name='tilt', exact=False).count() == 0
                assert page.evaluate('tiltRequests') == 0
                page.evaluate('sendTilt(45, 0); sendTilt(45, -10)')
                assert target(page) == [-0.5, 0]
                page.wait_for_timeout(2100)
                check_no_glow(page)
                page.screenshot(path=str(shots / f'mobile-auto-{"css" if fallback else "webgl"}.png'))
                page.evaluate("Object.defineProperty(screen.orientation, 'angle', {configurable:true, value:90})")
                page.evaluate('sendTilt(45, -10)')
                assert target(page) == [0, 0]  # Rotation recalibrates.
                page.evaluate('sendTilt(55, -10)')
                assert target(page) == [0.5, 0]
                page.emulate_media(reduced_motion='reduce')
                page.wait_for_timeout(100)
                assert page.locator('.hero-tilt').count() == 0
                assert target(page) == [0, 0]
                assert page.locator('.art canvas').count() == 0
                page.evaluate('sendTilt(75, 20)')
                assert target(page) == [0, 0]
                page.emulate_media(reduced_motion='no-preference')
                page.wait_for_timeout(250)
                page.evaluate('sendTilt(45, 0); sendTilt(55, 10)')
                assert target(page) != [0, 0]
                assert page.locator('.art canvas').count() == (0 if fallback else 1)
                context.close()
                print('PASS:', 'CSS fallback' if fallback else 'WebGL',
                      'default tilt without button, no mobile glow, permission denied/error/granted, calibration, clamping, touch scrolling, rotation, reduced motion and resume')
            # No sensor UI on desktop; mouse parallax still responds.
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
            assert page.locator('.hero-tilt').count() == 0
            page.mouse.move(1100, 250)
            assert target(page)[0] > 0
            assert page.locator('.art').evaluate("e => e.classList.contains('lit')")
            page.screenshot(path=str(shots / 'desktop.png'))
            # A narrow desktop browser preview must also suppress the mobile glow.
            page.set_viewport_size({'width': 390, 'height': 844})
            page.mouse.move(300, 200)
            page.wait_for_timeout(2100)
            check_no_glow(page)
            page.close()
            for fallback in (False, True):
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme='light')
                if fallback:
                    context.add_init_script("""const getContext = HTMLCanvasElement.prototype.getContext;
                        HTMLCanvasElement.prototype.getContext = function(type, ...args) {
                          return type === 'webgl' ? null : getContext.call(this, type, ...args);
                        };""")
                themed = context.new_page()
                themed.on('pageerror', lambda error: errors.append(str(error)))
                themed.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                themed.wait_for_timeout(2100)
                colors = {}
                for theme in ('light', 'dark', 'light', 'dark'):
                    if themed.locator('[data-theme-toggle]').get_attribute('aria-pressed') != str(theme == 'dark').lower():
                        themed.locator('[data-theme-toggle]').click()
                    expected = 'hero-light.webp' if theme == 'light' else 'hero.webp'
                    themed.wait_for_function("name => document.querySelector('.art img').currentSrc.endsWith('/' + name)", arg=expected)
                    themed.wait_for_function("name => document.querySelector('.brand img').currentSrc.endsWith('/avatar-' + name + '.webp')", arg=theme)
                    assert themed.locator('.theme-btn .i-sun').is_visible() == (theme == 'dark')
                    assert themed.locator('.theme-btn .i-moon').is_visible() == (theme == 'light')
                    if not fallback:
                        themed.wait_for_function("document.querySelector('.art').classList.contains('gl')")
                        assert themed.locator('.art canvas').count() == 1
                    themed.mouse.move(1200, 230)
                    themed.wait_for_timeout(850)
                    # Compare the rendered sky, including the actual WebGL texture.
                    picture = Image.open(BytesIO(themed.screenshot(path=str(shots / f'theme-{theme}-{"css" if fallback else "webgl"}.png')))).convert('RGB')
                    colors[theme] = sum(picture.getpixel((1080, 200))) / 3
                    text = themed.locator('.brand span').evaluate('e => getComputedStyle(e).color')
                    assert text == ('rgb(23, 23, 26)' if theme == 'light' else 'rgb(236, 236, 239)'), text
                    assert themed.locator('.art picture').count() == 1
                    assert themed.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    themed.evaluate('scrollTo(0, 180)')
                    themed.wait_for_timeout(350)
                    themed.screenshot(path=str(shots / f'scroll-{theme}-{"css" if fallback else "webgl"}.png'))
                    themed.evaluate('scrollTo(0, 0)')
                    themed.wait_for_timeout(350)
                assert colors['light'] > colors['dark'] + 70, colors
                themed.locator('[data-theme-toggle]').click()
                themed.wait_for_function("document.getAnimations().some(a => a.animationName === 'hero-theme-in')")
                assert themed.evaluate("getComputedStyle(document.documentElement, '::view-transition-group(hero-nav)').zIndex") == '2'
                assert themed.evaluate("getComputedStyle(document.documentElement, '::view-transition-group(hero-dock)').zIndex") == '2'
                themed.screenshot(path=str(shots / f'theme-transition-{"css" if fallback else "webgl"}.png'))
                themed.wait_for_timeout(850)
                # Rapid choices must settle on the final theme without duplicate renderers.
                themed.evaluate("""() => {
                  const button = document.querySelector('[data-theme-toggle]');
                  button.click(); button.click(); button.click();
                }""")
                themed.wait_for_function("document.documentElement.dataset.theme === 'dark'")
                themed.wait_for_timeout(1000)
                assert themed.locator('.art canvas').count() == (0 if fallback else 1)
                # Saved dark choice overrides a light OS on reload.
                themed.reload()
                themed.wait_for_function("document.querySelector('.art img').currentSrc.endsWith('/hero.webp')")
                if not fallback:
                    themed.wait_for_function("document.querySelector('.art').classList.contains('gl')")
                    themed.locator('.art canvas').evaluate("e => e.getContext('webgl').getExtension('WEBGL_lose_context').loseContext()")
                    themed.wait_for_function("!document.querySelector('.art').classList.contains('gl')")
                    assert themed.locator('.art img').is_visible()
                themed.emulate_media(reduced_motion='reduce')
                themed.wait_for_timeout(100)
                assert themed.locator('.art canvas').count() == 0
                themed.locator('[data-theme-toggle]').click()
                assert themed.locator('[data-theme-toggle]').get_attribute('aria-pressed') == 'false'
                assert not themed.evaluate("document.getAnimations().some(a => (a.animationName || '').startsWith('hero-theme-'))")
                context.close()
                print('PASS: theme transition, stacking, rapid toggles, current texture, wordmark colors, saved preference, scroll, intact fallback', 'CSS' if fallback else 'WebGL')
            for theme in ('light', 'dark'):
                context = browser.new_context(java_script_enabled=False, color_scheme=theme)
                page = context.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                expected = 'hero-light.webp' if theme == 'light' else 'hero.webp'
                assert page.locator('.art img').evaluate('(e) => e.currentSrc').endswith('/' + expected)
                assert page.locator('.art img').is_visible()
                context.close()
            print('PASS: light and dark artwork without JavaScript')
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
