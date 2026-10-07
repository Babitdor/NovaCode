"""CoworkBrokerMiddleware: the LIVE agent's file/shell tools are authorized
against WorkspacePolicy before executing — denied calls never run."""

from __future__ import annotations

from pathlib import Path

import pytest


class _Req:
    def __init__(self, name, **args):
        self.tool_call = {"name": name, "id": "c1", "args": args}


@pytest.fixture
def setup(tmp_path: Path):
    import novacode_cli.cowork.policy as P

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x")
    pol = P.WorkspacePolicy(store_path=tmp_path / "store.json")
    P._policy = pol  # the middleware uses get_policy()

    from novacode_cli.cowork.broker_middleware import CoworkBrokerMiddleware

    mw = CoworkBrokerMiddleware(root)
    return mw, pol, root, tmp_path


def _run(mw, req):
    """Return ('ALLOWED', handler-result) or ('DENIED', ToolMessage)."""
    calls = []

    def handler(_r):
        calls.append(True)
        return "RAN"

    out = mw.wrap_tool_call(req, handler)
    if calls:
        return "ALLOWED", out
    return "DENIED", out


def test_denied_by_default(setup):
    mw, _pol, _root, _ = setup
    verdict, out = _run(mw, _Req("read_file", file_path="/src/a.py"))
    assert verdict == "DENIED"
    assert "DENIED" in out.content and "ACCESS_DENIED_OUTSIDE_WORKSPACE" in out.content


def test_read_allowed_after_grant(setup):
    mw, pol, root, _ = setup
    pol.grant(root, read=True, write=True, execute=False)
    assert _run(mw, _Req("read_file", file_path="/src/a.py"))[0] == "ALLOWED"
    assert _run(mw, _Req("write_file", file_path="/src/a.py", content="y"))[0] == "ALLOWED"


def test_execute_scope_enforced(setup):
    mw, pol, root, _ = setup
    pol.grant(root, read=True, write=True, execute=False)  # no execute
    v, out = _run(mw, _Req("shell", command="ls"))
    assert v == "DENIED"
    # granting execute flips it (a fresh middleware picks up the same global policy)
    pol.grant(root, read=False, write=False, execute=True)
    assert _run(mw, _Req("execute", command="ls"))[0] == "DENIED"


def test_traversal_and_absolute_escape_denied(setup):
    mw, pol, root, tmp_path = setup
    pol.grant(root, read=True, write=True)
    assert _run(mw, _Req("read_file", file_path="/../secret/creds"))[0] == "DENIED"
    assert _run(mw, _Req("read_file", file_path=str(tmp_path / "elsewhere.txt")))[0] == "DENIED"


def test_move_gates_destination(setup):
    mw, pol, root, tmp_path = setup
    pol.grant(root, read=True, write=True)
    # moving out of the workspace is denied on the destination
    v, _ = _run(mw, _Req("edit_file", file_path="/src/a.py", new_path=str(tmp_path / "out.py")))
    assert v == "DENIED"


def test_non_filesystem_tool_passes_through(setup):
    mw, _pol, _root, _ = setup  # no grant at all
    assert _run(mw, _Req("web_search", query="hello"))[0] == "ALLOWED"
