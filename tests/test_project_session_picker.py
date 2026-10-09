import pytest
from textual.app import App
from textual.widgets import Input, Select

from novacode_cli.tui.screens import ProjectSessionPicker


class _PickerHost(App[None]):
    def __init__(self, screen: ProjectSessionPicker) -> None:
        super().__init__()
        self.picker = screen
        self.result = None

    def on_mount(self) -> None:
        self.push_screen(self.picker, self._save_result)

    def _save_result(self, result) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_project_session_picker_returns_folder_and_task():
    host = _PickerHost(
        ProjectSessionPicker(
            [("Harness", "B:/Harness"), ("NovaCode", "B:/NovaCode")],
            task="investigate tests",
            preferred_folder="B:/NovaCode",
        )
    )

    async with host.run_test() as pilot:
        await pilot.pause()
        assert host.screen.query_one("#project-folder", Select).value == "B:/NovaCode"
        assert host.screen.query_one("#project-task", Input).value == "investigate tests"
        await pilot.click("#continue")
        await pilot.pause()

    assert host.result == {"folder": "B:/NovaCode", "task": "investigate tests"}
