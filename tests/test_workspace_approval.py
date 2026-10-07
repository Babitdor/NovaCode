"""Startup consent stays explicit while the native layout remains usable."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Static

from novacode_cli.path_approval import PathApprovalManager
from novacode_cli.tui.workspace_approval import WorkspaceApprovalApp


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("novacode_cli.config.config.HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("y", "recursive"),
        ("o", "only"),
        ("n", None),
        ("escape", None),
        ("ctrl+c", None),
        ("enter", None),
    ],
)
async def test_keyboard_requires_explicit_approval(tmp_path: Path, key: str, expected: str | None):
    app = WorkspaceApprovalApp(tmp_path)
    async with app.run_test() as pilot:
        assert app.focused.id == "workspace-deny"
        await pilot.press(key)
    assert app.return_value == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button", "expected"),
    [
        ("workspace-tree", "recursive"),
        ("workspace-only", "only"),
        ("workspace-deny", None),
    ],
)
async def test_buttons_choose_exact_scope(tmp_path: Path, button: str, expected: str | None):
    app = WorkspaceApprovalApp(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        target = app.query_one(f"#{button}", Button)
        target.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(f"#{button}")
    assert app.return_value == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 45), (80, 24), (40, 16), (30, 12)])
async def test_layout_path_and_brand_reflow(tmp_path: Path, size: tuple[int, int]):
    path = tmp_path / "folder [red]" / ("very-long-folder-name-" * 3)
    app = WorkspaceApprovalApp(path)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        card = app.query_one("#workspace-card")
        deny = app.query_one("#workspace-deny", Button)
        assert card.region.right <= size[0]
        assert card.region.bottom <= size[1]
        assert deny.region.bottom <= size[1]
        if size[0] < 50:
            for selector in ("#workspace-tree", "#workspace-only"):
                button = app.query_one(selector, Button)
                assert cell_len(button.label.plain) <= button.content_size.width
        assert str(path.resolve()) == app.query_one("#workspace-path", Static).content.plain
        art = app.query_one("#workspace-art", Static).content.plain
        assert art.strip()
        assert all(cell_len(line) <= card.content_size.width for line in art.splitlines())
        if size[1] >= 28:
            assert "⣿" in art
        await pilot.resize_terminal(100, 40)
        await pilot.pause()
        assert "⣿" in app.query_one("#workspace-art", Static).content.plain
        await pilot.press("escape")


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "recursive"), [("recursive", True), ("only", False)])
async def test_manager_persists_only_selected_scope(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    choice: str,
    recursive: bool,
):
    import novacode_cli.path_approval as approval

    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(WorkspaceApprovalApp, "run_async", AsyncMock(return_value=choice))
    manager = PathApprovalManager()
    assert await manager.prompt_for_approval(tmp_path)
    reloaded = PathApprovalManager()
    assert reloaded.is_path_approved(tmp_path)
    assert reloaded.is_path_approved(tmp_path / "child") is recursive
    assert reloaded.list_approved_paths()[str(tmp_path.resolve())]["recursive"] is recursive


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [None, "unexpected", ""])
async def test_closing_or_unknown_choice_never_saves_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    choice: str | None,
):
    import novacode_cli.path_approval as approval

    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(approval.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(WorkspaceApprovalApp, "run_async", AsyncMock(return_value=choice))
    manager = PathApprovalManager()
    save = Mock()
    monkeypatch.setattr(manager, "approve_path", save)
    assert not await manager.prompt_for_approval(tmp_path)
    save.assert_not_called()


@pytest.mark.asyncio
async def test_noninteractive_terminal_keeps_text_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import novacode_cli.path_approval as approval

    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: False)
    manager = PathApprovalManager()
    fallback = AsyncMock(return_value=False)
    monkeypatch.setattr(manager, "_prompt_for_approval_text", fallback)
    screen = AsyncMock()
    monkeypatch.setattr(WorkspaceApprovalApp, "run_async", screen)
    assert not await manager.prompt_for_approval(tmp_path)
    fallback.assert_awaited_once_with(tmp_path)
    screen.assert_not_called()


@pytest.mark.asyncio
async def test_arrow_keys_select_scope_before_enter(tmp_path: Path):
    app = WorkspaceApprovalApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("up")
        assert app.focused.id == "workspace-only"
        await pilot.press("enter")
    assert app.return_value == "only"


@pytest.mark.asyncio
async def test_choices_are_visible_on_standard_short_terminal(tmp_path: Path):
    app = WorkspaceApprovalApp(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        viewport = app.query_one("#workspace-content").region
        for selector in ("#workspace-tree", "#workspace-only"):
            button = app.query_one(selector, Button)
            assert button.region.y >= viewport.y
            assert button.region.bottom <= viewport.bottom
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_saved_theme_applies_to_approval_screen(tmp_path: Path):
    from novacode_cli.config.nova_config import NovaConfig

    NovaConfig().set("theme", "matrix")
    app = WorkspaceApprovalApp(tmp_path)
    async with app.run_test() as pilot:
        assert app.theme == "matrix"
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_preapproved_folder_skips_screen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import novacode_cli.path_approval as approval

    manager = PathApprovalManager()
    manager.approve_path(tmp_path, recursive=True)
    monkeypatch.setattr(approval, "PathApprovalManager", lambda: manager)
    prompt = AsyncMock()
    monkeypatch.setattr(manager, "prompt_for_approval", prompt)
    assert await approval.check_path_approval(tmp_path / "child")
    prompt.assert_not_called()
