"""/agents: buttons stay on screen; tools can be chosen at creation and edited."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Input, OptionList, SelectionList  # noqa: E402

from novacode_cli.agents.agent_file import agent_tools, read_agent, write_agent  # noqa: E402
from tests.test_tui_app import _SS, _FakeAgent  # noqa: E402

ARSENAL = [
    ("web_search", "Search the web"),
    ("fetch_url", "Fetch a URL"),
    ("serena_find_symbol", "Find a symbol"),
]


def _isolate(monkeypatch, tmp_path):
    from novacode_cli.config.config import settings
    import novacode_cli.tui.screens as screens

    monkeypatch.setattr(settings, "get_agents_root_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "get_project_agents_dir", lambda: None)
    monkeypatch.setattr(screens, "_session_arsenal", lambda app: ARSENAL)
    write_agent(
        tmp_path / "long-one" / "agent.md",
        {"color": "#0ea5e9", "description": "Has a very long prompt"},
        "\n".join(f"Rule {i}: do the thing carefully." for i in range(200)),
    )


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=_FakeAgent(), assistant_id="nova-agent", session_state=_SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m",
    )


async def _settle(pilot) -> None:
    """Let a pushed modal finish mounting (Select children, zoom animation)."""
    for _ in range(4):
        await pilot.pause(0.05)


async def _open_agents(app, pilot):
    from novacode_cli.tui.app import AgentsScreen

    app.push_screen(AgentsScreen())
    await _settle(pilot)
    return app.screen


def test_buttons_stay_on_screen_with_a_long_prompt(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)

    async def run() -> None:
        app = _app()
        async with app.run_test(size=(100, 28)) as pilot:
            screen = await _open_agents(app, pilot)
            await _settle(pilot)
            for bid in ("create", "edit-tools", "delete", "close"):
                region = screen.query_one(f"#{bid}", Button).region
                assert region.height > 0 and region.bottom <= 28, (bid, region)

    asyncio.run(run())


def test_edit_tools_writes_the_selection(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)

    async def run() -> None:
        from novacode_cli.tui.screens import ToolPickerModal

        app = _app()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await _open_agents(app, pilot)
            screen.query_one("#agents-list", OptionList).highlighted = 0
            screen.query_one("#edit-tools", Button).press()
            await _settle(pilot)
            picker = app.screen
            assert isinstance(picker, ToolPickerModal)
            # No tools: key yet = every tool, so everything starts ticked.
            assert len(picker.query_one("#tool-list", SelectionList).selected) == 3
            picker.query_one("#none-shown", Button).press()
            await _settle(pilot)
            picker.query_one("#tool-filter", Input).value = "web"
            await _settle(pilot)
            picker.query_one("#all-shown", Button).press()
            await _settle(pilot)
            picker.query_one("#save", Button).press()
            await _settle(pilot)

        md = tmp_path / "long-one" / "agent.md"
        assert agent_tools(md.read_text(encoding="utf-8")) == ["web_search"]
        front, body = read_agent(md)
        assert front["description"] == "Has a very long prompt" and body.startswith("Rule 0")

    asyncio.run(run())


def test_create_with_chosen_tools(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    import novacode_cli.commands.agents_commands as ac

    async def fake_prompt(name, desc):  # noqa: ARG001
        return "You review code."

    monkeypatch.setattr(ac, "_generate_agent_system_prompt", fake_prompt)

    async def run() -> None:
        from novacode_cli.tui.screens import AgentCreateModal, ToolPickerModal

        app = _app()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await _open_agents(app, pilot)
            screen.query_one("#create", Button).press()
            await _settle(pilot)
            modal = app.screen
            assert isinstance(modal, AgentCreateModal)
            modal.query_one("#agent-name", Input).value = "reviewer"
            modal.query_one("#agent-desc", Input).value = "Reviews code: carefully"
            modal.query_one("#choose-tools", Button).press()
            await _settle(pilot)
            picker = app.screen
            assert isinstance(picker, ToolPickerModal)
            picker.query_one("#none-shown", Button).press()
            await _settle(pilot)
            picker.query_one("#tool-filter", Input).value = "serena"
            await _settle(pilot)
            picker.query_one("#all-shown", Button).press()
            await _settle(pilot)
            picker.query_one("#save", Button).press()
            await _settle(pilot)
            assert app.screen is modal
            modal.query_one("#do-create", Button).press()
            for _ in range(20):
                await _settle(pilot)
                if (tmp_path / "reviewer" / "agent.md").exists():
                    break

        front, body = read_agent(tmp_path / "reviewer" / "agent.md")
        assert front["tools"] == ["serena_find_symbol"]
        assert front["description"] == "Reviews code: carefully", "a colon no longer breaks it"
        assert body.strip() == "You review code."

    asyncio.run(run())


def test_named_subagent_gets_only_its_tools(tmp_path, monkeypatch) -> None:
    """build_named_subagents narrows the tool list and strips the frontmatter."""
    from novacode_cli.agents import core_agent
    from novacode_cli.config.config import settings

    write_agent(tmp_path / "picky" / "agent.md", {"description": "Picky", "tools": ["web_search"]},
                "Be picky.")
    monkeypatch.setattr(settings, "get_all_agents", lambda: [("picky", tmp_path / "picky", "global")])
    core_agent._named_subagents_cache.clear()

    class _T:
        def __init__(self, name):
            self.name = name

    specs = core_agent.build_named_subagents("nova-agent", [_T("web_search"), _T("fetch_url")])
    spec = next(s for s in specs if s["name"] == "picky")
    assert [t.name for t in spec["tools"]] == ["web_search"]
    assert spec["system_prompt"].strip() == "Be picky."

    class _M:
        name = "serena_find_symbol"

    spec["_nova_tool_names"] = ["web_search", "serena_find_symbol"]
    core_agent._grant_mcp_tools([spec], [_M()])
    assert [t.name for t in spec["tools"]] == ["web_search", "serena_find_symbol"]
