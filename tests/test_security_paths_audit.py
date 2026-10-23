"""Attack regressions and permitted controls for portable path boundaries."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from novacode_cli.integrations.host_path import host_path_to_virtual
from novacode_cli.utils.backend_paths import virtual_to_real_path


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    st.lists(
        st.sampled_from(["src", "notes", "version..txt", "file.md", "caf\u00e9"]),
        min_size=1,
        max_size=5,
    )
)
def test_generated_relative_paths_keep_the_same_destination(tmp_path, parts):
    assert virtual_to_real_path(
        "/" + "/".join(parts), workspace_root=tmp_path
    ) == tmp_path.joinpath(*parts)


def _directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Symlink creation unavailable")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )


@pytest.mark.parametrize(
    "path",
    [
        "/../outside",
        "/a/../../outside",
        "/a\\..\\..\\outside",
        "/C:/outside",
        "//server/share/file",
        "/nul\x00name",
    ],
)
def test_virtual_conversion_rejects_escape(tmp_path, path):
    assert virtual_to_real_path(path, workspace_root=tmp_path) is None


def test_virtual_conversion_confines_each_mount(tmp_path):
    mount = tmp_path / "memory"
    mount.mkdir()
    backend = SimpleNamespace(
        default=SimpleNamespace(root_dir=tmp_path),
        routes={"/memories/": SimpleNamespace(root_dir=mount, virtual_mode=True)},
    )
    assert virtual_to_real_path("/memories/../outside", backend=backend) is None
    assert virtual_to_real_path("/memories/notes.md", backend=backend) == mount / "notes.md"


def test_virtual_conversion_does_not_ignore_backend_denial(tmp_path):
    def reject(key):
        raise ValueError("denied")

    sub = SimpleNamespace(root_dir=tmp_path, virtual_mode=True, _resolve_path=reject)
    backend = SimpleNamespace(default=sub, routes={})
    assert virtual_to_real_path("/notes.md", backend=backend, workspace_root=tmp_path) is None


def test_virtual_conversion_uses_explicit_routes_not_search_fanout(tmp_path):
    from deepagents.backends.composite import CompositeBackend

    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

    mount = tmp_path / "memory"
    backend = CompositeBackend(
        default=OptimizedFilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        routes={"/memories/": OptimizedFilesystemBackend(root_dir=mount, virtual_mode=True)},
    )
    backend.routes = {}  # Nova hides non-project mounts from pathless search.
    assert virtual_to_real_path("/memories/notes.md", backend=backend) == mount / "notes.md"


def test_virtual_conversion_blocks_link_escape(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _directory_link(root / "link", outside)
    assert virtual_to_real_path("/link/secret", workspace_root=root) is None


@pytest.mark.parametrize("root", ["C:/project", "/home/project"])
def test_host_rewriting_does_not_retarget_parent_traversal(root):
    attack = root + "/../outside/file"
    assert host_path_to_virtual(attack, root) == attack
    assert host_path_to_virtual(root + "/src/file", root) == "/src/file"


def test_mount_rewriting_does_not_cross_mount_boundary():
    attack = "C:/skills/../secrets/file"
    assert host_path_to_virtual(attack, "C:/project", [("C:/skills", "/skills/")]) == attack


@pytest.mark.parametrize(
    "key", ["/C:/outside", "C:outside", "//server/share/file", "/\\server\\share\\file"]
)
def test_optimized_virtual_backend_rejects_foreign_anchors(tmp_path, key):
    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)
    with pytest.raises(ValueError):
        backend._resolve_path(key)


def test_optimized_virtual_backend_allows_dotted_names(tmp_path):
    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

    backend = OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)
    assert backend._resolve_path("/version..txt") == tmp_path / "version..txt"


@pytest.mark.skipif(os.name != "nt", reason="Windows device names / alternate streams")
@pytest.mark.parametrize("key", ["/file.txt:stream", "/CON", "/nul.txt", "/dir/PRN"])
def test_optimized_virtual_backend_blocks_windows_special_files(tmp_path, key):
    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend

    with pytest.raises(ValueError):
        OptimizedFilesystemBackend(str(tmp_path), virtual_mode=True)._resolve_path(key)


def test_memory_tool_traversal_never_writes_outside(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings
    from novacode_cli.tools.memory_tools import write_memory

    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(settings, "get_agent_dir", lambda _: memory)
    result = write_memory.invoke({"path": "/memories/../outside.md", "content": "escape"})
    assert result["success"] is False
    assert not (tmp_path / "outside.md").exists()


@pytest.mark.parametrize(
    "tool,arg", [("write_file", "file_path"), ("edit_file", "file_path"), ("write_memory", "path")]
)
def test_write_policy_normalizes_parent_aliases(tool, arg, tmp_path):
    from novacode_cli.security.policy import ApprovalPolicy

    denied = tmp_path / "blocked"
    policy = ApprovalPolicy({}, [], [], ["**"], [denied.as_posix() + "/**"], [], [])
    alias = str(tmp_path / "src" / ".." / "blocked" / "secret")
    assert policy.evaluate(tool, {arg: alias}).denied
    assert policy.evaluate(tool, {arg: str(tmp_path / "allowed")}).allowed


def test_write_policy_blocks_link_target(tmp_path):
    from novacode_cli.security.policy import ApprovalPolicy

    outside = tmp_path / "blocked"
    outside.mkdir()
    link = tmp_path / "linked"
    _directory_link(link, outside)
    policy = ApprovalPolicy({}, [], [], ["**"], [outside.as_posix() + "/**"], [], [])
    assert policy.evaluate("write_file", {"file_path": str(link / "secret")}).denied


def test_recursive_root_approval_includes_descendants(tmp_path, monkeypatch):
    from novacode_cli.path_approval import PathApprovalManager

    monkeypatch.setattr(
        PathApprovalManager,
        "_load_approved_paths",
        lambda self: {str(Path(tmp_path.anchor)): {"recursive": True}},
    )
    assert PathApprovalManager().is_path_approved(tmp_path / "file")


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")
def test_policy_blocks_case_alias_and_alternate_stream(tmp_path):
    from novacode_cli.security.policy import ApprovalPolicy

    policy = ApprovalPolicy({}, [], [], ["**"], [str(tmp_path / "Blocked" / "secret")], [], [])
    assert policy.evaluate(
        "write_file", {"file_path": str(tmp_path / "blocked" / "secret:stream")}
    ).denied


def test_policy_checks_default_memory_destination(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings
    from novacode_cli.security.policy import ApprovalPolicy

    monkeypatch.setattr(settings, "get_agent_dir", lambda _: tmp_path)
    policy = ApprovalPolicy({}, [], [], [], [(tmp_path / "agent.md").as_posix()], [], [])
    assert policy.evaluate("write_memory", {}).denied


def test_policy_checks_virtual_link_target(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings
    from novacode_cli.security.policy import ApprovalPolicy

    workspace, outside = tmp_path / "workspace", tmp_path / "blocked"
    workspace.mkdir()
    outside.mkdir()
    _directory_link(workspace / "link", outside)
    monkeypatch.setattr(settings, "get_workspace_root", lambda: workspace)
    policy = ApprovalPolicy({}, [], [], ["**"], [outside.as_posix() + "/**"], [], [])
    assert policy.evaluate("write_file", {"file_path": "/link/secret"}).denied


def test_team_memory_write_has_an_external_destination(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings
    from novacode_cli.security.policy import ApprovalPolicy
    from novacode_cli.teams.workspace import resolve_write_paths, write_boundary

    workspace = tmp_path / "project"
    memory = tmp_path / "memory"
    target = memory / "topic.md"
    monkeypatch.setattr(settings, "get_agent_dir", lambda _: memory)
    args = {"path": "/memories/topic.md"}
    assert resolve_write_paths("write_memory", args, workspace) == [target]
    assert write_boundary(
        "write_memory", args, workspace, [str(target)], ApprovalPolicy({}, [], [], [], [], [], [])
    ) == ([target], True)


def test_approval_rejects_malformed_recursion(tmp_path, monkeypatch):
    from novacode_cli.path_approval import PathApprovalManager

    monkeypatch.setattr(
        PathApprovalManager,
        "_load_approved_paths",
        lambda self: {str(tmp_path): {"recursive": "false"}},
    )
    assert not PathApprovalManager().is_path_approved(tmp_path / "child")


@pytest.mark.skipif(os.name != "nt", reason="Windows saved approval casing")
def test_approval_revocation_uses_case_normalized_path(tmp_path, monkeypatch):
    from novacode_cli.path_approval import PathApprovalManager

    monkeypatch.setattr(
        PathApprovalManager,
        "_load_approved_paths",
        lambda self: {str(tmp_path).upper(): {"recursive": True}},
    )
    monkeypatch.setattr(PathApprovalManager, "_save_approved_paths", lambda self: None)
    manager = PathApprovalManager()
    assert manager.is_path_approved(tmp_path / "child")
    assert manager.revoke_path(tmp_path)
    assert not manager.is_path_approved(tmp_path / "child")
