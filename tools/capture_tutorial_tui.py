"""Capture real Nova widgets with offline demonstration data, then render WebP assets.

Run with a Python environment containing Nova's dependencies, Pillow and Playwright:
    python tools/capture_tutorial_tui.py --repo /path/to/NovaCode
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import socket
from tempfile import TemporaryDirectory
from unittest.mock import patch
from math import ceil
from xml.etree import ElementTree

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, required=True)
REPO = parser.parse_args().repo.resolve()
if not (REPO / 'novacode_cli/tui/app.py').is_file():
    parser.error('--repo must point to the NovaCode source repository')
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'assets'
SHOTS = ROOT / '.shots/walkthrough'
SHOTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO))


async def capture():
    from rich.text import Text
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.screens import ApprovalModal
    from novacode_cli.ui.ui_elements import TokenTracker
    import novacode_cli.ui_events as ev
    from tests.test_tui_app import _SS, _FakeAgent

    class CaptureApp(NovaApp):
        CSS_PATH = REPO / 'novacode_cli/tui/interface_styles.tcss'
        # Compose the real UI without starting services, history, or agent work.
        async def on_mount(self):
            self._apply_saved_theme()
            self.query_one('#cmdpalette').display = False
            self._root_screen = self.screen
            self._init_root_pane()
            self._set_home_visible(visible=False)
            self._set_status('ready')
            self._update_mode_badge()

    # Textual invokes base and subclass mount hooks; replace the base hook as well.
    isolated_mount = CaptureApp.on_mount
    del CaptureApp.on_mount
    NovaApp.on_mount = isolated_mount
    session = _SS()
    session.auto_approve = False
    app = CaptureApp(agent=_FakeAgent(), assistant_id='nova-agent', session_state=session,
                      backend=None, token_tracker=TokenTracker(), image_tracker=None,
                      model_name='demo-model', model_provider='demo')
    regions = {}
    async with app.run_test(size=(110, 28)) as pilot:
        await pilot.pause()
        app.title = 'NovaCode · atlas · Main'
        app.query_one('#info-workspace').update(Text('~/projects/atlas'))
        await app._add_message(app._user_label(), 'user', Text('Review the parser change and check the nesting tests.'))
        for task_id, label, role in [('review', 'Review parser edge cases', 'reviewer'),
                                     ('tests', 'Check nested-block regressions', 'test-engineer')]:
            await app._render(ev.SubagentTask(task_id=task_id, phase_kind='direct',
                              subagent_type=role, label=label, description=label))
        await app._render(ev.SubagentPreview('review', 'text', '## Parser review\n\nChecking how nested lists reach validation.'))
        await app._render(ev.SubagentPreview('review', 'tool', 'read_file(path="src/parser.py")', 'demo-read'))
        await app._render(ev.SubagentPreview('review', 'result', 'Read 82 lines. The nesting branch needs a closer look.', 'demo-read'))
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        app.save_screenshot(str(OUT / 'tutorial-delegation.svg'))
        app._open_subagent_preview('review')
        await pilot.pause()
        app.save_screenshot(str(OUT / 'tutorial-agent-preview.svg'))
        await pilot.press('escape')
        await pilot.pause()
        decisions = []
        modal = ApprovalModal('Tool approval', Text('shell\n\npython -m pytest tests/test_parser.py -q\n\nRun the focused parser tests in this project.'))
        await app.push_screen(modal, decisions.append)
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        # Use composed widget bounds so website highlights follow the actual renderer.
        for selector in ['#modal-title', '#modal-body', '#modal-choices']:
            r = modal.query_one(selector).region
            regions[selector] = [r.x, r.y, r.width, r.height]
        app.save_screenshot(str(OUT / 'tutorial-approval.svg'))
        await pilot.press('n')
        await pilot.pause()
        assert decisions == ['reject'], decisions
        assert app.screen is app._root_screen
    (SHOTS / 'capture-regions.json').write_text(json.dumps(regions, indent=2))
    print('Captured delegation, agent preview, and approval; Escape and reject verified.')


original_connect = socket.socket.connect
def offline_connect(sock, address):
    if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return original_connect(sock, address)
    raise RuntimeError('Offline screenshot harness')

original_cwd = Path.cwd()
with TemporaryDirectory(prefix='nova-website-capture-') as profile:
    os.environ['NOVA_HOME'] = profile
    os.environ['NOVA_DISABLE_UPDATE_CHECK'] = '1'
    os.environ.pop('NO_COLOR', None)
    os.chdir(profile)
    try:
        with patch('socket.socket.connect', offline_connect), patch.object(Path, 'home', return_value=Path(profile)):
            asyncio.run(capture())
    finally:
        os.chdir(original_cwd)

# Rasterize the unmodified SVG with local fonts; shipped WebP files need no font requests.
from playwright.sync_api import sync_playwright
from PIL import Image

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='msedge')
    page = browser.new_page()
    page.route('https://**/*', lambda route: route.abort())
    for name in ('delegation', 'agent-preview', 'approval'):
        source = OUT / f'tutorial-{name}.svg'
        bounds = ElementTree.parse(source).getroot().attrib['viewBox'].split()
        page.set_viewport_size({'width': ceil(float(bounds[2])), 'height': ceil(float(bounds[3]))})
        page.goto(source.as_uri())
        png = SHOTS / f'capture-{name}.png'
        page.locator('svg').screenshot(path=str(png))
        with Image.open(png) as capture_image:
            assert capture_image.size == (1360, 734), capture_image.size
            capture_image.convert('RGB').save(source.with_suffix('.webp'), quality=90)
        print(source.with_suffix('.webp').name)
    browser.close()
