"""Drive the existing command panels from a scoped remote conversation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from contextvars import ContextVar

from rich.console import Console
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    OptionList,
    Select,
    Static,
    Switch,
    Tab,
    Tabs,
    TextArea,
    Tree,
)

logger = logging.getLogger(__name__)

command_output: ContextVar[list | None] = ContextVar("remote_command_output", default=None)


def plain(value) -> str:
    output = io.StringIO()
    Console(file=output, force_terminal=False, width=90).print(value)
    return output.getvalue().strip()


def identity(msg) -> tuple:
    return (
        msg.platform,
        str(msg.chat_id),
        str(getattr(msg, "sender_id", "")),
        getattr(msg, "thread_id", None),
    )


class RemoteCommandPanel:
    """One requester can operate one panel; unrelated topics keep routing normally."""

    def __init__(self, app, msg):
        self.app, self.msg = app, msg
        self.owner = identity(msg)
        self.original = app.screen
        self.current = None
        self.actions = []
        self.task = None
        self.expires = time.monotonic() + 600
        self.closed = False
        self.page = 1

    def start(self):
        self.task = asyncio.create_task(self._watch())

    async def close(self, *, dismiss=False):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if dismiss:
            self._dismiss()

    def _dismiss(self):
        with contextlib.suppress(Exception):
            if self.current is not None and self.app.screen is self.current:
                self.current.dismiss(None)

    async def _watch(self):
        try:
            while time.monotonic() < self.expires:
                await asyncio.sleep(0.2)
                if self.app.screen is self.original:
                    if self.current is not None:
                        break
                elif self.app.screen is not self.current:
                    await self.refresh()
            self.closed = True
            self._dismiss()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.closed = True
            self._dismiss()
            logger.debug("Remote command panel delivery failed", exc_info=True)

    async def refresh(self, page=None):
        screen = self.app.screen
        if screen is self.original:
            return
        self.current = screen
        self.actions = []
        lines = [f"**{type(screen).__name__.removesuffix('Screen')}**"]

        def add(widget, kind, value, label):
            self.actions.append((widget, kind, value))
            lines.append(f"{len(self.actions)}. {label}")

        for widget in screen.query():
            if any(
                not node.display or not node.visible
                for node in widget.ancestors_with_self
                if hasattr(node, "display")
            ):
                continue
            if isinstance(widget, Button):
                if not widget.disabled:
                    add(widget, "button", None, plain(widget.label))
            elif isinstance(widget, Input):
                if not widget.disabled:
                    label = widget.placeholder or widget.id or "Input"
                    value = "[hidden]" if widget.password else widget.value
                    add(widget, "input", None, f"{label}: {value} (/set or /input)")
            elif isinstance(widget, (Checkbox, Switch)):
                if not widget.disabled:
                    label = plain(widget.label) if isinstance(widget, Checkbox) else widget.id
                    add(widget, "toggle", None, f"{'☑' if widget.value else '☐'} {label}")
            elif isinstance(widget, TextArea):
                if not widget.read_only and not widget.disabled:
                    add(
                        widget,
                        "text",
                        None,
                        f"{widget.id or 'Editor'} (/set)\n{widget.text[:2000]}",
                    )
            elif isinstance(widget, Tree):
                nodes = list(reversed(widget.root.children))
                while nodes:
                    node = nodes.pop()
                    add(widget, "tree", node, plain(node.label))
                    if node.is_expanded:
                        nodes.extend(reversed(node.children))
            elif isinstance(widget, Tabs):
                for tab in widget.query(Tab):
                    if not tab.disabled:
                        add(widget, "tab", tab.id, plain(tab.label))
            elif isinstance(widget, OptionList):
                for index in range(widget.option_count):
                    option = widget.get_option_at_index(index)
                    if not widget.disabled and not option.disabled:
                        add(widget, "option", index, plain(option.prompt))
            elif isinstance(widget, Select):
                if not widget.disabled:
                    for label, value in widget._options:
                        add(widget, "select", value, plain(label))
            elif isinstance(widget, DataTable):
                for index in range(widget.row_count):
                    label = " · ".join(plain(cell) for cell in widget.get_row_at(index))
                    add(widget, "row", index, label)
            elif isinstance(widget, Static):
                text = plain(widget.render())
                if text:
                    lines.append(text)
        body = "\n".join(lines)
        pages = [body[i : i + 3500] for i in range(0, len(body), 3500)] or [""]
        self.page = min(max(1, page or self.page), len(pages))
        help_text = (
            f"\n\nPage {self.page}/{len(pages)} · /panel N for another page\n"
            "/choose N · /set N value · /input N value (submit) · /back"
        )
        await asyncio.wait_for(self.msg.reply_fn(pages[self.page - 1] + help_text), timeout=15)

    async def handle(self, msg) -> bool:
        if self.closed or identity(msg) != self.owner:
            return False
        parts = (msg.text or "").strip().split(maxsplit=2)
        if parts:
            parts[0] = parts[0].lower().split("@", 1)[0]
        if not parts or parts[0].lower() not in {
            "/choose",
            "/set",
            "/input",
            "/toggle",
            "/panel",
            "/back",
        }:
            return False
        if time.monotonic() >= self.expires or self.app.screen is self.original:
            await self.close(dismiss=True)
            await msg.reply_fn("That panel has closed. Send its slash command again.")
            return True
        if parts[0].lower() == "/panel":
            try:
                await self.refresh(int(parts[1]) if len(parts) > 1 else None)
            except ValueError:
                await msg.reply_fn("Usage: /panel [page number]")
            return True
        if parts[0].lower() == "/back":
            self.app.screen.dismiss(None)
        else:
            try:
                index = int(parts[1]) - 1
                if index < 0 or self.app.screen is not self.current:
                    raise ValueError
                widget, kind, value = self.actions[index]
                command = parts[0].lower()
                if kind == "text" and command == "/set":
                    widget.load_text(parts[2] if len(parts) > 2 else "")
                elif kind == "input" and command in {"/set", "/input"}:
                    widget.value = parts[2] if len(parts) > 2 else ""
                    if command == "/input":
                        widget.post_message(Input.Submitted(widget, widget.value))
                elif command in {"/choose", "/toggle"}:
                    if kind == "button":
                        widget.press()
                    elif kind == "toggle":
                        widget.value = not widget.value
                    elif kind == "option":
                        widget.highlighted = value
                        widget.action_select()
                    elif kind == "select":
                        widget.value = value
                    elif kind == "row":
                        widget.move_cursor(row=value)
                        widget.action_select_cursor()
                    elif kind == "tree":
                        widget.select_node(value)
                    elif kind == "tab":
                        widget.active = value
                    else:
                        raise ValueError
                else:
                    raise ValueError
            except (IndexError, ValueError):
                await msg.reply_fn("Choose a number from /panel; use /set N value for fields.")
                return True
        await asyncio.sleep(0.1)
        if self.app.screen is self.original:
            await self.close()
            await msg.reply_fn("Panel closed.")
        else:
            await self.refresh()
        return True
