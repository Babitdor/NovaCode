"""Optional real-browser checks with synthetic SSE, workspace APIs, and WebSocket events.

Requires Playwright and Chromium (or an installed Chrome). No models, external
services, or real folder grants are used.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pw = pytest.importorskip("playwright.sync_api")

from novacode_cli.commands.chat_handler import _make_chat_html
from novacode_cli.cowork.ui import render_cowork_html


@pytest.fixture(scope="module")
def browser():
    executable = next(
        (value for name in ("chromium", "chromium-browser", "google-chrome")
         if (value := shutil.which(name))), None
    )
    windows_chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if not executable and windows_chrome.exists():
        executable = str(windows_chrome)
    with pw.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(executable_path=executable, headless=True)
        except pw.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Install a Playwright Chromium browser to run UI checks")
            raise
        yield instance
        instance.close()


@pytest.fixture
def ui(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    errors, requests, grants = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    context.add_init_script("""
      window.sentMessages = [];
      window.WebSocket = class {
        constructor(url) { this.url=url; this.readyState=1; window.testSocket=this;
          setTimeout(()=>this.onopen && this.onopen(),0); }
        send(data) { window.sentMessages.push(JSON.parse(data)); }
        close() { this.readyState=3; if(this.onclose) this.onclose(); }
        emit(data) { this.onmessage({data:JSON.stringify(data)}); }
      };
    """)

    def backend(route):
        request = route.request
        path = urlsplit(request.url).path
        payload = json.loads(request.post_data or "{}")
        requests.append((path, request.headers, payload))
        if path == "/council":
            route.fulfill(content_type="text/html", body=_make_chat_html())
            return
        if path == "/cowork":
            route.fulfill(content_type="text/html", body=render_cowork_html("test-ipc-token"))
            return
        if path == "/api/council":
            topic = parse_qs(urlsplit(request.url).query)["topic"][0]
            if topic == "error":
                events = [("council_error", {"message": "Synthetic failure"})]
            else:
                advisor = {"id": "architect", "name": "The Architect", "color": "#a5b6ff"}
                events = [
                    ("council_start", {"agents": [advisor]}), ("agent_start", advisor),
                    ("agent_delta", {"id": "architect", "text": "Start with a small scope."}),
                    ("agent_done", {"id": "architect", "text": "Start with a small scope."}),
                    ("verdict", {"winner_id": "architect", "winner_name": "The Architect",
                                 "tally": {"architect": 1}, "votes": 1, "answer": "Start small."}),
                    ("done", {}),
                ]
            body = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events)
            route.fulfill(content_type="text/event-stream", body=body)
            return
        data = {}
        if path == "/api/workspace":
            data = {"grants": grants}
        elif path == "/api/workspace/grant":
            grant = dict(payload, id="synthetic-grant", display_path=payload["path"], canonical=payload["path"])
            grants.append(grant)
            data = {"grant": grant}
        elif path == "/api/workspace/revoke":
            grants.clear()
            data = {"revoked": True}
        elif path == "/sessions":
            data = {"session_id": "synthetic-session"}
        route.fulfill(content_type="application/json", body=json.dumps(data))

    context.route("http://nova.test/**", backend)
    # Markdown presentation dependencies stay outside this layout test's scope.
    context.route("https://**", lambda route: route.fulfill(
        content_type="application/javascript", body=(
            "window.hljs={highlightElement:()=>{}};" if "highlight" in route.request.url
            else 'window.marked={parse:text=>"<p>"+String(text).replaceAll("<","&lt;")+"</p>"};'
        )
    ))
    try:
        yield SimpleNamespace(page=page, requests=requests)
        assert errors == []
    finally:
        context.close()


def check_drawer(page, composer):
    for width in (320, 390, 760):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        box = page.locator(composer).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
        page.get_by_role("button", name="Open sidebar", exact=True).click()
        pw.expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "true")
        page.keyboard.press("Escape")
        pw.expect(page.locator("#sidebar-toggle")).to_be_focused()
        assert page.locator("#workspace-sidebar").evaluate("el => el.inert")
    page.set_viewport_size({"width": 1440, "height": 1000})
    assert not page.locator("#workspace-sidebar").evaluate("el => el.inert")
    assert page.locator(".workspace-chat").bounding_box()["x"] == page.locator("#workspace-sidebar").bounding_box()["width"]


def test_council_sidebar_keeps_stream_followups_and_approval_working(ui):
    page = ui.page
    page.goto("http://nova.test/council")
    check_drawer(page, "#input-area")
    page.get_by_role("button", name="Compare approaches", exact=True).click()
    pw.expect(page.locator("#input")).to_have_value("Help me compare two approaches to ")
    for question in ("Compare these approaches", "And what should we do first?"):
        page.locator("#input").fill(question)
        page.locator("#send-btn").click()
        pw.expect(page.locator("#input")).to_be_enabled()
    pw.expect(page.locator(".verdict")).to_have_count(2)
    pw.expect(page.locator("#bench .seat")).to_have_count(1)
    page.locator(".verdict").last.get_by_role("button", name="✓ Approve", exact=True).click()
    pw.expect(page.get_by_text("✓ Settled by you", exact=True)).to_be_visible()
    page.locator("#input").fill("error")
    page.locator("#send-btn").click()
    pw.expect(page.get_by_text("Synthetic failure", exact=True)).to_be_visible()
    pw.expect(page.locator("#input")).to_be_enabled()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#sidebar-toggle").click()
    page.keyboard.press("Shift+Tab")
    pw.expect(page.locator("#stop-council")).to_be_focused()
    page.keyboard.press("Tab")
    pw.expect(page.locator("#sidebar-close")).to_be_focused()
    page.locator("#new-thread").click()
    pw.expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "false")
    pw.expect(page.locator("#input")).to_be_focused()
    pw.expect(page.locator(".verdict")).to_have_count(0)


def test_cowork_sidebar_keeps_grants_chat_questions_and_stop_working(ui):
    page = ui.page
    page.goto("http://nova.test/cowork")
    pw.expect(page.locator("#in")).to_be_disabled()
    assert not any(path == "/sessions" for path, _, _ in ui.requests)
    check_drawer(page, "#composer")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Choose a workspace", exact=True).click()
    pw.expect(page.locator("#path")).to_be_focused()
    page.locator("#path").fill("/synthetic/folder")
    page.locator("#perm-x").uncheck()
    page.locator("#grant").click()
    pw.expect(page.locator("#conn")).to_have_text("● connected")
    grant = next((headers, payload) for path, headers, payload in ui.requests if path == "/api/workspace/grant")
    assert grant[0]["x-cowork-token"] == "test-ipc-token"
    assert grant[1]["execute"] is False
    assert "token=test-ipc-token" in page.evaluate("testSocket.url")
    page.keyboard.press("Escape")
    pw.expect(page.locator("#sidebar-toggle")).to_be_focused()
    page.locator("#in").fill("Review this project")
    page.locator("#send").click()
    assert page.evaluate("sentMessages.at(-1)") == {"type": "message", "data": {"content": "Review this project"}}
    page.evaluate("""() => {
      testSocket.emit({type:'assistant_token',data:{token:'A focused response.'}});
      testSocket.emit({type:'assistant_done'});
      testSocket.emit({type:'todo_update',data:{todos:[{content:'Review the project',status:'completed'}]}});
      testSocket.emit({type:'tool_finished',data:{name:'read_file',output:'Done'}});
      testSocket.emit({type:'interrupt',data:{kind:'question',payload:{question:'Continue?',options:['Continue']}}});
    }""")
    page.get_by_role("button", name="Continue", exact=True).click()
    assert page.evaluate("sentMessages.at(-1).type") == "interrupt_response"
    page.evaluate("testSocket.emit({type:'assistant_token',data:{token:'Working.'}})")
    page.set_viewport_size({"width": 320, "height": 844})
    assert page.locator(".chat-topbar").bounding_box()["height"] == 70
    page.locator("#stop").click()
    assert page.evaluate("sentMessages.at(-1).type") == "cancel"
    page.locator("#sidebar-toggle").click()
    page.get_by_role("tab", name="Activity", exact=True).click()
    pw.expect(page.locator("#todos")).to_contain_text("Review the project")
    pw.expect(page.locator("#activity")).to_contain_text("tool done: read_file")
    page.get_by_role("tab", name="Workspace", exact=True).click()
    page.get_by_role("button", name="revoke", exact=True).click()
    pw.expect(page.locator("#in")).to_be_disabled()
    assert page.evaluate("testSocket.readyState") == 3


def test_council_decision_brief_and_question_controls(ui):
    page = ui.page
    page.add_init_script('''
      window.EventSource = class extends EventTarget {
        constructor(url) { super(); this.url=url; this.readyState=1; window.decisionStream=this; }
        close() { this.readyState=2; }
        emit(type, data) { this.dispatchEvent(new MessageEvent(type,{data:JSON.stringify(data)})); }
      };
    ''')
    page.goto('http://nova.test/council')
    page.locator('#decision-options').fill('Build\nBuy')
    page.locator('#decision-criteria').fill('Time to market')
    page.locator('#input').fill('Which approach?')
    page.locator('#send-btn').click()
    assert 'options=Build%0ABuy' in page.evaluate('window.decisionStream.url')
    page.evaluate('''window.decisionStream.emit('question', {
      id:'architect', name:'Architect', question:'What matters most?', choices:['Speed','Cost'],token:'test-token'
    })''')
    page.get_by_role('button', name='Speed', exact=True).click()
    pw.expect(page.get_by_role('button', name='Speed', exact=True)).to_be_disabled()
    assert any(path == '/api/council/answer' and payload['answer'] == 'Speed'
               for path, headers, payload in ui.requests)
    page.evaluate('''window.decisionStream.emit('verdict', {
      winner_name:'Buy', option_tally:{Build:1,Buy:3},votes:4,answer:'Buy saves time.'
    }); window.decisionStream.emit('done',{});''')
    pw.expect(page.get_by_text('Council recommendation: Buy', exact=True)).to_be_visible()
    pw.expect(page.locator('#input')).to_be_enabled()


def test_council_web_settings_drafts_templates_models_and_mobile(ui):
    from dataclasses import asdict
    from novacode_cli.council_settings import default_settings, personality_templates

    page = ui.page
    stored = default_settings().to_dict()
    saves = []
    reject = [False]

    def settings_api(route):
        nonlocal stored
        if route.request.method == 'POST':
            if reject[0]:
                route.fulfill(status=400, content_type='application/json', body=json.dumps({'error': 'Every council member needs a name and system prompt'}))
                return
            stored = json.loads(route.request.post_data)
            saves.append(stored)
            data = {'settings': stored}
        else:
            data = {'settings': stored, 'defaults': default_settings().to_dict(),
                    'templates': [asdict(t) for t in personality_templates()],
                    'providers': [{'id':'openai','name':'OpenAI','models':['example-model']}]}
        route.fulfill(content_type='application/json', body=json.dumps(data))

    page.route('http://nova.test/api/council/settings', settings_api)
    page.goto('http://nova.test/council')
    page.get_by_role('button', name='Council settings', exact=True).click()
    pw.expect(page.locator('#settings-name')).to_have_value('The Architect')
    pw.expect(page.locator('#settings-model')).to_be_disabled()
    page.locator('#settings-name').fill('Draft voice')
    page.locator('#settings-provider').select_option('openai')
    page.locator('#settings-model').fill('custom-model')
    page.locator('#settings-template').select_option('security')
    page.get_by_role('button', name='Apply', exact=True).click()
    pw.expect(page.locator('#settings-model')).to_have_value('custom-model')
    page.locator('#settings-seats button').last.click()
    pw.expect(page.locator('#settings-role')).to_have_text('Chairman · web synthesis; judge · planning councils')
    page.locator('#settings-system').fill('Evidence before confidence.')
    page.locator('#settings-seats button').first.click()
    pw.expect(page.locator('#settings-name')).to_have_value('The Security Reviewer')
    page.get_by_role('button', name='Cancel', exact=True).click()
    assert not saves
    page.get_by_role('button', name='Council settings', exact=True).click()
    pw.expect(page.locator('#settings-name')).to_have_value('The Architect')
    page.locator('#settings-name').fill('Saved architect')
    reject[0] = True
    page.get_by_role('button', name='Save changes', exact=True).click()
    pw.expect(page.locator('#settings-message')).to_contain_text('Every council member')
    pw.expect(page.locator('#council-settings')).to_be_visible()
    reject[0] = False
    page.get_by_role('button', name='Save changes', exact=True).click()
    pw.expect(page.locator('#council-settings')).not_to_be_visible()
    assert saves[-1]['members'][0]['name'] == 'Saved architect'
    for width in (320,390,760):
        page.set_viewport_size({'width':width,'height':844})
        page.get_by_role('button', name='Council settings', exact=True).click()
        pw.expect(page.locator('#settings-name')).to_have_value('Saved architect')
        assert page.locator('#council-settings').evaluate('el=>el.scrollWidth<=el.clientWidth')
        page.locator('#settings-seats button').last.click()
        page.get_by_role('button', name='Reset all', exact=True).click()
        pw.expect(page.locator('#settings-system')).to_have_value(default_settings().judge.system)
        page.keyboard.press('Escape')
        pw.expect(page.get_by_role('button',name='Council settings',exact=True)).to_be_focused()


def test_cowork_sidebar_scrolls_whole_panels_on_short_and_mobile_screens(ui):
    page = ui.page
    grants = [dict(id=f'folder-{i}', display_path=f'/work/projects/project-{i}/deeply/nested/folder',
                   read=True, write=True, execute=False, recursive=True) for i in range(35)]
    page.route('http://nova.test/api/workspace', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'grants': grants})))
    page.goto('http://nova.test/cowork')
    pw.expect(page.locator('.grant')).to_have_count(35)
    for width,height in ((1440,560),(390,568),(320,480)):
        page.set_viewport_size({'width':width,'height':height})
        if width <= 760:
            page.locator('#sidebar-toggle').click()
        page.get_by_role('tab',name='Workspace',exact=True).click()
        region = page.get_by_role('region',name='Workspace folders',exact=True)
        region.evaluate('el=>el.scrollTop=0')
        pw.expect(page.locator('#path')).to_be_in_viewport()
        assert region.evaluate('el=>el.scrollHeight>el.clientHeight')
        assert page.locator('#grants').evaluate('el=>getComputedStyle(el).overflowY') == 'visible'
        region.focus()
        page.keyboard.press('Control+End')
        pw.expect(page.locator('.grant').last).to_be_in_viewport()
        pw.expect(page.locator('#workspace-tab')).to_be_in_viewport()
        pw.expect(page.locator('#ws-state')).to_be_in_viewport()
        page.get_by_role('tab',name='Activity',exact=True).click()
        page.evaluate('''()=>{
          testSocket.emit({type:'todo_update',data:{todos:Array.from({length:30},(_,i)=>({content:'Task '+i,status:'pending'}))}});
          testSocket.emit({type:'tool_finished',data:{name:'read_file',output:'Last activity'}});
        }''')
        activity = page.get_by_role('region',name='Workspace activity',exact=True)
        assert activity.evaluate('el=>el.scrollHeight>el.clientHeight')
        activity.evaluate('el=>el.scrollTop=el.scrollHeight')
        pw.expect(page.locator('#activity .act').last).to_be_in_viewport()
        pw.expect(page.locator('#activity-tab')).to_be_in_viewport()
        if width <= 760:
            page.keyboard.press('Escape')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_council_streams_chairman_before_showing_final_approval(ui):
    page=ui.page
    page.add_init_script('''window.EventSource=class extends EventTarget{
      constructor(url){super();this.readyState=1;window.councilStream=this;}
      close(){this.readyState=2;}
      emit(type,data){this.dispatchEvent(new MessageEvent(type,{data:JSON.stringify(data)}));}
    };''')
    page.goto('http://nova.test/council')
    page.locator('#input').fill('Which option is safer?')
    page.locator('#send-btn').click()
    page.evaluate('''()=>{
      councilStream.emit('vote_result',{tied:true,votes:2});
      councilStream.emit('chairman_start',{name:'Chairman'});
      councilStream.emit('chairman_delta',{text:'B is safer, subject to verification.'});
    }''')
    pw.expect(page.get_by_text('B is safer, subject to verification.',exact=True)).to_be_visible()
    pw.expect(page.get_by_role('button',name='✓ Approve',exact=True)).to_have_count(0)
    page.evaluate('''()=>{
      councilStream.emit('verdict',{synthesized:true,chairman_name:'Chairman',winner_name:'No consensus',
        option_tally:{A:1,B:1},votes:2,tied:true,answer:'Prefer B. Verify isolation before committing.'});
      councilStream.emit('done',{});
    }''')
    pw.expect(page.get_by_text('Final consolidated answer',exact=True)).to_be_visible()
    pw.expect(page.get_by_role('button',name='✓ Approve',exact=True)).to_be_visible()
    pw.expect(page.get_by_text('B is safer, subject to verification.',exact=True)).to_have_count(0)
    pw.expect(page.locator('#input')).to_be_enabled()
