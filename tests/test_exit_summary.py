"""Nova's exit display is responsive and never invents a resumable session."""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.console import Console


@pytest.mark.parametrize("width", [20, 45, 100])
def test_exit_logo_and_resume_command_fit_terminal(width):
    from novacode_cli._version import __version__
    from novacode_cli.ui.exit_summary import build_exit_message

    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(build_exit_message("session-123", "Fix authentication", saved=True, width=width))
    text = output.getvalue()
    assert __version__ in text
    assert "Session" in text and "Continue" in text
    assert "nova --continue session-123" in " ".join(text.split())
    assert all(len(line) <= width for line in text.splitlines())


def test_exit_without_saved_conversation_does_not_offer_continue():
    from novacode_cli.ui.exit_summary import build_exit_message

    text = build_exit_message("s", "main", saved=False, width=100).plain
    assert "nova --continue" not in text
    assert "No saved conversation" in text


def test_exit_user_content_cannot_inject_terminal_controls_or_shell_arguments():
    from novacode_cli.ui.exit_summary import build_exit_message

    text = build_exit_message(
        "s; echo bad", "\x1b]0;bad\x07\n[red]Task", saved=True, width=100
    ).plain
    assert "\x1b" not in text and "\x07" not in text
    assert "nova --resume" in text
    assert "nova --continue s; echo bad" not in text


async def test_exit_checks_root_saved_metadata_and_prints_resume_command():
    from novacode_cli.ui.exit_summary import print_exit_summary

    root_state = SimpleNamespace(session_id="root-saved")
    get_meta = Mock(
        return_value=SimpleNamespace(message_count=3, cleared=False, current_task="Fix a bug")
    )
    app = SimpleNamespace(
        _root_pane=SimpleNamespace(state={"session_state": root_state}, title="root"),
        session_state=SimpleNamespace(session_id="visible-child"),
        session_manager=SimpleNamespace(load_session_meta=get_meta),
    )
    output = StringIO()
    await print_exit_summary(app, console=Console(file=output, width=100, color_system=None))
    get_meta.assert_called_once_with("root-saved")
    assert "nova --continue root-saved" in output.getvalue()
    assert "Fix a bug" in output.getvalue()


async def test_exit_read_error_never_claims_session_saved():
    from novacode_cli.ui.exit_summary import print_exit_summary

    app = SimpleNamespace(
        session_state=SimpleNamespace(session_id="s"),
        session_manager=SimpleNamespace(load_session_meta=Mock(side_effect=OSError("unavailable"))),
    )
    output = StringIO()
    await print_exit_summary(app, console=Console(file=output, width=100, color_system=None))
    assert "nova --continue" not in output.getvalue()
    assert "No saved conversation" in output.getvalue()


@pytest.mark.parametrize("crashed", [True, False])
async def test_tui_exit_prints_after_terminal_restore_and_save_only_on_normal_exit(
    monkeypatch, crashed
):
    from novacode_cli.tui import app as app_module

    sequence = []

    class AppBoundary:
        def __init__(self, **kwargs):
            self._exception = None

        async def run_async(self):
            sequence.append("terminal restored")
            if crashed:
                self._exception = RuntimeError("crashed")
                raise self._exception

        async def _save_session(self, **kwargs):
            sequence.append("saved")

    async def print_summary(app):
        sequence.append("exit summary")

    monkeypatch.setattr(app_module, "NovaApp", AppBoundary)
    monkeypatch.setattr("novacode_cli.ui.exit_summary.print_exit_summary", print_summary)
    kwargs = dict(
        agent=None,
        assistant_id=None,
        session_state=None,
        backend=None,
        token_tracker=None,
        image_tracker=None,
        model_name=None,
    )
    if crashed:
        with pytest.raises(RuntimeError, match="crashed"):
            await app_module.run_tui(**kwargs)
        assert sequence == ["terminal restored", "saved"]
    else:
        await app_module.run_tui(**kwargs)
        assert sequence == ["terminal restored", "saved", "exit summary"]


def test_exit_summary_calls_a_method_the_session_manager_has():
    """A mocked manager accepted any name, so a misspelt lookup hid behind the
    summary's own error handling and every exit read "No saved conversation"."""
    import inspect

    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.ui import exit_summary

    assert callable(SessionManager.load_session_meta)
    assert "manager.load_session_meta" in inspect.getsource(exit_summary.print_exit_summary)
