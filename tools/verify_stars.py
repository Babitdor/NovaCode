"""Browser proof for the background rocket. Run: python -m tools.verify_stars."""
from functools import partial
from http.server import ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright

from tools.verify_site import Handler, ROOT


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shots = ROOT / '.shots' / 'rocket'
    shots.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            for width, theme, motion in ((1440, 'dark', 'no-preference'),
                                         (1440, 'light', 'no-preference'),
                                         (390, 'dark', 'no-preference'),
                                         (390, 'light', 'reduce')):
                context = browser.new_context(viewport={'width': width, 'height': 900},
                                              color_scheme=theme, reduced_motion=motion)
                # Record actual rocket drawing coordinates, without a production test API.
                context.add_init_script("""(() => {
                  window.rocketFrames = [];
                  const rotate = CanvasRenderingContext2D.prototype.rotate;
                  CanvasRenderingContext2D.prototype.rotate = function(angle) {
                    if (this.canvas.id === 'page-stars') {
                      const m = this.getTransform();
                      rocketFrames.push({x: m.e / m.a, y: m.f / m.d});
                    }
                    return rotate.call(this, angle);
                  };
                })()""")
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.clock.install()
                page.goto(f'http://127.0.0.1:{server.server_port}/NovaCode/')
                page.clock.run_for(1800)
                assert page.evaluate('rocketFrames.length') == 0, 'rocket launched behind hero'
                mascot = page.locator('#nova-mascot')
                assert mascot.is_visible()
                # A direct activation proves launch independently of the scroll-triggered one.
                mascot.evaluate('e => e.click()')
                assert mascot.get_attribute('data-reaction') == ''
                assert page.locator('#nova-hint').inner_text() == ('Hello, explorer!' if motion == 'reduce' else 'Liftoff!')
                page.clock.run_for(1600)
                assert mascot.get_attribute('data-reaction') is None
                if motion != 'reduce':
                    assert page.evaluate('rocketFrames.length') > 0, 'mascot did not launch rocket'
                    # Complete the manual flight before testing the automatic launch.
                    page.clock.fast_forward(30000)
                    page.evaluate('rocketFrames.length = 0')
                page.evaluate("""() => {
                  Math.random = () => .7;
                  window.scrollTo({top: document.querySelector('#memory').offsetTop - 120, behavior:'instant'});
                }""")
                page.clock.run_for(3500)
                if motion == 'reduce':
                    assert page.evaluate('rocketFrames.length') == 0
                    assert page.locator('#page-stars').evaluate('e => e.width') == 300, 'reduced motion started canvas'
                else:
                    frames = page.evaluate('rocketFrames')
                    assert len(frames) > 100, len(frames)
                    assert frames[-1]['y'] < frames[0]['y'] - 300, 'rocket must climb'
                    assert abs(frames[-1]['x'] - frames[0]['x']) > 10, 'ascent should curve'
                    page.screenshot(path=str(shots / f'{width}-{theme}.png'))
                    page.locator('#page-stars').screenshot(path=str(shots / f'{width}-{theme}-canvas.png'))
                    page.clock.run_for(3000)
                    count = page.evaluate('rocketFrames.length')
                    page.clock.run_for(3000)
                    assert page.evaluate('rocketFrames.length') == count, 'flight did not finish'
                    # Hide the tab before the next scheduled launch.
                    page.evaluate("Object.defineProperty(document, 'hidden', {value:true, configurable:true}); document.dispatchEvent(new Event('visibilitychange'))")
                    page.clock.fast_forward(30000)
                    assert page.evaluate('rocketFrames.length') == count, 'hidden tab still animates'
                    page.evaluate("Object.defineProperty(document, 'hidden', {value:false, configurable:true}); document.dispatchEvent(new Event('visibilitychange'))")
                    page.clock.run_for(1500)
                    assert page.evaluate('rocketFrames.length') > count, 'visible tab did not resume'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                mascot.scroll_into_view_if_needed()
                page.clock.run_for(500)
                if width == 1440:
                    box = mascot.bounding_box()
                    page.mouse.move(box['x'] - 150, box['y'] - 100)
                    assert page.locator('.nova-sprite').evaluate('e => e.style.backgroundPosition') == '0% 0%'
                    page.mouse.move(width - 1, box['y'] + 220)
                    assert page.locator('.nova-sprite').evaluate('e => e.style.backgroundPosition') == '100% 100%'
                mascot.focus()
                page.keyboard.press('Enter')
                assert mascot.get_attribute('data-reaction') == ''
                page.clock.run_for(350)
                page.screenshot(path=str(shots / f'{width}-{theme}-mascot.png'))
                page.clock.run_for(1600)
                assert mascot.get_attribute('data-reaction') is None
                print(f'PASS: {width}px {theme}, {motion}', flush=True)
                context.close()
            browser.close()
        assert not errors, errors
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
