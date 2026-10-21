"""Keep the conversation controls inside their new sidebar/chat containers."""

from collections import Counter
from html.parser import HTMLParser

from novacode_cli.commands.chat_handler import _make_chat_html
from novacode_cli.cowork.ui import render_cowork_html


class LayoutParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.ids = Counter()
        self.parents = {}
        self.attributes = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        if element_id:
            self.ids[element_id] += 1
            self.parents[element_id] = [item[1] for item in self.stack]
            self.attributes[element_id] = attrs
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


def test_council_controls_survive_sidebar_layout():
    html = _make_chat_html()
    layout = LayoutParser(html)
    assert all(count == 1 for count in layout.ids.values())
    for element in ("bench", "new-thread", "sid"):
        assert "workspace-sidebar" in layout.parents[element]
    for element in ("messages", "input", "send-btn", "status-msg", "stat-run", "clock"):
        assert "conversation" in layout.parents[element]
    assert "__WORKSPACE_" not in html


def test_cowork_controls_survive_sidebar_layout_and_keep_access_gate():
    html = render_cowork_html("layout-test-token")
    layout = LayoutParser(html)
    assert all(count == 1 for count in layout.ids.values())
    for element in ("path", "grant", "grants", "activity", "todos", "ws-state"):
        assert "workspace-sidebar" in layout.parents[element]
    for element in ("chat", "in", "send", "stop", "new-chat", "clear-chat", "qmodal", "conn"):
        assert "col-chat" in layout.parents[element]
    assert "disabled" in layout.attributes["in"]
    assert "disabled" in layout.attributes["send"]
    assert "hidden" in layout.attributes["col-activity"]
    assert 'const TOKEN = "layout-test-token"' in html
    assert "__COWORK_TOKEN__" not in html
    assert "__WORKSPACE_" not in html
