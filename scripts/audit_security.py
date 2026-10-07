"""Reproduce audit findings without accessing live credentials or user data.

Exit 1 means a vulnerability was reproduced, 2 means a check failed unexpectedly.
This is an audit evidence runner, not a permanent regression test suite. All file
mutations occur in TemporaryDirectory fixtures; network requests are loopback-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from contextlib import contextmanager
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def default_policy():
    from novacode_cli.security import policy as p

    return p.ApprovalPolicy(
        tool_tiers=dict(p._DEFAULT_TOOL_TIERS),
        shell_allow=list(p._DEFAULT_SHELL_ALLOW),
        shell_deny=list(p._DEFAULT_SHELL_DENY),
        path_allow=[],
        path_deny=list(p._DEFAULT_PATH_DENY),
        domain_allow=[],
        domain_deny=list(p._DEFAULT_DOMAIN_DENY),
    )


def request(name, **args):
    return SimpleNamespace(tool_call={"name": name, "id": "audit", "args": args})


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="nova-security-audit-") as directory:
        yield Path(directory)


@contextmanager
def serve(handler, backend=None):
    server = HTTPServer(("127.0.0.1", 0), handler)
    if backend is not None:
        server.backend = backend
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def http(port, path, method="GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method=method,
        headers={"Origin": "https://audit.invalid", "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=5) as response:
        return response.status, dict(response.headers), response.read().decode()


def shell_prefix():
    policy = default_policy()
    direct = policy.evaluate("shell", {"command": "python audit.py"}).tier
    chained = policy.evaluate("shell", {"command": "echo audit && python audit.py"}).tier
    redirect = policy.evaluate("shell", {"command": "echo audit > ../outside.txt"}).tier
    assert direct == "ask" and chained == "allow" and redirect == "allow"
    assert policy.evaluate("shell", {"command": "ls; rm -rf /"}).tier == "deny"
    return "Direct Python requires approval; an echo prefix or output redirection is allowed."


def project_policy():
    from novacode_cli.security import policy as p

    with fixture() as directory:
        user = directory / "user"
        project = directory / "project"
        user.mkdir()
        (project / ".nova").mkdir(parents=True)
        (user / "approval-policy.json").write_text(json.dumps({"tools": {"shell": "deny"}}))
        with patch.object(p, "HOME_DIR", user):
            before = p.load_policy(project).evaluate("shell", {"command": "python audit.py"})
            (project / ".nova" / "approval-policy.json").write_text(
                json.dumps({"tools": {"shell": "allow"}})
            )
            after = p.load_policy(project).evaluate("shell", {"command": "python audit.py"})
        assert before.tier == "deny" and after.tier == "allow"
    return "Project-controlled policy overrides the user's shell deny default."


def cowork_shell():
    from novacode_cli.cowork import policy as p
    from novacode_cli.cowork.broker_middleware import CoworkBrokerMiddleware

    with fixture() as directory:
        root = directory / "workspace"
        root.mkdir()
        outside = directory / "outside.txt"
        outside.write_text("private fixture")
        policy = p.WorkspacePolicy(directory / "grants.json")
        grant = policy.grant(root, execute=False)
        middleware = CoworkBrokerMiddleware(root)
        with patch.object(p, "_policy", policy):
            assert middleware._deny(request("read_file", file_path="/../outside.txt")) is not None
            command = "echo audit-marker > ../outside.txt"
            assert middleware._deny(request("shell", command=command)) is not None
            policy.revoke(grant.id)
            policy.grant(root, execute=True)
            assert middleware._deny(request("shell", command=command)) is None

            def execute(req):
                subprocess.run(req.tool_call["args"]["command"], shell=True, cwd=root,
                               check=True, timeout=5, capture_output=True)
                return "executed"

            assert middleware.wrap_tool_call(request("shell", command=command), execute) == "executed"
            assert outside.read_text().strip() == "audit-marker"
    return "An execute grant permits a shell write outside the granted folder; direct read is denied."


def wiki_traversal():
    from novacode_cli.cowork.broker_middleware import CoworkBrokerMiddleware
    from novacode_cli.tools import wiki_tools
    from novacode_cli.wiki.manager import WikiManager

    with fixture() as directory:
        workspace = directory / "workspace"
        workspace.mkdir()
        manager = WikiManager(workspace / ".nova" / "wiki")
        target = directory / "outside.txt"
        escape = os.path.relpath(target, manager.root / "wiki" / "technologies").replace("\\", "/")
        path = f"technologies/{escape}"
        assert CoworkBrokerMiddleware(workspace)._deny(request("wiki_write", path=path)) is None
        with patch.object(wiki_tools, "WikiManager", return_value=manager):
            outcome = wiki_tools.wiki_write.invoke({"path": path, "content": "audit-marker"})
            assert outcome.startswith("✓") and target.read_text().strip() == "audit-marker"
            read_path = os.path.relpath(target, manager.root / "wiki").replace("\\", "/")
            assert wiki_tools.wiki_read.invoke({"path": read_path}).strip() == "audit-marker"
            wiki_tools.wiki_write.invoke({"path": "technologies/safe.md", "content": "safe"})
            assert (manager.root / "wiki" / "technologies" / "safe.md").read_text().strip() == "safe"
    return "The actual wiki tools read/write a temporary file outside their workspace using '..'."


def plugin_parent():
    from novacode_cli.plugins import claude_plugins as cp
    from novacode_cli.plugins import marketplaces as mp

    class StoppedUnsafeDelete(Exception):
        pass

    attempted = []
    with fixture() as directory:
        source = directory / "source"
        (source / ".claude-plugin").mkdir(parents=True)
        (source / ".claude-plugin" / "plugin.json").write_text('{"name":".."}')
        (source / ".claude-plugin" / "marketplace.json").write_text('{"name":".."}')
        for module, root_key, installer in ((cp, "PLUGINS_DIR", cp.install),
                                             (mp, "MARKETPLACES_DIR", mp.add)):
            install_root = directory / module.__name__.rsplit(".", 1)[-1] / "entries"
            original = cp._force_rmtree

            def guarded_delete(path):
                resolved = path.resolve()
                if resolved == install_root.resolve() or not resolved.is_relative_to(install_root.resolve()):
                    attempted.append(resolved)
                    raise StoppedUnsafeDelete
                original(path)

            with patch.object(module, root_key, install_root), patch.object(cp, "_force_rmtree", guarded_delete):
                try:
                    installer(str(source))
                except StoppedUnsafeDelete:
                    pass
                else:
                    raise AssertionError("Unsafe destination was not intercepted")
            assert attempted[-1] == install_root.parent
    return "Plugin and marketplace name '..' both reach recursive deletion of the parent; deletion intercepted."


def browser_apis():
    from novacode_cli.commands.create_server import CreateRequestHandler
    from novacode_cli.commands.trello_server import TrelloRequestHandler, TrelloServer

    with fixture() as directory:
        skills = directory / "skills"
        settings = SimpleNamespace(get_global_skills_dir=lambda: skills,
                                   get_project_skills_dir=lambda: None)
        with patch.object(CreateRequestHandler, "settings", settings), serve(CreateRequestHandler) as port:
            status, headers, _ = http(port, "/api/skills", "OPTIONS")
            assert status == 204 and headers["Access-Control-Allow-Origin"] == "*"
            status, headers, _ = http(port, "/api/skills", "POST", {
                "name": "audit-only", "content": "# harmless fixture", "scope": "global"})
            assert status == 201 and headers["Access-Control-Allow-Origin"] == "*"
            assert (skills / "audit-only" / "SKILL.md").is_file()
            try:
                http(port, "/api/unknown")
            except HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError("Unknown-route control failed")
        backend = TrelloServer()
        with serve(TrelloRequestHandler, backend) as port:
            status, headers, _ = http(port, "/api/tasks", "POST", {"description": "harmless audit task"})
            assert status == 201 and headers["Access-Control-Allow-Origin"] == "*"
            assert http(port, "/api/settings", "POST", {"auto_advance": True})[0] == 200
            assert backend.auto_advance and backend.pop_next_loaded_task()["description"] == "harmless audit task"
    return "Cross-origin, unauthenticated HTTP creates a skill and queues an auto-advance task; no agent run."


def cowork_xss():
    from novacode_cli.cowork.ui import render_cowork_html

    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node is required to run the actual Markdown renderer")
    html = render_cowork_html("audit-token")
    javascript = html[html.index("function mdEscape("):html.index("function onEvent(")]
    javascript += "process.stdout.write(renderMarkdown(JSON.parse(require('fs').readFileSync(0,'utf8'))));"
    payload = '[audit](https://example.invalid/"onclick="globalThis.auditFlag=1)'
    proc = subprocess.run([node, "-e", javascript], input=json.dumps(payload),
                          text=True, capture_output=True, check=True, timeout=5)

    class Anchors(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.links.append(dict(attrs))

    parser = Anchors()
    parser.feed(proc.stdout)
    assert parser.links[0]["onclick"] == "globalThis.auditFlag=1"
    control = subprocess.run([node, "-e", javascript], input=json.dumps("<script>audit</script>"),
                             text=True, capture_output=True, check=True, timeout=5)
    assert "<script>" not in control.stdout and "&lt;script&gt;" in control.stdout
    return "The actual JS renderer produces an onclick attribute from a Markdown URL; raw-script control escaped."


def autoapprove_denies():
    from novacode_cli.security import policy as p
    from novacode_cli.ui.hitl_approval import evaluate_tool_actions

    req = {"action_requests": [{"name": "fetch_url", "args": {"url": "http://169.254.169.254/"}}]}
    with patch.object(p, "get_policy", return_value=default_policy()):
        denied = evaluate_tool_actions(req, SimpleNamespace(auto_approve=False))
        approved = evaluate_tool_actions(req, SimpleNamespace(auto_approve=True))
    assert denied[0]["type"] == "reject" and approved[0]["type"] == "approve"
    return "Automatic approval overrides even a policy-denied metadata request; no request sent."


def redirect_policy():
    import requests
    from novacode_cli.tools import fetch_tools

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/private")
                self.end_headers()
            else:
                body = b'{"marker":"audit-private-endpoint"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

    policy = default_policy()
    policy.domain_deny.append("127.0.0.1")
    policy.domain_allow.append("localhost")
    with serve(Redirect) as port, requests.Session() as session:
        session.trust_env = False
        initial = f"http://localhost:{port}/redirect"
        final = f"http://127.0.0.1:{port}/private"
        assert policy.evaluate("fetch_url", {"url": initial}).tier == "allow"
        assert policy.evaluate("fetch_url", {"url": final}).tier == "deny"
        with patch.object(fetch_tools, "_get_fetch_session", return_value=session):
            result = fetch_tools.fetch_url.invoke({"url": initial, "max_retries": 1})
        assert result["success"] and "audit-private-endpoint" in json.dumps(result)
    return "A permitted localhost URL redirects to a policy-denied loopback host and returns its response."


def subagent_approvals():
    from novacode_cli.agents import core_agent

    tool = SimpleNamespace(name="write_file")
    spec = {"name": "audit-agent", "system_prompt": "audit", "tools": [tool],
            "interrupt_on": {"write_file": {"allowed_decisions": ["approve", "reject"]}}}
    with patch.object(core_agent, "retrieve_async_subagents", return_value=[]):
        hardened = core_agent._harden_subagent_specs([spec])[0]
    assert hardened["interrupt_on"] == {} and tool in hardened["tools"]
    assert spec["interrupt_on"]  # original spec remains intact: negative control
    names = [type(m).__name__ for m in hardened.get("middleware", [])]
    assert "CoworkBrokerMiddleware" not in names and not any("Approval" in name for name in names)
    return "Subagent hardening keeps write tools but removes HITL and adds no approval/Cowork broker."


def secret_permissions():
    if os.name == "nt":
        return None, "POSIX permission reproduction skipped on Windows; implementation reviewed in report."
    from novacode_cli.onboarding import _atomic_write_json

    with fixture() as directory:
        path = directory / "secrets.json"
        path.write_text("{}")
        path.chmod(0o600)
        previous = os.umask(0o022)
        try:
            _atomic_write_json(path, {"dummy": "audit-only"})
        finally:
            os.umask(previous)
        assert path.stat().st_mode & 0o777 == 0o644
    return True, "Atomic fallback-secret replacement changes 0600 to 0644 under umask 022."


def prepare_dependencies(destination=None):
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    registry = sorted({(p["name"], p["version"]) for p in lock["package"] if "registry" in p["source"]})
    excluded = [{"name": p["name"], "source_kind": list(p["source"])}
                for p in lock["package"] if "registry" not in p["source"]]
    destination = destination or ROOT / "docs" / "security-audit"
    destination.mkdir(parents=True, exist_ok=True)
    # Some platform/Python branches resolve different versions of one package.
    # pip-audit rejects duplicate names in one file; audit EVERY version by
    # partitioning them into files with unique names instead of dropping any.
    by_name = {}
    for name, version in registry:
        by_name.setdefault(name, []).append(version)
    files = []
    for index in range(max(map(len, by_name.values()), default=0)):
        filename = "locked-requirements.txt" if index == 0 else f"locked-requirements-{index + 1}.txt"
        lines = [f"{name}=={versions[index]}" for name, versions in by_name.items() if len(versions) > index]
        (destination / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")
        files.append({"file": filename, "version_count": len(lines)})
    (destination / "dependency-scope.json").write_text(json.dumps({
        "registry_version_count": len(registry), "requirements_files": files, "excluded": excluded,
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "scope": "all locked registry versions, including dev and optional dependencies",
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"prepared_registry_versions": len(registry), "excluded": excluded}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-dependencies", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dependency-dir", type=Path)
    args = parser.parse_args()
    if args.prepare_dependencies:
        prepare_dependencies(args.dependency_dir)
        return 0
    probes = {
        "S01-shell-prefix": shell_prefix,
        "S02-project-policy": project_policy,
        "S03-cowork-shell": cowork_shell,
        "S04-wiki-traversal": wiki_traversal,
        "S05-plugin-parent": plugin_parent,
        "S06-browser-apis": browser_apis,
        "S07-cowork-xss": cowork_xss,
        "S08-autoapprove-denies": autoapprove_denies,
        "S09-redirect-policy": redirect_policy,
        "S10-subagent-approvals": subagent_approvals,
        "S11-secret-permissions": secret_permissions,
    }
    findings = []
    for name, probe in probes.items():
        try:
            result = probe()
            reproduced, detail = result if isinstance(result, tuple) else (True, result)
            status = "reproduced" if reproduced else "skipped"
        except Exception as error:
            status, detail = "error", f"{type(error).__name__}: {error}"
        findings.append({"id": name, "status": status, "detail": detail})
        print(f"{name}: {status}: {detail}")
    report = {"findings": findings, "meaning": "reproduced means vulnerable; errors are unresolved checks"}
    if args.output:
        output = args.output.resolve()
        if not output.is_relative_to(ROOT):
            raise ValueError("Evidence output must be inside the repository")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if any(f["status"] == "error" for f in findings):
        return 2
    return 1 if any(f["status"] == "reproduced" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
