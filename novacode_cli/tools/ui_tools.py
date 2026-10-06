"""Agent access to the local TUI's validated, declarative harness."""

import asyncio
import json

from langchain_core.tools import tool
from textual._context import active_app

from novacode_cli.tui.harness import UIHarnessRequest


async def _request(operation: str, payload: dict | None = None) -> str:
    try:
        app = active_app.get()
    except LookupError:
        return "UI customization is available only in a running local Nova TUI."
    if not getattr(app, "_ui_harness", None):
        return "UI customization is unavailable in this session."
    future = asyncio.get_running_loop().create_future()
    if not app.post_message(UIHarnessRequest(operation, payload or {}, future)):
        return "The UI is closing; no customization was applied."
    try:
        return json.dumps(await asyncio.wait_for(future, timeout=15))
    except TimeoutError:
        return "The UI request timed out. Inspect the layout before retrying."


@tool
async def ui_inspect() -> str:
    """Inspect the local UI layout, registered components, themes and protected controls."""
    return await _request("inspect")


@tool
async def ui_patch(patch: dict) -> str:
    """Stage a UI change without rendering or saving it.

    Merge top-level fields into the candidate: panels, width, theme, bindings, commands.
    Panels replace the list and have id, type (notes/files/activity), title, text,
    height (3-30), visible. Shortcuts F6-F12 and commands ui-<name> may map to
    toggle:<panel-id>. Use ui_inspect first, ui_preview next, then ui_commit or ui_rollback.
    The conversation, prompt, approval controls and core shortcuts cannot be replaced.
    """
    return await _request("patch", patch)


@tool
async def ui_preview() -> str:
    """Render the staged UI layout without saving; ui_rollback restores the committed layout."""
    return await _request("preview")


@tool
async def ui_commit() -> str:
    """Render and save the staged UI layout for this workspace across restarts."""
    return await _request("commit")


@tool
async def ui_rollback() -> str:
    """Discard staged UI changes and restore the last committed layout."""
    return await _request("rollback")
