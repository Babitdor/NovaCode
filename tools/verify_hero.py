"""Focused phone-tilt regressions. Run: python -m tools.verify_hero."""
from functools import partial
from http.server import ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright

from tools.verify_site import Handler, ROOT, check_links


SENSOR = """(() => {
  window.tiltPermission = 'granted';
  window.tiltRequests = 0;
  DeviceOrientationEvent.requestPermission = async () => {
    window.tiltRequests++;
    if (window.tiltPermission === 'error') throw new Error('blocked');
    return window.tiltPermission;
  };
  window.sendTilt = (beta, gamma) => window.dispatchEvent(
    new DeviceOrientationEvent('deviceorientation', {beta, gamma}));
})();"""


def target(page):
    return page.locator('.art').evaluate("e => ['--mx', '--my'].map(k => Number(e.style.getPropertyValue(k)))")


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
                button = page.get_by_role('button', name='Enable tilt', exact=True)
                assert button.is_visible()
                assert page.evaluate('tiltRequests') == 0
                page.evaluate("tiltPermission = 'denied'")
                button.click()
                retry = page.get_by_role('button', name='Retry tilt', exact=True)
                assert retry.get_attribute('aria-pressed') == 'false'
                page.evaluate('sendTilt(45, 15)')
                assert target(page) == [0, 0]
                page.evaluate("tiltPermission = 'error'")
                retry.click()
                assert retry.is_enabled()
                page.evaluate("tiltPermission = 'granted'")
                retry.click()
                assert page.get_by_role('button', name='Disable tilt').get_attribute('aria-pressed') == 'true'
                page.evaluate('sendTilt(null, null); sendTilt(45, 0)')
                assert target(page) == [0, 0]  # Current holding position is neutral.
                page.evaluate('sendTilt(55, 10)')
                assert target(page) == [0.5, 0.5]
                page.wait_for_timeout(1100)
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
                page.get_by_role('button', name='Disable tilt').click()
                assert target(page) == [0, 0]
                page.evaluate('sendTilt(60, 15)')
                assert target(page) == [0, 0]
                # Android-style API: orientation events require no permission method.
                page.evaluate('delete DeviceOrientationEvent.requestPermission')
                page.get_by_role('button', name='Enable tilt', exact=True).click()
                page.evaluate('sendTilt(45, 0); sendTilt(45, -10)')
                assert target(page) == [-0.5, 0]
                page.evaluate("Object.defineProperty(screen.orientation, 'angle', {configurable:true, value:90})")
                page.evaluate('sendTilt(45, -10)')
                assert target(page) == [0, 0]  # Rotation recalibrates.
                page.evaluate('sendTilt(55, -10)')
                assert target(page) == [0.5, 0]
                page.emulate_media(reduced_motion='reduce')
                page.wait_for_timeout(100)
                assert page.locator('.hero-tilt').is_hidden()
                assert target(page) == [0, 0]
                assert page.locator('.art canvas').count() == 0
                page.evaluate('sendTilt(75, 20)')
                assert target(page) == [0, 0]
                context.close()
                print('PASS:', 'CSS fallback' if fallback else 'WebGL',
                      'permission denied/error/retry, calibration, tilt, clamping, touch scrolling, disable, rotation, reduced motion')
            # No sensor UI on desktop; mouse parallax still responds.
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
            assert page.locator('.hero-tilt').count() == 0
            page.mouse.move(1100, 250)
            assert target(page)[0] > 0
            page.screenshot(path=str(shots / 'desktop.png'))
            assert not errors, errors
            browser.close()
            print('PASS: desktop pointer preserved; no mobile JavaScript errors')
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
