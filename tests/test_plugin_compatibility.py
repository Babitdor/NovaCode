"""Standard directory plugins: lifecycle, ownership and executable boundaries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from novacode_cli.plugins import claude_plugins as cp
from novacode_cli.plugins import marketplaces as mp
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401

if TYPE_CHECKING:
    from novacode_cli.plugins.components import Plugin

Store = tuple[Path, Path, Path]


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(cp, "PLUGINS_DIR", home / "plugins")
    monkeypatch.setattr(cp, "MANIFEST", home / "plugins" / "installed.json")
    monkeypatch.setattr(mp, "MARKETPLACES_DIR", home / "marketplaces")
    monkeypatch.setattr(mp, "MANIFEST", home / "marketplaces" / "marketplaces.json")
    monkeypatch.setattr(cp, "project_root", lambda: project, raising=False)
    return tmp_path, home, project


def bundle(base: Path, name: str = "demo", **manifest: object) -> Path:
    root = base / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0", **manifest}), encoding="utf-8"
    )
    (root / "skills/review").mkdir(parents=True)
    (root / "skills/review/SKILL.md").write_text(
        "---\nname: review\ndescription: Review safely\n---\nRead first.", encoding="utf-8"
    )
    (root / "commands").mkdir()
    (root / "commands/check.md").write_text("Check $ARGUMENTS", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents/reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Check code\n---\nRead only.", encoding="utf-8"
    )
    return root


def test_namespaces_and_disable_remove_every_component(store: Store):
    source = bundle(store[0])
    cp.install(str(source))
    assert "demo:check" in {name for name, _, _ in cp.plugin_commands()}
    assert cp.plugin_agent_specs()[0]["name"] == "demo:reviewer"
    cp.set_enabled("demo", enabled=False)
    assert cp.plugin_commands() == []
    assert cp.plugin_skill_dirs() == []
    assert cp.plugin_agent_specs() == []
    cp.set_enabled("demo", enabled=True)
    assert cp.plugin_commands()


def test_scope_precedence_and_install_does_not_touch_mcp_or_hooks(store: Store):
    source = bundle(store[0])
    (source / ".mcp.json").write_text('{"mcpServers":{"api":{"url":"https://example.com/mcp"}}}')
    cp.install(str(source), scope="user")
    cp.set_enabled("demo", enabled=False, scope="project")
    assert not cp.list_plugins()[0]["enabled"]
    cp.set_enabled("demo", enabled=True, scope="local")
    assert cp.list_plugins()[0]["enabled"]
    assert not (store[1] / "mcp.json").exists()
    assert not (store[1] / "hooks.json").exists()
    assert json.loads((store[2] / ".nova/settings.local.json").read_text())["enabledPlugins"][
        "demo"
    ]


def test_failed_update_preserves_previous_snapshot(store: Store):
    source = bundle(store[0])
    cp.install(str(source))
    before = cp.list_plugins()[0]["path"]
    (source / ".claude-plugin/plugin.json").write_text("{broken")
    with pytest.raises(ValueError, match=r".+"):
        cp.update("demo")
    assert cp.list_plugins()[0]["path"] == before
    assert cp.plugin_commands()


def test_manifest_paths_and_inline_components(store: Store):
    source = bundle(
        store[0],
        commands={"hello": {"content": "Hello $ARGUMENTS"}},
        mcpServers={"remote": {"type": "http", "url": "https://example.com/mcp"}},
    )
    cp.install(str(source))
    assert [name for name, _, _ in cp.plugin_commands()] == ["demo:hello", "demo:review"]
    assert "demo__remote" in cp.plugin_mcp_servers()


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:\\outside", "./../outside"])
def test_component_path_escape_fails_before_install(store: Store, path: str):
    source = bundle(store[0], commands=path)
    with pytest.raises(ValueError, match=r".+"):
        cp.install(str(source))
    assert cp.list_plugins() == []


def test_marketplace_relative_source_cannot_escape(store: Store):
    with pytest.raises(ValueError, match=r".+"):
        mp._resolve_entry_source({"name": "demo", "source": "../outside"}, store[0])


def test_concurrent_installs_preserve_both_records(store: Store):
    sources = [bundle(store[0], name) for name in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert set(workers.map(lambda source: cp.install(str(source)), sources)) == {"one", "two"}
    assert {p["name"] for p in cp.list_plugins()} == {"one", "two"}


def test_legacy_install_remains_discoverable(store: Store):
    assert store[1].is_relative_to(store[0])
    root = bundle(cp.PLUGINS_DIR)
    cp.MANIFEST.write_text(json.dumps({"demo": {"source": str(root), "enabled": True}}))
    assert cp.list_plugins()[0]["path"] == str(root)
    assert cp.plugin_commands()[0][0] == "demo:check"


def test_two_marketplaces_keep_distinct_identities(store: Store):
    source = bundle(store[0])
    cp.install(str(source), marketplace="first")
    cp.install(str(source), marketplace="second")
    assert {p["id"] for p in cp.list_plugins()} == {"demo@first", "demo@second"}


def test_unknown_marketplace_source_type_is_rejected(store: Store):
    with pytest.raises(ValueError, match=r".+"):
        mp._resolve_entry_source(
            {"name": "demo", "source": {"source": "unknown", "repo": "o/r"}}, store[0]
        )


def hook_bundle(
    store: Store,
    *,
    event: str = "PreToolUse",
    matcher: str = "*",
    response: dict | None = None,
    exit_code: int = 0,
) -> Path:
    import sys

    source = bundle(store[0])
    script = source / "probe.py"
    script.write_text(
        "import json, os, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert request['hook_event_name']\n"
        f"assert os.path.samefile(os.getcwd(), {str(store[2])!r})\n"
        "assert os.path.isdir(os.environ['CLAUDE_PLUGIN_ROOT'])\n"
        f"print({json.dumps(json.dumps(response or {}))})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    (source / "hooks").mkdir()
    (source / "hooks/hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "matcher": matcher,
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": sys.executable,
                                    "args": ["${CLAUDE_PLUGIN_ROOT}/probe.py"],
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cp.install(str(source))
    return source


def test_actual_hook_process_receives_protocol_and_blocks(store: Store):
    from novacode_cli.plugins.hooks import execute_hooks

    hook_bundle(
        store,
        matcher="Bash",
        response={
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "fixture denial",
            }
        },
    )
    assert execute_hooks("PreToolUse", {"tool_name": "read_file"}).denial == ""
    assert "fixture denial" in execute_hooks("PreToolUse", {"tool_name": "execute"}).denial
    cp.set_enabled("demo", enabled=False)
    assert execute_hooks("PreToolUse", {"tool_name": "execute"}).denial == ""


def test_hook_context_reaches_request_copy_only(store: Store):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import HumanMessage, SystemMessage

    from novacode_cli.plugins.hooks import PluginHooksMiddleware

    hook_bundle(
        store,
        event="SessionStart",
        response={
            "hookSpecificOutput": {
                "additionalContext": "Published fixture finding",
            }
        },
    )
    middleware = PluginHooksMiddleware()
    update = middleware.before_agent({"messages": [HumanMessage("hello")]}, None)
    original = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=[HumanMessage("hello")],
        state=update,
        system_message=SystemMessage("Keep owner permissions."),
    )
    prepared = middleware._prepare(original)
    assert "Published fixture finding" in prepared.system_message.text
    assert original.system_message.text == "Keep owner permissions."
    assert prepared.messages == original.messages


async def test_hook_denial_prevents_sync_and_async_tool_execution(store: Store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from novacode_cli.plugins.hooks import PluginHooksMiddleware

    hook_bundle(store, response={"continue": False, "stopReason": "Denied"})
    request = SimpleNamespace(tool_call={"name": "execute", "id": "call", "args": {}})
    middleware = PluginHooksMiddleware()
    handler = Mock()
    assert middleware.wrap_tool_call(request, handler).status == "error"
    handler.assert_not_called()
    async_handler = AsyncMock()
    assert (await middleware.awrap_tool_call(request, async_handler)).status == "error"
    async_handler.assert_not_awaited()


def test_hook_allow_still_calls_normal_policy_handler(store: Store):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from langchain_core.messages import ToolMessage

    from novacode_cli.plugins.hooks import PluginHooksMiddleware

    hook_bundle(store, response={"hookSpecificOutput": {"permissionDecision": "allow"}})
    request = SimpleNamespace(tool_call={"name": "execute", "id": "call", "args": {}})
    blocked = ToolMessage("Owner policy denial", tool_call_id="call", status="error")
    handler = Mock(return_value=blocked)
    assert PluginHooksMiddleware().wrap_tool_call(request, handler) is blocked
    handler.assert_called_once_with(request)


def test_plan_mode_never_executes_plugin_commands(store: Store, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from novacode_cli.config.plan_mode import active_plan_session
    from novacode_cli.plugins import hooks

    hook_bundle(store)
    run = Mock(side_effect=AssertionError("Must not execute"))
    monkeypatch.setattr(hooks, "_run", run)
    token = active_plan_session.set(SimpleNamespace(plan_mode_enabled=True))
    try:
        assert hooks.execute_hooks("PreToolUse", {"tool_name": "read_file"}).denial == ""
    finally:
        active_plan_session.reset(token)
    run.assert_not_called()


def test_runtime_mcp_overlay_is_removed_on_disable_without_config_mutation(store: Store):
    from novacode_cli.mcp.config import MCPConfig

    source = bundle(
        store[0], mcpServers={"api": {"type": "http", "url": "https://example.com/mcp"}}
    )
    cp.install(str(source))
    configuration = MCPConfig(store[1] / "mcp.json")
    configuration._include_plugins = True
    assert configuration.list_servers()["demo__api"].url == "https://example.com/mcp"
    cp.set_enabled("demo", enabled=False)
    assert configuration.list_servers() == {}
    assert not configuration.config_path.exists()


def test_namespaced_skill_metadata_in_agent_and_invoker(store: Store):
    from novacode_cli.skills.load import list_skills

    cp.install(str(bundle(store[0])))
    directories = [directory for _, directory in cp.plugin_skill_dirs()]
    assert list_skills(plugin_skills_dirs=directories)[0]["name"] == "demo:review"


def test_marketplace_install_records_identity_and_update_uses_refreshed_catalog(store: Store):
    root = store[0] / "catalog"
    (root / ".claude-plugin").mkdir(parents=True)
    plugin = bundle(root)
    (root / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "name": "example",
                "plugins": [{"name": "demo", "source": "./demo"}],
            }
        )
    )
    assert mp.add(str(root)) == "example"
    assert mp.install_plugin("demo@example") == "demo@example"
    before = cp.list_plugins()[0]["path"]
    (plugin / "commands/check.md").write_text("Updated command")
    mp.update_marketplace("example")
    cp.update("demo@example")
    assert cp.list_plugins()[0]["path"] != before
    assert Path(before).is_dir()
    assert cp.plugin_commands()[0][2] == "Updated command"


def test_shell_service_and_quoted_windows_arguments(store: Store):
    from novacode_cli.commands.plugin_install_handler import command_arguments
    from novacode_cli.plugins.cli import execute

    source = bundle(store[0])
    assert command_arguments('install "C:\\My Project\\plugin" --scope local') == [
        "install",
        "C:\\My Project\\plugin",
        "--scope",
        "local",
    ]
    assert execute(["install", str(source), "--scope", "local"]).changed
    assert "local" in execute(["list"]).lines[0]
    assert execute(["disable", "demo", "--scope", "local"]).changed
    assert not cp.plugin_commands()
    assert execute(["enable", "demo", "--scope", "local"]).changed
    assert execute(["validate", str(source)]).report
    assert execute(["uninstall", "demo", "--scope", "local"]).changed
    assert cp.list_plugins() == []


async def test_plugin_manager_tabs_and_transient_operation_feedback(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import Mock

    from textual.widgets import Tabs

    from novacode_cli.tui.screens import ClaudePluginsScreen
    from tests.test_tui_home_preferences import _app

    cp.install(str(bundle(store[0])))
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=(60, 30)) as pilot:
        await app.push_screen(ClaudePluginsScreen())
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        listing = app.screen.query_one("#cplugins-list")
        assert listing.option_count == 1
        app.screen.query_one("#plugin-tabs", Tabs).active = "discover"
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        assert listing.option_count == 0
        app.screen.query_one("#plugin-tabs", Tabs).active = "marketplaces"
        await pilot.pause()
        assert app.screen._view == "marketplaces"
        await pilot.press("escape")
        log = Mock()
        monkeypatch.setattr(app, "_log", log)
        await app._run_plugins("/plugin disable demo")
        log.assert_not_called()


def test_custom_skill_roots_are_namespaced_before_agent_deduplication(store: Store):
    from deepagents.backends import CompositeBackend

    from novacode_cli.backends import OptimizedFilesystemBackend
    from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware

    source = bundle(store[0], skills=["./custom/special"])
    (source / "custom/special").mkdir(parents=True)
    (source / "custom/special/SKILL.md").write_text(
        "---\nname: review\ndescription: A second review\n---\nSpecial review."
    )
    cp.install(str(source))
    roots = cp.plugin_skill_dirs()
    backend = CompositeBackend(
        default=OptimizedFilesystemBackend(root_dir=store[0], virtual_mode=True),
        routes={
            f"/plugin-skills-{name}/": OptimizedFilesystemBackend(root_dir=path, virtual_mode=True)
            for name, path in roots
        },
    )
    middleware = RefreshingSkillsMiddleware(
        backend=backend,
        sources=[f"/plugin-skills-{name}/" for name, _ in roots],
        watch_dirs=[path for _, path in roots],
    )
    middleware.before_agent({}, None, {})
    assert {s["name"] for s in middleware._skills} == {"demo:review", "demo:special"}


def test_local_development_bundle_is_session_only(store: Store):
    source = bundle(store[0])
    cp.set_session_plugins([str(source)])
    assert cp.plugin_commands()[0][0] == "demo:check"
    assert cp.list_plugins() == []
    assert not cp.MANIFEST.exists()
    cp.set_session_plugins([])
    assert cp.plugin_commands() == []


def test_failed_registry_publish_restores_activation_settings(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import Mock

    source = bundle(store[0])
    cp.install(str(source))
    cp.set_enabled("demo", enabled=False)
    before = (store[1] / "settings.json").read_bytes()
    monkeypatch.setattr(cp, "_save_manifest", Mock(side_effect=OSError("fixture disk failure")))
    with pytest.raises(OSError, match="fixture disk failure"):
        cp.install(str(source), enable=True)
    assert (store[1] / "settings.json").read_bytes() == before
    assert not cp.list_plugins()[0]["enabled"]


def test_legacy_mcp_overlay_preserves_owner_edits(store: Store):
    from novacode_cli.mcp.config import MCPConfig, MCPServerConfig

    root = bundle(cp.PLUGINS_DIR)
    (root / ".mcp.json").write_text('{"mcpServers":{"api":{"url":"https://original.example/mcp"}}}')
    cp.MANIFEST.write_text(json.dumps({"demo": {"source": str(root), "enabled": False}}))
    config = MCPConfig(store[1] / "mcp.json")
    config._include_plugins = True
    config.save(
        {"demo__api": MCPServerConfig(transport="http", url="https://original.example/mcp")}
    )
    assert config.list_servers() == {}
    config.save({"demo__api": MCPServerConfig(transport="http", url="https://owner.example/mcp")})
    assert config.list_servers()["demo__api"].url == "https://owner.example/mcp"


def test_catalog_entry_metadata_and_alias_are_retained(store: Store):
    source = bundle(store[0])
    identity = cp.install(
        str(source),
        marketplace="example",
        expected_name="alias",
        entry={
            "name": "alias",
            "defaultEnabled": False,
        },
    )
    assert identity == "alias@example"
    assert not cp.list_plugins()[0]["enabled"]
    cp.set_enabled(identity, enabled=True)
    assert cp.plugin_commands()[0][0] == "demo:check"


def test_catalog_and_manifest_conflict_rejected_before_publication(store: Store):
    source = bundle(store[0])
    with pytest.raises(ValueError, match="conflicting manifests"):
        cp.install(str(source), entry={"strict": False, "commands": "./commands"})
    assert cp.list_plugins() == []


@pytest.mark.parametrize(
    "manifest",
    [[], {"name": "demo", "commands": 42}, {"name": "demo", "defaultEnabled": "yes"}, {}],
)
def test_invalid_manifest_never_publishes_an_install(store: Store, manifest: object):
    source = bundle(store[0])
    (source / ".claude-plugin/plugin.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r".+"):
        cp.install(str(source))
    assert cp.list_plugins() == []


def test_source_relative_path_is_stable_across_working_directories(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    source = bundle(store[0])
    monkeypatch.chdir(source.parent)
    cp.install("./demo")
    monkeypatch.chdir(store[2])
    cp.update("demo")
    assert cp.plugin_commands()


def test_hook_exit_error_and_missing_interpreter_are_blocked(store: Store):
    from novacode_cli.plugins.hooks import execute_hooks

    source = hook_bundle(store, exit_code=2)
    assert "exited 2" in execute_hooks("PreToolUse", {"tool_name": "execute"}).denial
    hooks = source / "hooks/hooks.json"
    data = json.loads(hooks.read_text())
    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "nova-fixture-missing-executable"
    hooks.write_text(json.dumps(data))
    cp.update("demo")
    assert "could not run" in execute_hooks("PreToolUse", {"tool_name": "execute"}).denial


def test_hook_input_rewrite_cannot_change_the_approved_action(store: Store):
    from novacode_cli.plugins.hooks import execute_hooks

    hook_bundle(store, response={"hookSpecificOutput": {"updatedInput": {"command": "unsafe"}}})
    assert (
        "new approved tool request" in execute_hooks("PreToolUse", {"tool_name": "execute"}).denial
    )


def test_invalid_matcher_fails_during_install(store: Store):
    from novacode_cli.plugins.components import Plugin

    source = hook_bundle(store)
    path = source / "hooks/hooks.json"
    data = json.loads(path.read_text())
    data["hooks"]["PreToolUse"][0]["matcher"] = "[unterminated"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid plugin hook matcher"):
        Plugin(source, "demo").validate()


def test_plugin_agent_tool_restrictions_survive_component_loading(store: Store):
    source = bundle(store[0])
    (source / "agents/reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Read-only review\ntools: Read, Grep\n---\nInspect."
    )
    cp.install(str(source))
    spec = cp.plugin_agent_specs()[0]
    assert spec["_nova_tool_names"] == ["read_file", "grep"]
    assert spec["_nova_fs_allow"] == {"read_file", "grep"}


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"continue":false,"stopReason":"halt"}', "halt"),
        ('{"decision":"block","reason":"stop"}', "stop"),
        ('{"hookSpecificOutput":{"permissionDecision":"ask"}}', "blocked"),
        ('{"hookSpecificOutput":[]}', "Invalid hookSpecificOutput"),
        ("[]", "Invalid plugin hook JSON"),
    ],
)
def test_hook_protocol_boundary_rejects_unsupported_or_blocking_responses(
    output: str, expected: str
):
    from novacode_cli.plugins.hooks import _decode

    assert expected in _decode(output, "", 0, "PreToolUse").denial


def test_stop_hook_denial_is_reported(store: Store):
    from novacode_cli.plugins.hooks import PluginHooksMiddleware

    hook_bundle(store, event="Stop", response={"decision": "block", "reason": "review required"})
    with pytest.raises(RuntimeError, match="review required"):
        PluginHooksMiddleware().after_agent({"messages": []}, None)


def test_standard_layout_without_manifest_can_be_installed(store: Store):
    source = bundle(store[0])
    (source / ".claude-plugin/plugin.json").unlink()
    cp.install(str(source))
    assert cp.plugin_commands()[0][0] == "demo:check"


def test_validation_reports_unsupported_host_components(store: Store):
    from novacode_cli.plugins.components import Plugin

    source = bundle(store[0], lspServers={"python": {"command": "pyright"}})
    assert "Unsupported in Nova: lspServers" in Plugin(source, "demo").validate()


def test_marketplace_management_and_errors_use_shared_service(store: Store):
    from novacode_cli.plugins.cli import execute, main

    root = store[0] / "catalog"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/marketplace.json").write_text('{"name":"empty","plugins":[]}')
    assert execute(["marketplace", "add", str(root)]).changed
    assert execute(["marketplace", "list"]).report
    assert execute(["marketplace", "update", "empty"]).changed
    assert execute(["search", "nothing"]).report
    assert execute(["validate", str(root)]).report
    assert execute(["marketplace", "remove", "empty"]).changed
    assert main(["enable", "missing"]) == 1
    assert main(["list"]) == 0


def test_mcp_root_expansion_and_environment_do_not_persist_secrets(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    source = bundle(
        store[0],
        mcpServers={
            "remote": {
                "url": "https://example.com/${NOVA_PLUGIN_FIXTURE_PATH}",
                "headers": {"Authorization": "Bearer ${NOVA_PLUGIN_FIXTURE_TOKEN}"},
            }
        },
    )
    monkeypatch.setenv("NOVA_PLUGIN_FIXTURE_PATH", "mcp")
    monkeypatch.setenv("NOVA_PLUGIN_FIXTURE_TOKEN", "synthetic-token")
    cp.install(str(source))
    definition = cp.plugin_mcp_servers()["demo__remote"]
    assert definition["url"] == "https://example.com/mcp"
    assert definition["headers"]["Authorization"] == "Bearer synthetic-token"
    assert "synthetic-token" not in cp.MANIFEST.read_text()


def test_legacy_uninstall_suppresses_owned_entries_and_keeps_files(store: Store):
    assert store[1].is_relative_to(store[0])
    root = bundle(cp.PLUGINS_DIR)
    cp.MANIFEST.write_text(json.dumps({"demo": {"source": str(root), "enabled": True}}))
    assert cp.remove("demo")
    assert cp.list_plugins() == []
    assert root.is_dir()


def test_scope_installation_outside_current_project_is_not_activated(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    source = bundle(store[0])
    cp.install(str(source), scope="project")
    other = store[0] / "other-project"
    other.mkdir()
    monkeypatch.setattr(cp, "project_root", lambda: other)
    assert cp.list_plugins() == []
    assert cp.plugin_commands() == []


def test_existing_external_component_is_never_read(store: Store):
    outside = store[0] / "outside.md"
    outside.write_text("Outside content must not become a plugin command.")
    source = bundle(store[0], commands=str(outside))
    with pytest.raises(ValueError, match="relative to the plugin root"):
        cp.install(str(source))
    assert cp.list_plugins() == []


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", max_size=30))
def test_generated_safe_identifiers_are_preserved(suffix: str):
    from novacode_cli.plugins.components import safe_name

    name = "plugin-" + suffix
    assert safe_name(name) == name


@given(st.integers(min_value=1, max_value=8))
def test_generated_parent_paths_cannot_cross_bundle_root(depth: int):
    from novacode_cli.plugins.components import contained

    with pytest.raises(ValueError, match="escapes plugin root"):
        contained(Path.cwd(), "../" * depth + "outside", exists=False)


def test_hook_timeout_and_output_limits_are_enforced(store: Store):
    from novacode_cli.plugins.hooks import execute_hooks

    source = hook_bundle(store)
    (source / "probe.py").write_text("print('x' * 65537)")
    cp.update("demo")
    assert "exceeded 64 KiB" in execute_hooks("PreToolUse", {"tool_name": "execute"}).denial
    (source / "probe.py").write_text("import time; time.sleep(2)")
    path = source / "hooks/hooks.json"
    data = json.loads(path.read_text())
    data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 0.05
    path.write_text(json.dumps(data))
    cp.update("demo")
    assert "could not run" in execute_hooks("PreToolUse", {"tool_name": "execute"}).denial


async def test_real_agent_graph_preserves_hook_denial_before_effects(store: Store):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.tools import tool

    from novacode_cli.plugins.hooks import PluginHooksMiddleware
    from tests.test_skills_runtime import RecordingModel

    hook_bundle(store, response={"continue": False, "stopReason": "No fixture effects"})
    effects = []

    @tool
    def fixture_probe() -> str:
        """Record a harmless local effect if execution is permitted."""
        effects.append("executed")
        return "ok"

    model = RecordingModel(
        responses=[
            AIMessage("", tool_calls=[{"name": "fixture_probe", "id": "probe", "args": {}}]),
            AIMessage("The probe was blocked."),
        ]
    )
    graph = create_agent(model, tools=[fixture_probe], middleware=[PluginHooksMiddleware()])
    result = await graph.ainvoke(
        {"messages": [HumanMessage("Run the fixture probe.")]},
        {"configurable": {"thread_id": "fixture-session"}},
    )
    assert effects == []
    assert any(
        isinstance(message, ToolMessage) and message.status == "error"
        for message in result["messages"]
    )


def test_namespaced_skill_body_expands_its_bundle_root(store: Store):
    from novacode_cli.skills.load import list_skills
    from novacode_cli.skills.runtime import skill_snapshot

    source = bundle(store[0])
    (source / "skills/review/SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\nRead ${CLAUDE_PLUGIN_ROOT}/reference.md"
    )
    cp.install(str(source))
    skills = list_skills(plugin_skills_dirs=[directory for _, directory in cp.plugin_skill_dirs()])
    body, _ = skill_snapshot(skills[0], Path(skills[0]["path"]).read_text())
    assert "${CLAUDE_PLUGIN_ROOT}" not in body
    assert str(cp.list_plugins()[0]["path"]) in body


def test_default_hook_file_is_not_executed_twice_when_declared(store: Store):
    from novacode_cli.plugins.hooks import execute_hooks

    source = hook_bundle(
        store,
        event="SessionStart",
        response={
            "hookSpecificOutput": {"additionalContext": "Exactly once"},
        },
    )
    path = source / ".claude-plugin/plugin.json"
    manifest = json.loads(path.read_text())
    manifest["hooks"] = "./hooks/hooks.json"
    path.write_text(json.dumps(manifest))
    cp.update("demo")
    assert execute_hooks("SessionStart", {}).context == "Exactly once"


def test_git_fetch_keeps_ref_and_subdirectory_metadata(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    import shutil
    from types import SimpleNamespace

    repository = store[0] / "repository"
    bundle(repository / "packages")
    calls = []

    def clone(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        shutil.copytree(repository, command[-1])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(cp.subprocess, "run", clone)
    cp.install("owner/repo", ref="v1.0", subdirectory="packages/demo")
    assert calls[0][-4:] == ["v1.0", "--", "https://github.com/owner/repo", calls[0][-1]]
    item = cp.list_plugins()[0]
    assert item["ref"] == "v1.0"
    assert item["subdirectory"] == "packages/demo"
    assert item["source"] == "https://github.com/owner/repo"


def test_failed_git_fetch_preserves_an_existing_install(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    cp.install(str(bundle(store[0])))
    before = cp.MANIFEST.read_bytes()
    monkeypatch.setattr(
        cp.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=1, stderr="fixture fetch failure")),
    )
    with pytest.raises(RuntimeError, match="fixture fetch failure"):
        cp.install("owner/repo")
    assert cp.MANIFEST.read_bytes() == before
    assert cp.plugin_commands()


@pytest.mark.timeout(30)
def test_independent_process_installs_do_not_lose_registry_updates(store: Store):
    import subprocess
    import sys

    sources = [bundle(store[0], name) for name in ("first", "second")]
    code = (
        "import sys; from pathlib import Path; "
        "from novacode_cli.plugins import claude_plugins as cp; "
        "root=Path(sys.argv[1]); cp.PLUGINS_DIR=root/'plugins'; "
        "cp.MANIFEST=cp.PLUGINS_DIR/'installed.json'; "
        "cp.project_root=lambda:root/'project'; cp.install(sys.argv[2])"
    )

    def install(source: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed local Python fixture process.
            [sys.executable, "-c", code, str(store[1]), str(source)],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        for result in workers.map(install, sources):
            assert result.returncode == 0, result.stderr
    assert {item["name"] for item in cp.list_plugins()} == {"first", "second"}


async def test_installed_panel_actions_target_the_existing_scope(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import Mock

    from novacode_cli.tui.screens import ClaudePluginsScreen
    from tests.test_tui_home_preferences import _app

    cp.install(str(bundle(store[0])), scope="project")
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=(45, 25)) as pilot:
        await app.push_screen(ClaudePluginsScreen())
        await pilot.pause()
        await app.screen.workers.wait_for_complete()
        await app.screen._operate("plugin-toggle").wait()
        await app.screen.workers.wait_for_complete()
        assert not cp.list_plugins()[0]["enabled"]
        await app.screen._operate("remove").wait()
        await app.screen.workers.wait_for_complete()
        assert cp.list_plugins() == []


def test_plugin_help_returns_to_the_host_without_terminal_printing(
    capsys: pytest.CaptureFixture[str],
):
    from novacode_cli.plugins.cli import execute

    result = execute(["install", "--help"])
    assert result.report
    assert "--scope" in result.lines[0]
    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("store")
def test_shell_management_prints_results_and_errors(capsys: pytest.CaptureFixture[str]):
    from novacode_cli.plugins.cli import main

    assert main(["--help"]) == 0
    assert "Manage standard directory plugins" in capsys.readouterr().out
    assert main(["list"]) == 0
    assert "No plugins installed." in capsys.readouterr().out
    assert main(["enable", "missing"]) == 1
    assert "missing" in capsys.readouterr().err


def test_reload_discards_older_discovery_and_keeps_autocomplete_session_local(
    monkeypatch: pytest.MonkeyPatch,
):
    from unittest.mock import Mock

    from novacode_cli.tui.app import _TUI_SLASH_COMMANDS
    from tests.test_tui_home_preferences import _app

    app = _app()
    monkeypatch.setattr(app, "_schedule_remote_command_menu", Mock())
    base_commands = list(_TUI_SLASH_COMMANDS)
    app._plugin_command_generation = 2
    handler = Mock()
    app._register_plugin_commands({"old:review": handler}, generation=1)
    assert app._plugin_commands == {}
    app._register_plugin_commands({"new:review": handler}, generation=2)
    assert set(app._plugin_commands) == {"new:review"}
    app._register_plugin_commands({}, generation=2)
    assert app._plugin_commands == {}
    assert base_commands == _TUI_SLASH_COMMANDS


def test_local_install_never_runs_hooks_mcp_or_package_scripts(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import Mock

    source = bundle(store[0])
    (source / "setup.py").write_text("raise RuntimeError('must not run')")
    run = Mock(side_effect=AssertionError("Install must not execute local code"))
    monkeypatch.setattr(cp.subprocess, "run", run)
    cp.install(str(source))
    run.assert_not_called()


def test_project_settings_cannot_download_an_unknown_plugin(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import Mock

    (store[2] / ".nova").mkdir()
    (store[2] / ".nova/settings.json").write_text('{"enabledPlugins":{"missing@untrusted":true}}')
    fetch = Mock(side_effect=AssertionError("Settings must not fetch code"))
    monkeypatch.setattr(cp, "fetch", fetch)
    assert cp.enabled_plugins() == []
    fetch.assert_not_called()


def test_startup_context_tracks_enable_disable_update_and_explicit_reload(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.plugins import hooks

    source = hook_bundle(
        store,
        event="SessionStart",
        response={"hookSpecificOutput": {"additionalContext": "First context"}},
    )
    middleware = hooks.PluginHooksMiddleware()
    original = hooks._run
    called = []

    def run(plugin: Plugin, hook: dict, event: str, payload: dict) -> hooks.HookResult:
        called.append(plugin.name)
        return original(plugin, hook, event, payload)

    monkeypatch.setattr(hooks, "_run", run)
    state = middleware.before_agent({"messages": []}, None)
    assert state["plugin_session_context"] == "First context"
    state = middleware.before_agent(state, None)
    assert called == ["demo"]
    cp.set_enabled("demo", enabled=False)
    state = middleware.before_agent(state, None)
    assert state["plugin_session_context"] == ""
    cp.set_enabled("demo", enabled=True)
    state = middleware.before_agent(state, None)
    assert called == ["demo", "demo"]
    hooks.reload_plugin_hooks()
    state = middleware.before_agent(state, None)
    assert called == ["demo", "demo", "demo"]

    output = json.dumps({"hookSpecificOutput": {"additionalContext": "Updated context"}})
    (source / "probe.py").write_text("print(" + repr(output) + ")")
    cp.update("demo")
    state = middleware.before_agent(state, None)
    assert state["plugin_session_context"] == "Updated context"
    assert called == ["demo", "demo", "demo", "demo"]


def test_subagent_hooks_cannot_impersonate_owner_lifecycle_events(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import Mock

    from novacode_cli.plugins import hooks

    hook_bundle(store, event="SessionStart")
    execute = Mock(side_effect=AssertionError("Subagent is not an owner session"))
    monkeypatch.setattr(hooks, "execute_hooks", execute)
    middleware = hooks.PluginHooksMiddleware(lifecycle=False)
    assert middleware.before_agent({"messages": []}, None) == {}
    middleware.after_agent({"messages": []}, None)
    execute.assert_not_called()


def test_git_timeout_is_a_reportable_operation_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    import subprocess
    from unittest.mock import Mock

    from novacode_cli.plugins.cli import main

    cp.install(str(bundle(store[0])))
    before = cp.MANIFEST.read_bytes()
    monkeypatch.setattr(
        cp.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("git", 120))
    )
    assert main(["install", "owner/repo"]) == 1
    assert cp.MANIFEST.read_bytes() == before


def test_snapshot_identity_preserves_filename_content_boundaries(store: Store):
    source = bundle(store[0])
    (source / "a").write_text("bc")
    cp.install(str(source))
    before = cp.list_plugins()[0]["path"]
    (source / "a").unlink()
    (source / "ab").write_text("c")
    cp.update("demo")
    after = cp.list_plugins()[0]["path"]
    assert after != before
    assert (Path(after) / "ab").read_text() == "c"
    assert (Path(before) / "a").read_text() == "bc"
