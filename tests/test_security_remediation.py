"""Attack regressions and permitted controls for the security audit findings."""

import json
import threading
from http.server import HTTPServer
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.parametrize("name", [".", "..", ".staging", "installed.json"])
def test_plugin_reserved_names_rejected(tmp_path, name):
    from novacode_cli.plugins.claude_plugins import _storage_path

    with pytest.raises(ValueError):
        _storage_path(tmp_path, name)
    assert _storage_path(tmp_path, "example") == tmp_path / "example"


def test_wiki_read_write_cannot_escape(tmp_path):
    from novacode_cli.wiki.manager import WikiManager

    wiki = WikiManager(wiki_root=tmp_path / "notes")
    for operation in (
        lambda: wiki.read_page("../../outside"),
        lambda: wiki.write_page("..", "../outside", "bad"),
    ):
        with pytest.raises(ValueError):
            operation()
    wiki.write_page("projects", "valid.md", "content")
    assert wiki.read_page("projects/valid.md") == "content\n"
    assert not (tmp_path / "outside").exists()


def test_project_cannot_weaken_user_policy(tmp_path, monkeypatch):
    from novacode_cli.security import policy

    monkeypatch.setattr(policy, "HOME_DIR", tmp_path / "user")
    policy.HOME_DIR.mkdir()
    (policy.HOME_DIR / "approval-policy.json").write_text(json.dumps({"tools": {"shell": "deny"}}))
    project = tmp_path / "project"
    (project / ".nova").mkdir(parents=True)
    (project / ".nova" / "approval-policy.json").write_text(
        json.dumps({"tools": {"shell": "allow"}, "shell": {"allow": [".*"]}})
    )
    assert policy.load_policy(project).evaluate("shell", {"command": "echo safe"}).tier == "deny"


@pytest.mark.parametrize(
    "command",
    ["echo safe && python evil.py", "echo safe > ../secret", "ls | python evil.py", "echo `evil`"],
)
def test_shell_prefix_cannot_authorize_compound_operations(command):
    from novacode_cli.security.policy import load_policy

    policy = load_policy()
    assert policy.evaluate("shell", {"command": command}).tier != "allow"


def test_unattended_delegate_refuses_write_and_allows_read(monkeypatch):
    from novacode_cli.security.delegated_approval import DelegatedApprovalMiddleware
    from novacode_cli.security.policy import ApprovalPolicy

    monkeypatch.setattr(
        "novacode_cli.security.delegated_approval.get_policy",
        lambda: ApprovalPolicy(
            tool_tiers={},
            shell_allow=[],
            shell_deny=[],
            path_allow=[],
            path_deny=[],
            domain_allow=[],
            domain_deny=[],
        ),
    )
    middleware = DelegatedApprovalMiddleware()
    calls = []

    def invoke(name):
        return middleware.wrap_tool_call(
            SimpleNamespace(tool_call={"name": name, "id": "a", "args": {"file_path": "/src/a"}}),
            lambda _: calls.append(name),
        )

    assert invoke("write_file").status == "error"
    invoke("read_file")
    assert calls == ["read_file"]


def test_browser_origin_and_mutation_protection():
    from novacode_cli.security.local_http import LocalRequestHandler

    class Handler(LocalRequestHandler):
        def do_POST(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.auth_token = "dummy-token"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.post(url, headers={"X-Nova-Request": "1"}).status_code == 403
            assert (
                client.post(
                    url, headers={"Origin": "https://audit.invalid", "X-Nova-Request": "1"}
                ).status_code
                == 403
            )
            assert client.post(url).status_code == 403
            assert (
                client.post(
                    url, headers={"Host": "audit.invalid", "X-Nova-Request": "1"}
                ).status_code
                == 403
            )
            assert (
                client.post(
                    url,
                    headers={"Origin": url, "X-Nova-Request": "1", "X-Nova-Token": "dummy-token"},
                ).status_code
                == 204
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_secret_replacement_restricts_file_before_writing(tmp_path):
    import os
    import stat
    from novacode_cli.security.secret_files import write_secret_json

    target = tmp_path / "secret.json"
    write_secret_json(target, {"token": "dummy"})
    write_secret_json(target, {"token": "replacement"})
    assert json.loads(target.read_text())["token"] == "replacement"
    assert list(tmp_path.iterdir()) == [target]
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_markdown_url_cannot_inject_event_attribute():
    import shutil
    import subprocess
    from html.parser import HTMLParser
    from novacode_cli.cowork.ui import render_cowork_html

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the actual Markdown renderer")
    html = render_cowork_html("dummy")
    javascript = html[html.index("function mdEscape(") : html.index("function onEvent(")]
    javascript += (
        "process.stdout.write(renderMarkdown(JSON.parse(require('fs').readFileSync(0,'utf8'))));"
    )
    rendered = subprocess.run(
        [node, "-e", javascript],
        input=json.dumps('[audit](https://example.invalid/"onclick="evil)'),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                assert "onclick" not in dict(attrs)

    Parser().feed(rendered)
    assert '<a href="https://' in rendered


def test_redirect_denied_before_target_request(monkeypatch):
    from http.server import BaseHTTPRequestHandler
    import requests
    from novacode_cli.tools import fetch_tools
    from novacode_cli.security import policy

    denied_policy = policy.load_policy()
    denied_policy.domain_deny.append("127.0.0.1")
    visited = []

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/private")
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(policy, "get_policy", lambda: denied_policy)
    try:
        with requests.Session() as session:
            session.trust_env = False
            monkeypatch.setattr(fetch_tools, "_get_fetch_session", lambda: session)
            result = fetch_tools.fetch_url.invoke(
                {"url": f"http://localhost:{server.server_port}/redirect", "max_retries": 1}
            )
            assert result["success"] is False
            assert "denied" in result["error"].lower()
            assert visited == ["/redirect"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("kind", ["create", "trello"])
async def test_browser_servers_require_session_capability(kind, tmp_path, monkeypatch):
    from novacode_cli.commands.create_server import CreateServer, Settings
    from novacode_cli.commands.trello_server import TrelloServer

    monkeypatch.setattr(
        Settings,
        "from_environment",
        lambda: SimpleNamespace(
            get_global_skills_dir=lambda: tmp_path / "skills",
            get_project_skills_dir=lambda: None,
        ),
    )
    backend = CreateServer() if kind == "create" else TrelloServer()
    await backend.start()
    try:
        base = f"http://localhost:{backend.port}"
        with httpx.Client(trust_env=False) as client:
            assert client.get(base).status_code == 403
            page = client.get(backend.url)
            assert page.status_code == 200
            assert "no-referrer" in page.text
            assert "X-Nova-Token" in page.text
            endpoint = "/api/tasks" if kind == "trello" else "/api/skills"
            payload = (
                {"description": "safe task"}
                if kind == "trello"
                else {"name": "safe-skill", "content": "safe"}
            )
            assert (
                client.post(
                    base + endpoint, json=payload, headers={"X-Nova-Request": "1"}
                ).status_code
                == 403
            )
            response = client.post(
                base + endpoint,
                json=payload,
                headers={
                    "X-Nova-Request": "1",
                    "X-Nova-Token": backend._server.auth_token,
                    "Origin": base,
                },
            )
            assert response.status_code == 201
    finally:
        backend.stop()


def test_cowork_backend_does_not_expose_external_virtual_mounts(tmp_path):
    from deepagents.backends import CompositeBackend
    from deepagents.backends.filesystem import FilesystemBackend
    from novacode_cli.cowork.broker_middleware import confine_backend

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("private-marker")
    original = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={
            "/external/": FilesystemBackend(root_dir=outside, virtual_mode=True),
        },
    )
    assert original.read("/external/secret").error is None
    confined = confine_backend(original)
    assert confined.read("/external/secret").error is not None
    assert original.sorted_routes
