"""`/statusline` over the socket: a toggle form instead of a picker.

The Textual UI asks the same question with a multi-select list. A socket client
cannot answer "a set" without inventing an envelope, so the form carries one
toggle per field and the answer is a field-keyed map of booleans — the same
shape a typed form already returns, with the values as decisions.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui import port_server
from novacode_cli.ui.port_server import ServerPort
from novacode_cli.ui.screen_specs import statusline_spec
from novacode_cli.ui.statusline_fields import DEFAULT_FIELDS, FIELDS, selected_fields

ROOT = Path(__file__).resolve().parents[1]


def _worker() -> SimpleNamespace:
    """A worker stand-in; `/statusline` reads no session facts."""
    return SimpleNamespace(
        agent=object(),
        session_state=SimpleNamespace(
            thread_id="t1", session_id="s1", auto_approve=False, todos=[], headless_out_fd=None
        ),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record persisted field lists instead of writing the user's config."""
    written: list[list[str]] = []
    monkeypatch.setattr(port_server, "_persist_statusline", written.append)
    monkeypatch.setattr(port_server, "_persisted_statusline", lambda: None)
    return written


def _port(answer: object = None) -> tuple[ServerPort, list[Any]]:
    """A port whose screen requests are captured."""
    asked: list[Any] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 — the answer is the caller's
        asked.append(spec)
        return answer

    sent: list[dict[str, Any]] = []
    return ServerPort(sent.append, _worker(), ask_screen=ask_screen), asked


def test_the_spec_is_one_toggle_per_field() -> None:
    spec = statusline_spec()
    assert spec.kind == "form"
    fields = spec.payload["fields"]
    assert len(fields) == len(FIELDS)
    assert all(field["toggle"] is True for field in fields)
    assert [field["key"] for field in fields] == list(FIELDS)
    on = {field["key"] for field in fields if field["value"] == "on"}
    assert on == set(DEFAULT_FIELDS)


def test_the_spec_honours_an_existing_selection() -> None:
    spec = statusline_spec(("model", "branch"))
    on = [field["key"] for field in spec.payload["fields"] if field["value"] == "on"]
    assert on == ["model", "branch"]


def test_an_empty_selection_stays_empty() -> None:
    spec = statusline_spec(())
    assert not [field for field in spec.payload["fields"] if field["value"] == "on"]


def test_unknown_persisted_fields_are_dropped() -> None:
    assert selected_fields(["model", "not-a-field"]) == ("model",)
    assert selected_fields("not a list") == DEFAULT_FIELDS


async def test_the_answer_becomes_the_persisted_selection(stored: list[list[str]]) -> None:
    port, asked = _port(answer={"model": True, "branch": True, "activity": False})
    result = await port.run("/statusline", None)
    assert len(asked) == 1
    assert result.ok is True
    # Field order follows FIELDS, so the status bar cannot reorder itself by accident.
    assert stored == [["model", "branch"]]
    assert "model, branch" in result.blocks[0].text


async def test_a_cancelled_form_changes_nothing(stored: list[list[str]]) -> None:
    port, asked = _port(answer=None)
    result = await port.run("statusline", None)
    assert len(asked) == 1
    assert "unchanged" in result.message
    assert stored == []


async def test_arguments_are_rejected_with_the_usage_line() -> None:
    port, asked = _port()
    result = await port.run("/statusline", "everything")
    assert result.ok is False
    assert result.message == "Usage: /statusline"
    assert asked == []


def test_the_field_table_does_not_import_textual() -> None:
    """The server reads this module; it must stay framework-free (ADR-003)."""
    code = "import sys, novacode_cli.ui.statusline_fields; print('textual' in sys.modules)"
    done = subprocess.run(  # noqa: S603 — fixed interpreter and module, no shell
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert done.stdout.strip() == "False"
