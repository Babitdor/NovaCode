from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual import on, events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, RichLog, Static
from textual.widget import Widget

from novacode_cli.config.config import settings


class EmbeddedTerminal(Widget):
    """An interactive, real-time embedded terminal widget styled like a ChatMessage."""

    DEFAULT_CSS = """
    EmbeddedTerminal {
        height: 22;
        margin: 1 0;
        border-left: thick $accent;
        background: $surface;
        padding: 1 4;
        display: block;
    }
    /* Hide the scrollbar cosmetically (wheel/keys still scroll). The app-level
       rule covers this too, but the widget is also usable standalone.
       Make the bar transparent rather than zeroing `scrollbar-size-vertical`:
       a zero scrollbar size on a scroll container breaks Textual's
       content-width calculation and collapses its children to zero width.
       A transparent scrollbar color makes ScrollBar.render() emit blanks while
       keeping the bar's size, so layout is untouched. ScrollBar reads
       `self.parent.styles`, so this must be set on the scroll container. */
    #term-log {
        scrollbar-color: transparent;
        scrollbar-background: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-color-active: transparent;
        scrollbar-background-active: transparent;
    }
    #term-header {
        height: 3;
        background: $panel;
        border-bottom: solid $border;
        padding: 0 1;
    }
    #term-title {
        color: $primary;
        text-style: bold;
        align: left middle;
        width: 1fr;
    }
    #term-buttons {
        width: auto;
        height: auto;
        align: right middle;
    }
    #term-buttons Button {
        min-width: 8;
        height: 1;
        margin-left: 1;
    }
    #term-log {
        height: 1fr;
        background: $boost;
        border: round $border;
        margin: 1 0;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    #term-input-container {
        height: 3;
        align: left middle;
        padding: 0 1;
        border-top: solid $border;
    }
    #term-prompt {
        color: $success;
        text-style: bold;
        margin-right: 1;
    }
    #term-input {
        width: 1fr;
        border: none;
        background: transparent;
        color: $text;
        height: 1;
        padding: 0;
    }
    """

    def __init__(self, initial_cmd: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.initial_cmd = initial_cmd
        self.current_process: asyncio.subprocess.Process | None = None
        self.cwd = settings.get_workspace_root()

    def compose(self) -> ComposeResult:
        with Horizontal(id="term-header"):
            yield Static("🖥️ Embedded Terminal", id="term-title")
            with Horizontal(id="term-buttons"):
                yield Button("Kill", id="btn-kill", variant="error")
                yield Button("Clear", id="btn-clear")
        yield RichLog(id="term-log", highlight=True, markup=True)
        with Horizontal(id="term-input-container"):
            yield Static("nova-cli $ ", id="term-prompt")
            yield Input(placeholder="Type command and press Enter...", id="term-input")

    def on_mount(self) -> None:
        self.update_prompts()
        if self.initial_cmd:
            asyncio.create_task(self.run_initial_cmd())

    def update_prompts(self) -> None:
        proj_root = settings.get_workspace_root()
        
        # Resolve relative to project root
        try:
            rel = self.cwd.relative_to(proj_root)
            path_str = f"project:/{rel.as_posix()}" if str(rel) != "." else "project:/"
        except ValueError:
            if self.cwd == Path.home():
                path_str = "~"
            else:
                path_str = self.cwd.as_posix()
                
        self.query_one("#term-prompt", Static).update(f"[bold #bb9af7]{self.cwd.name}[/bold #bb9af7] $ ")
        self.query_one("#term-title", Static).update(f"🖥️ Embedded Terminal ([dim]{path_str}[/dim])")

    async def run_initial_cmd(self) -> None:
        # Give textual a frame to mount elements
        await asyncio.sleep(0.05)
        log = self.query_one("#term-log", RichLog)
        
        prompt_text = f"{self.cwd.name} $ "
        log.write(Text.assemble(
            Text(prompt_text, style="bold #bb9af7"),
            Text(self.initial_cmd, style="bold white")
        ))

        # Check special commands
        if self.initial_cmd == "clear":
            self.clear_log()
            return

        if self.initial_cmd == "exit":
            self.app.action_toggle_terminal()
            return

        parts = self.initial_cmd.split(maxsplit=1)
        if parts[0] == "cd":
            if len(parts) > 1:
                target = parts[1].strip()
                if target == "~":
                    new_dir = Path.home()
                elif target == "/":
                    new_dir = Path("/")
                else:
                    new_dir = (self.cwd / target).resolve()
                
                if new_dir.is_dir():
                    self.cwd = new_dir
                else:
                    log.write(f"[bold red]cd: no such file or directory: {target}[/bold red]")
            else:
                self.cwd = Path.home()
            
            self.update_prompts()
            return

        await self.run_command_async(self.initial_cmd)

    def on_click(self, event: events.Click) -> None:
        """Focus the input field when any part of the terminal card is clicked."""
        self.query_one("#term-input", Input).focus()
        event.stop()

    @on(Input.Submitted, "#term-input")
    async def handle_command(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        if not cmd:
            return

        term_input = self.query_one("#term-input", Input)
        term_input.value = ""

        log = self.query_one("#term-log", RichLog)

        # Show the command being executed
        prompt_text = f"{self.cwd.name} $ "
        log.write(Text.assemble(
            Text(prompt_text, style="bold #bb9af7"),
            Text(cmd, style="bold white")
        ))

        # Check special commands
        if cmd == "clear":
            self.clear_log()
            return

        if cmd == "exit":
            # Remove this terminal card from the transcript when exiting
            self.remove()
            return

        parts = cmd.split(maxsplit=1)
        if parts[0] == "cd":
            if len(parts) > 1:
                target = parts[1].strip()
                if target == "~":
                    new_dir = Path.home()
                elif target == "/":
                    new_dir = Path("/")
                else:
                    new_dir = (self.cwd / target).resolve()
                
                if new_dir.is_dir():
                    self.cwd = new_dir
                else:
                    log.write(f"[bold red]cd: no such file or directory: {target}[/bold red]")
            else:
                self.cwd = Path.home()
            
            self.update_prompts()
            return

        if self.current_process is not None:
            log.write("[bold red]Error: A command is already running. Please kill it first or wait for completion.[/bold red]")
            return

        # Start execution
        asyncio.create_task(self.run_command_async(cmd))

    async def run_command_async(self, cmd: str) -> None:
        log = self.query_one("#term-log", RichLog)
        
        try:
            self.current_process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
            )
            
            # Read line by line
            while True:
                line_bytes = await self.current_process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                log.write(line)
                log.scroll_end(animate=False)
                
            await self.current_process.wait()
            exit_code = self.current_process.returncode
            if exit_code == 0:
                log.write("[bold green]Command finished successfully.[/bold green]")
            else:
                log.write(f"[bold red]Command exited with code {exit_code}.[/bold red]")
        except Exception as e:
            log.write(f"[bold red]Failed to execute command: {e}[/bold red]")
        finally:
            self.current_process = None

    @on(Button.Pressed, "#btn-kill")
    def kill_process(self) -> None:
        log = self.query_one("#term-log", RichLog)
        if self.current_process is not None:
            try:
                self.current_process.terminate()
                log.write("[bold orange]Command terminated by user.[/bold orange]")
            except Exception as e:
                log.write(f"[bold red]Error terminating command: {e}[/bold red]")
        else:
            log.write("[dim]No process currently running.[/dim]")

    @on(Button.Pressed, "#btn-clear")
    def clear_log(self) -> None:
        self.query_one("#term-log", RichLog).clear()
