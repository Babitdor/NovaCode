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
                    assert tutorial.locator('.tutorial-focus').nth(index).evaluate("e => getComputedStyle(e).opacity") == '1'
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
                    tutorial.locator('img').evaluate('e => e.decode()')
                    assert tutorial.locator('img').evaluate('e => e.complete && e.naturalWidth === Number(e.getAttribute("width"))')
                    assert fallback.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert tutorial.locator('.tutorial-figure').evaluate("e => getComputedStyle(e).position") == 'static'
                    fallback.screenshot(path=str(shots / f'{tutorial_id}-{label}.png'))
                context.close()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    print('PASS: forward/reverse highlights, sticky image, view toggle, mobile, reduced motion, no JS, links.')


if __name__ == '__main__':
    main()
