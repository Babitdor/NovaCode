"""`/theme` over the socket: the generated table, the picker, and applying.

The theme list must be the Textual UI's own, so the table is generated and
@@--check@@ed rather than transcribed. Applying has three paths — an argument, a
client answer, and a cancelled picker — and each has to end somewhere definite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.server import frames
from novacode_cli.ui import port_server, theme_table
from novacode_cli.ui.port_server import ServerPort
from novacode_cli.ui.screen_specs import theme_spec

ROOT = Path(__file__).resolve().parents[1]


def _generator() -> Any:  # noqa: ANN401 — a module loaded from its path
    """Load `scripts/gen_theme_table.py` so the check runs in-process."""
    path = ROOT / "scripts" / "gen_theme_table.py"
    spec = importlib.util.spec_from_file_location("gen_theme_table", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_theme_table"] = module
    spec.loader.exec_module(module)
    return module


def _worker() -> SimpleNamespace:
    """A worker stand-in; `/theme` reads no session facts."""
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
def persisted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record theme writes instead of touching the user's config file."""
    written: list[str] = []
    monkeypatch.setattr(port_server, "_persist_theme", written.append)
    monkeypatch.setattr(port_server, "_persisted_theme", lambda: None)
    return written


def _port(
    worker: SimpleNamespace | None = None,
    *,
    answer: object = None,
) -> tuple[ServerPort, list[dict[str, Any]], list[Any]]:
    """A port whose frames and screen requests are captured."""
    sent: list[dict[str, Any]] = []
    asked: list[Any] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 — the answer shape is the caller's
        asked.append(spec)
        return answer

    port = ServerPort(sent.append, worker or _worker(), ask_screen=ask_screen)
    return port, sent, asked


def test_the_checked_in_table_matches_its_sources() -> None:
    """A stale table would offer themes the Textual UI does not have."""
    generator = _generator()
    text, count = generator.build()
    assert (ROOT / "novacode_cli" / "ui" / "theme_table.py").read_text(encoding="utf-8") == text
    assert count == len(theme_table.THEMES)


def test_the_table_offers_both_nova_themes_and_the_default() -> None:
    names = set(theme_table.BY_NAME)
    assert {"nova-signal", "nova-classic"} <= names, "Nova's own palettes must survive"
    assert theme_table.DEFAULT_THEME in names
    assert theme_table.BY_NAME[theme_table.DEFAULT_THEME].dark is True


def test_the_picker_marks_the_theme_in_force() -> None:
    spec = theme_spec("nova-signal")
    assert spec.kind == "list"
    items = spec.payload["items"]
    assert len(items) == len(theme_table.THEMES)
    marked = [item for item in items if item["label"].endswith(" *")]
    assert [item["id"] for item in marked] == ["nova-signal"]
    assert all(item["id"] and item["detail"] for item in items)


async def test_theme_with_a_name_applies_and_tells_the_client(persisted: list[str]) -> None:
    port, sent, asked = _port()
    result = await port.run("/theme", "nova-signal")
    assert result.ok is True
    assert result.action == "theme"
    assert persisted == ["nova-signal"]
    assert asked == [], "an explicit name must not open a picker"
    theme_frames = [frame for frame in sent if frame["t"] == "ui_theme"]
    assert len(theme_frames) == 1
    frame = theme_frames[0]
    assert frame["name"] == "nova-signal"
    assert frame["dark"] is True
    assert frame["primary"].startswith("#")


async def test_an_unknown_theme_fails_and_lists_the_real_names(persisted: list[str]) -> None:
    port, sent, _ = _port()
    result = await port.run("theme", "no-such-theme")
    assert result.ok is False
    assert "no-such-theme" in result.message
    assert "nova-signal" in result.message
    assert persisted == []
    assert not [frame for frame in sent if frame["t"] == "ui_theme"]


async def test_bare_theme_asks_and_applies_the_answer(persisted: list[str]) -> None:
    port, sent, asked = _port(answer={"id": "nova-classic"})
    result = await port.run("theme", None)
    assert len(asked) == 1
    assert asked[0].kind == "list"
    assert result.ok is True
    assert persisted == ["nova-classic"]
    assert [frame["name"] for frame in sent if frame["t"] == "ui_theme"] == ["nova-classic"]


async def test_a_cancelled_picker_changes_nothing(persisted: list[str]) -> None:
    port, sent, asked = _port(answer=None)
    result = await port.run("theme", None)
    assert len(asked) == 1
    assert result.ok is True
    assert "unchanged" in result.message
    assert persisted == []
    assert not [frame for frame in sent if frame["t"] == "ui_theme"]


async def test_an_unusable_answer_is_treated_as_a_cancellation(persisted: list[str]) -> None:
    port, _, _ = _port(answer={"nothing": "useful"})
    result = await port.run("theme", None)
    assert result.ok is True
    assert persisted == []


def test_the_ui_theme_frame_omits_empty_colours() -> None:
    frame = frames.ui_theme("x", dark=False, primary="#111111")
    assert frame == {"t": "ui_theme", "name": "x", "dark": False, "primary": "#111111"}
    assert "muted" not in frame, "a theme without text-muted must not send an empty string"
