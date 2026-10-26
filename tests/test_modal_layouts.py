"""Modal actions remain reachable across interface presets and terminal sizes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from rich.text import Text
from textual.app import App
from textual.widgets import OptionList, Select, Static, TextArea

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.council_settings import default_settings
from novacode_cli.tui import screens
from novacode_cli.tui.app import NovaApp, _responsive_breakpoints
from novacode_cli.tui.auth_screens import AuthManagerScreen
from novacode_cli.tui.caveman_screen import CavemanScreen
from novacode_cli.tui.council_settings_screen import CouncilSettingsScreen
from novacode_cli.tui.home import HOME_STYLES
from novacode_cli.tui.mcp_tools_screen import McpToolsScreen
from novacode_cli.tui.statusline_screen import StatusLineScreen
from novacode_cli.tui.widgets import NOVA_TOKYO_NIGHT

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.screen import ModalScreen


class ModalHost(App):
    """Use production style rules without starting agents or remote services."""

    CSS = NovaApp.CSS
    CSS_PATH = str(Path(screens.__file__).with_name("interface_styles.tcss"))
    _home_style = "dashboard"

    def _set_home_style(self, style: str) -> None:
        self._home_style = style
        self.apply_layout()

    def apply_layout(self) -> None:
        """Apply the same style classes and breakpoints as NovaApp."""
        NovaApp._apply_interface_style(self)
        for name, enabled in zip(
            ("narrow", "compact", "short", "tiny"),
            _responsive_breakpoints(self.size.width, self.size.height),
            strict=True,
        ):
            self.screen.set_class(enabled, name)


LONG_BODY = "\n".join(f"Line {i}: Review this detail before choosing an action." for i in range(40))
CASES: dict[str, Callable[[], ModalScreen]] = {
    "theme": screens.ThemeScreen,
    "settings": screens.SettingsScreen,
    "confirm": lambda: screens.ConfirmModal("Confirm action", LONG_BODY),
    "approval": lambda: screens.ApprovalModal("Review input", LONG_BODY),
    "plan": lambda: screens.PlanApprovalModal("Review plan", LONG_BODY),
    "model": screens.ModelScreen,
    "mcp": screens.McpScreen,
    "mcp-custom": screens.McpCustomModal,
    "plugins": screens.PluginsScreen,
    "agents": screens.AgentsScreen,
    "agent-create": screens.AgentCreateModal,
    "skill-create": screens.SkillCreateModal,
    "hooks": screens.HooksScreen,
    "hook-create": screens.HookCreateModal,
    "servers": screens.ServersScreen,
    "usage": screens.UsageScreen,
    "info": lambda: screens.InfoListScreen("Information", LONG_BODY.splitlines()),
    "pick": lambda: screens.PickScreen("Pick an item", LONG_BODY.splitlines()),
    "tool-picker": lambda: screens.ToolPickerModal(
        "Tools", [(f"tool-{i}", "Description") for i in range(30)], None
    ),
    "remember-rule": lambda: screens.RememberRuleModal(
        SimpleNamespace(human="Allow this command", category="shell", value="git status")
    ),
    "caveman": lambda: CavemanScreen({}, router_ready=False),
    "statusline": lambda: StatusLineScreen([], lambda _fields: Text("Status preview")),
    "sessions": lambda: screens.SessionsScreen(None, None),
    "auth": AuthManagerScreen,
    "langsmith": screens.LangSmithSetupModal,
    "mcp-install": lambda: screens.McpInstallModal(
        {"name": "Example MCP", "description": LONG_BODY, "setup_prompt": "API key"}, "example"
    ),
    "mcp-tools": lambda: McpToolsScreen(
        "example", [(f"tool-{i}", "Description", 100) for i in range(30)], None
    ),
    "claude-plugins": screens.ClaudePluginsScreen,
    "update": screens.UpdateScreen,
    "remote": lambda: screens.RemoteScreen(SimpleNamespace()),
    "router": lambda: screens.RouterScreen(NovaConfig()),
    "archive": lambda: screens._ArchivePickerModal([]),
    "council": lambda: CouncilSettingsScreen(default_settings()),
}


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", lambda _var: "")


def action_failures(app: App, screen: ModalScreen) -> list[str]:
    """Check painted hit targets, including clipping by ancestors or siblings."""
    failures = []
    box = screen.query_one("#modal-box")
    for button in screen.query("#modal-buttons Button, #remember-buttons Button"):
        if not button.display:
            continue
        region = button.region
        if (
            not region
            or not box.region.contains_region(region)
            or not screen.region.contains_region(region)
        ):
            failures.append(f"{button.id}: outside dialog {region}")
            continue
        widget, _ = app.get_widget_at(*region.center)
        if widget is not button and button not in widget.ancestors:
            failures.append(f"{button.id}: covered by {widget.id or type(widget).__name__}")
    failures.extend(
        "approval choices: outside dialog"
        for choices in screen.query("#choices")
        if not choices.region or choices.region.bottom > box.region.bottom
    )
    failures.extend(
        f"{editor.id}: multiline editor lost its frame or content area"
        for editor in screen.query(TextArea)
        if editor.display
        and editor.region
        and (not editor.styles.border_top[0] or editor.content_region.height < 1)
    )
    return failures


@pytest.mark.parametrize("style", [key for _, key in HOME_STYLES])
@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.timeout(30)
async def test_modal_actions_across_presets(
    case: str, style: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    NovaConfig().set("home_style", style)
    monkeypatch.setattr(screens, "animate_modal_screen", lambda _screen: None)
    factory = CASES[case]
    screen = factory()
    # Layout fixtures never contact providers, inspect credentials, or start
    # update checks. Existing screen tests exercise their real data loaders.
    if case not in {
        "theme",
        "approval",
        "plan",
        "confirm",
        "info",
        "pick",
        "mcp-custom",
        "agent-create",
        "remember-rule",
        "caveman",
        "statusline",
    }:
        monkeypatch.setattr(type(screen), "on_mount", lambda _self: None)
    if case != "theme":
        for event in ("on_select_changed", "on_switch_changed"):
            if hasattr(type(screen), event):
                monkeypatch.setattr(type(screen), event, lambda _self, _event: None)
    app = ModalHost()
    app.register_theme(NOVA_TOKYO_NIGHT)
    app.theme = "tokyo-night"
    app._home_style = style
    problems = []
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(screen)
        app.apply_layout()
        await pilot.pause()
        for option_list in screen.query(OptionList):
            if option_list.id in {
                "model-options",
                "mcp-configured",
                "mcp-presets",
                "plugins",
                "agents-list",
                "servers-list",
                "hooks-list",
            }:
                option_list.add_options([f"Item {i}: description" for i in range(30)])
        if case == "usage":
            screen.query_one("#usage-body", Static).update(LONG_BODY)
        for width, height in ((120, 40), (80, 24), (48, 18)):
            await pilot.resize_terminal(width, height)
            app.apply_layout()
            await pilot.pause()
            failures = action_failures(app, screen)
            box = screen.query_one("#modal-box")
            box.scroll_end(immediate=True)
            await pilot.pause()
            failures.extend(action_failures(app, screen))
            box.scroll_home(immediate=True)
            if failures:
                snapshot = tmp_path / f"{case}-{style}-{width}x{height}.svg"
                snapshot.write_text(app.export_screenshot(), encoding="utf-8")
                problems.append(f"{width}x{height}: {', '.join(failures)}; {snapshot}")
    assert not problems, "\n".join(problems)


@pytest.mark.timeout(30)
async def test_theme_style_and_color_changes_keep_actions_reachable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(screens, "animate_modal_screen", lambda _screen: None)
    app = ModalHost()
    app.register_theme(NOVA_TOKYO_NIGHT)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = screens.ThemeScreen()
        app.push_screen(screen)
        app.apply_layout()
        await pilot.pause()
        for theme in ("tokyo-night", "textual-light"):
            app.theme = theme
            for _, style in HOME_STYLES:
                screen.query_one("#home-style-select", Select).value = style
                await pilot.pause()
                assert not action_failures(app, screen), (theme, style)
        choices = screen.query_one("#theme-list", OptionList)
        choices.highlighted = choices.option_count - 1
        choices.focus()
        await pilot.press("enter")
        assert NovaConfig().get("theme") == screen._theme_names[-1]
