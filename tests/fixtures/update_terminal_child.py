"""Native console fixture: real update handoff/resume, no install or model calls."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROLE, DIRECTORY = sys.argv[1], Path(sys.argv[2])
SCRIPT = str(Path(__file__).resolve())
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if ROLE == "launcher":
    from novacode_cli import windows_launcher

    original = subprocess.Popen

    def spawn(_command, **kwargs):
        return original([sys.executable, SCRIPT, "runtime", str(DIRECTORY)], **kwargs)

    subprocess.Popen = spawn
    raise SystemExit(windows_launcher.supervise(["--continue", "saved"]))

if ROLE == "runtime":
    from novacode_cli import updates, windows_launcher

    os.environ.pop(windows_launcher.CHILD_ENV, None)
    original = subprocess.Popen

    def spawn(_command, **kwargs):
        return original([sys.executable, SCRIPT, "updater", str(DIRECTORY)], **kwargs)

    subprocess.Popen = spawn
    assert updates._hand_off_update({"session": "saved", "continue": False})
    raise SystemExit(0)

if ROLE == "updater":
    from novacode_cli import updates

    updates._wait_for_handoff()
    original = subprocess.Popen

    def spawn(_command, **kwargs):
        return original([sys.executable, SCRIPT, "resumed", str(DIRECTORY)], **kwargs)

    subprocess.Popen = spawn
    raise SystemExit(updates._relaunch("saved", continue_turn=False))

if ROLE == "resumed":
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Input, Static

    from novacode_cli import updates, windows_launcher

    os.environ.pop(windows_launcher.CHILD_ENV, None)
    second = (DIRECTORY / "updated-once").exists()

    class Resumed(App):
        def compose(self) -> ComposeResult:
            yield Input(id="composer")
            with VerticalScroll(id="history"):
                yield Static("\n".join(f"Message {i}" for i in range(100)))

        def snapshot(self):
            state = {
                "second": second,
                "text": self.query_one("#composer", Input).value,
                "scroll": self.query_one("#history", VerticalScroll).scroll_y,
            }
            temporary = DIRECTORY / "state.tmp"
            temporary.write_text(json.dumps(state), encoding="utf-8")
            try:
                temporary.replace(DIRECTORY / "state.json")
            except PermissionError:
                pass  # the next sample retries if Windows caught the reader mid-open

        def on_mount(self):
            self.query_one("#composer", Input).focus()
            self.snapshot()
            (DIRECTORY / ("ready-second" if second else "ready")).write_text(
                "ready", encoding="ascii"
            )
            self.set_interval(0.1, self.snapshot)
            self.set_timer(5, self.exit)

    Resumed().run()
    if not second:
        (DIRECTORY / "updated-once").write_text("updated", encoding="ascii")
        original = subprocess.Popen

        def spawn(_command, **kwargs):
            return original([sys.executable, SCRIPT, "updater", str(DIRECTORY)], **kwargs)

        subprocess.Popen = spawn
        assert updates._hand_off_update({"session": "saved", "continue": False})
