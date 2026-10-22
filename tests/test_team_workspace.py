"""Project write boundaries must survive delegated auto-approval."""

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from novacode_cli.teams.workspace import resolve_write_paths


def test_virtual_relative_and_absolute_project_paths(tmp_path: Path):
    for raw in ("notes/a.md", "/notes/a.md", str(tmp_path / "notes/a.md")):
        assert resolve_write_paths("write_file", {"file_path": raw}, tmp_path) == [
            tmp_path / "notes/a.md"
        ]


def test_traversal_keeps_its_external_destination(tmp_path: Path):
    assert resolve_write_paths("edit_file", {"file_path": "../outside.md"}, tmp_path) == [
        tmp_path.parent / "outside.md"
    ]


def test_reads_have_no_write_destinations(tmp_path: Path):
    assert resolve_write_paths("read_file", {"file_path": "../a"}, tmp_path) == []


def test_missing_write_path_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="path"):
        resolve_write_paths("write_file", {}, tmp_path)


def test_existing_symlink_resolves_outside(tmp_path: Path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    try:
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    assert resolve_write_paths("write_file", {"file_path": "/link/a.md"}, tmp_path) == [
        outside / "a.md"
    ]


async def test_mounted_external_write_does_not_execute_without_approval(tmp_path: Path):
    from types import SimpleNamespace

    from deepagents.backends import CompositeBackend

    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend
    from novacode_cli.teams.gateway import Binding
    from novacode_cli.teams.middleware import TeamMiddleware
    from novacode_cli.teams.models import Identity, operation_digest

    outside = tmp_path / "external"
    project = tmp_path / "project"
    outside.mkdir()
    project.mkdir()
    backend = CompositeBackend(
        default=OptimizedFilesystemBackend(root_dir=project, virtual_mode=True),
        routes={"/memories/": OptimizedFilesystemBackend(root_dir=outside, virtual_mode=True)},
    )
    seen = []

    async def endpoint(_method: str, payload: dict) -> dict:
        seen.append(payload)
        return {"allowed": False, "reason": "outside project"}

    middleware = TeamMiddleware(
        Binding(Identity("p", "r", "worker", "t"), endpoint), workspace=project
    )
    middleware.backend = backend
    call = {"name": "write_file", "args": {"file_path": "/memories/report.md"}, "id": "write"}
    invoked = False

    async def handler(request: SimpleNamespace) -> str:
        nonlocal invoked
        invoked = True
        backend.write(request.tool_call["args"]["file_path"], "approved content")
        return "done"

    result = await middleware.awrap_tool_call(SimpleNamespace(tool_call=call), handler)
    assert result.status == "error"
    assert seen[0]["write_paths"] == [str(outside / "report.md")]
    assert not invoked
    assert not (outside / "report.md").exists()

    async def approve(_method: str, payload: dict) -> dict:
        return {
            "allowed": True,
            "digest": operation_digest(payload["tool"], payload["args"], payload["tool_call_id"]),
        }

    middleware.binding = Binding(Identity("p", "r", "worker", "t"), approve)
    assert await middleware.awrap_tool_call(SimpleNamespace(tool_call=call), handler) == "done"
    assert invoked
    assert (outside / "report.md").read_text() == "approved content"


@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.lists(st.text(alphabet="abcdef012345", min_size=1, max_size=8), min_size=1, max_size=4))
def test_generated_project_paths_never_depend_on_process_cwd(tmp_path: Path, parts: list[str]):
    expected = tmp_path.joinpath(*parts).with_suffix(".md")
    raw = "/" + "/".join(parts) + ".md"
    assert resolve_write_paths("write_file", {"file_path": raw}, tmp_path) == [expected]


def test_write_boundary_uses_worker_root_and_rejects_invalid_envelopes(tmp_path: Path):
    from novacode_cli.security.policy import ApprovalPolicy
    from novacode_cli.teams.workspace import write_boundary

    policy = ApprovalPolicy({}, [], [], [], [], [], [])
    worktree = tmp_path / "worker"
    target = worktree / "notes.md"
    args = {"file_path": "/notes.md"}
    assert write_boundary("write_file", args, worktree, [str(target)], policy) == ([target], False)
    for invalid in ([], ["relative/path"], "path", [1]):
        with pytest.raises(ValueError, match="write paths"):
            write_boundary("write_file", args, worktree, invalid, policy)
    policy.path_deny.append(target.as_posix())
    with pytest.raises(ValueError, match="denied"):
        write_boundary("write_file", args, worktree, [str(target)], policy)


def test_store_route_without_host_path_fails_closed(tmp_path: Path):
    from types import SimpleNamespace

    backend = SimpleNamespace(_get_backend_and_key=lambda _raw: (object(), "/item"))
    with pytest.raises(ValueError, match="Cannot resolve"):
        resolve_write_paths("write_file", {"file_path": "/store/item"}, tmp_path, backend)


async def test_changed_destination_during_approval_never_executes(tmp_path: Path):
    from types import SimpleNamespace

    from novacode_cli.teams.gateway import Binding
    from novacode_cli.teams.middleware import TeamMiddleware
    from novacode_cli.teams.models import Identity, operation_digest

    destination = tmp_path / "before.md"

    def resolve(_key: str) -> Path:
        return destination

    backend = SimpleNamespace(
        _get_backend_and_key=lambda _raw: (SimpleNamespace(_resolve_path=resolve), "/file")
    )

    async def approve(_method: str, payload: dict) -> dict:
        nonlocal destination
        destination = tmp_path / "changed.md"
        return {
            "allowed": True,
            "digest": operation_digest(payload["tool"], payload["args"], payload["tool_call_id"]),
        }

    middleware = TeamMiddleware(
        Binding(Identity("p", "r", "worker", "t"), approve), workspace=tmp_path
    )
    middleware.backend = backend

    async def handler(_request: SimpleNamespace) -> str:
        pytest.fail("Changed destination must never execute")

    result = await middleware.awrap_tool_call(
        SimpleNamespace(
            tool_call={
                "name": "write_file",
                "args": {"file_path": "/file"},
                "id": "changed",
            }
        ),
        handler,
    )
    assert result.status == "error"
    assert "destination changed" in result.content


@pytest.mark.parametrize("fail_after_approval", [False, True])
async def test_unresolvable_destination_never_executes(
    tmp_path: Path, *, fail_after_approval: bool,
):
    from types import SimpleNamespace

    from novacode_cli.teams.gateway import Binding
    from novacode_cli.teams.middleware import TeamMiddleware
    from novacode_cli.teams.models import Identity, operation_digest

    approved = False

    def resolve(_key: str) -> Path:
        if not fail_after_approval or approved:
            message = "Unavailable path"
            raise OSError(message)
        return tmp_path / "a.md"

    async def endpoint(_method: str, payload: dict) -> dict:
        nonlocal approved
        approved = True
        return {
            "allowed": True,
            "digest": operation_digest(payload["tool"], payload["args"], payload["tool_call_id"]),
        }

    middleware = TeamMiddleware(
        Binding(Identity("p", "r", "worker", "t"), endpoint), workspace=tmp_path
    )
    middleware.backend = SimpleNamespace(
        _get_backend_and_key=lambda _raw: (SimpleNamespace(_resolve_path=resolve), "/a")
    )

    async def handler(_request: SimpleNamespace) -> str:
        pytest.fail("An unresolved write must never run")

    result = await middleware.awrap_tool_call(
        SimpleNamespace(
            tool_call={
                "name": "write_file",
                "args": {"file_path": "/a"},
                "id": "unresolved",
            }
        ),
        handler,
    )
    assert result.status == "error"
    assert approved == fail_after_approval
