"""Focused browser proof for the screenshot tutorial. Run from the site root."""
from functools import partial
from http.server import ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright

from verify_site import Handler, ROOT, check_links


def main():
    check_links()
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/NovaCode/'
    shots = ROOT / '.shots' / 'walkthrough'
    shots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base)
            page.wait_for_function("!document.querySelector('#boot')")
            page.wait_for_selector('.tutorial-live')
            tutorial_ids = ['walkthrough-start', 'walkthrough', 'walkthrough-agent', 'walkthrough-debug', 'walkthrough-approval']
            assert page.locator('.tutorial-live').count() == len(tutorial_ids)
            for tutorial_id in tutorial_ids:
                tutorial = page.locator('#' + tutorial_id)
                for index in (0, 1, 2, 1, 0):
                    page.evaluate("""({tutorial_id, index}) => {
                        const step = document.getElementById(tutorial_id).querySelectorAll('.tutorial-steps li')[index];
                        const trigger = ScrollTrigger.getAll().find(t => t.trigger === step);
                        window.scrollTo({top: trigger.start + 30, behavior: 'instant'});
                        ScrollTrigger.update();
                    }""", {'tutorial_id': tutorial_id, 'index': index})
                    page.wait_for_function("({tutorial_id, index}) => document.getElementById(tutorial_id).dataset.step === String(index)", arg={'tutorial_id': tutorial_id, 'index': index})
                    assert tutorial.locator('[aria-current="step"]').count() == 1
                    assert tutorial.locator(f'[data-tut="{index}"]').count() >= 1
                    page.wait_for_function(
                        "({id, i}) => [...document.getElementById(id).querySelectorAll('[data-tut=\"' + i + '\"]')]"
                        ".every(e => Number(getComputedStyle(e).opacity) > 0.99)",
                        arg={'id': tutorial_id, 'i': index})
                    rect = tutorial.locator('.tutorial-screen').bounding_box()
                    assert rect['y'] >= 80 and rect['y'] + rect['height'] <= 1000, (tutorial_id, index, rect)
                    page.screenshot(path=str(shots / f'{tutorial_id}-desktop-{index}.png'))
            page.locator('[data-view-toggle]').first.click()
            page.wait_for_function("!document.querySelector('.tutorial-live')")
            page.locator('[data-view-toggle]').first.click()
            page.wait_for_selector('.tutorial-live')
            assert page.locator('.tutorial-live').count() == len(tutorial_ids)
            page.set_viewport_size({'width': 800, 'height': 1000})
            page.wait_for_function("!document.querySelector('.tutorial-live')")
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.wait_for_selector('.tutorial-live')
            for options, label in (
                ({'viewport': {'width': 390, 'height': 844}}, 'mobile'),
                ({'viewport': {'width': 1440, 'height': 1000}, 'reduced_motion': 'reduce'}, 'reduced'),
                ({'viewport': {'width': 1440, 'height': 1000}, 'java_script_enabled': False}, 'nojs'),
            ):
                context = browser.new_context(**options)
                fallback = context.new_page()
                fallback.goto(base)
                if options.get('java_script_enabled', True):
                    fallback.wait_for_function("!document.querySelector('#boot')")
                assert not fallback.locator('.tutorial-live').count()
                for tutorial_id in tutorial_ids:
                    tutorial = fallback.locator('#' + tutorial_id)
                    tutorial.scroll_into_view_if_needed()
                    assert tutorial.locator('.tutorial-steps li').count() == 3
                    # The screen is built in CSS; it must still render its own text at every size.
                    assert tutorial.locator('.tui').count() == 1
                    assert tutorial.locator('.tui .tui-main').evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                    assert fallback.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert tutorial.locator('.tutorial-figure').evaluate("e => getComputedStyle(e).position") == 'static'
                    fallback.screenshot(path=str(shots / f'{tutorial_id}-{label}.png'))
                context.close()
            # The plate is a screen in either theme: its ink stays legible on its own
            # plate, and the two palettes never collapse onto the same colours.
            plates = {}
            for scheme in ('dark', 'light'):
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme=scheme)
                themed = context.new_page()
                themed.goto(base)
                themed.wait_for_function("!document.querySelector('#boot')")
                plates[scheme] = themed.evaluate("""() => {
                    const cs = getComputedStyle(document.querySelector('#walkthrough .tui'));
                    const chan = c => (c.match(/[0-9.]+/g) || []).slice(0, 3).map(Number);
                    const lum = v => { const f = x => { x /= 255;
                      return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; };
                      return .2126 * f(v[0]) + .7152 * f(v[1]) + .0722 * f(v[2]); };
                    const a = lum(chan(cs.backgroundColor)), b = lum(chan(cs.color));
                    return {plate: cs.backgroundColor, ink: cs.color,
                            contrast: (Math.max(a, b) + .05) / (Math.min(a, b) + .05)};
                }""")
                assert plates[scheme]['contrast'] >= 4.5, (scheme, plates[scheme])
                themed.screenshot(path=str(shots / f'plate-{scheme}.png'))
                context.close()
            assert plates['dark']['plate'] != plates['light']['plate'], plates
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    print('PASS: forward/reverse highlights, sticky image, view toggle, mobile, reduced motion, no JS, links, themed plates.')


if __name__ == '__main__':
    main()
