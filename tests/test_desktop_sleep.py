"""The overnight "park the machine" tool: what it runs, and when it refuses.

`sleep_desktop` is the one tool that acts on the machine rather than the repo, so
these pin what makes it safe to leave bound every turn: the overnight window, the
refusal outside it, and the fact that no test here ever suspends anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from novacode_cli.tools import desktop_tools, sleep_desktop

if TYPE_CHECKING:
    from pathlib import Path

#: The developer's own zone. The tool localises the clock it reads, so a frozen
#: instant has to be in this zone or the conversion would shift the hour the
#: overnight window is about.
_LOCAL_TZ = datetime.now(UTC).astimezone().tzinfo


def _freeze(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """Pin the clock the tool reads, so the overnight window is testable."""
    monkeypatch.setattr(desktop_tools, "datetime", SimpleNamespace(now=lambda: when))


def _at(hour: int, minute: int = 0) -> datetime:
    """A fixed instant on the local wall clock."""
    return datetime(2026, 9, 29, hour, minute, tzinfo=_LOCAL_TZ)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture what would be spawned, so nothing here suspends anything.

    Stubbing the spawn is the only thing between these tests and actually parking
    the developer's desktop.
    """
    calls: list[list[str]] = []

    def fake_spawn(command: list[str], _log_path: Path) -> int:
        calls.append(command)
        return 4242

    monkeypatch.setattr(desktop_tools, "_spawn", fake_spawn)
    return calls


def test_sleep_desktop_is_bound_every_turn() -> None:
    """A deferred tool needs a `search_tools` round-trip, which this one cannot have.

    The prompt ends an overnight session with it, and a prompt instruction naming a
    tool the model cannot see is a dead instruction.
    """
    from novacode_cli.agents.tool_search import CORE_TOOLS

    assert "sleep_desktop" in CORE_TOOLS


def test_the_prompt_tells_the_agent_when_to_sleep_the_desktop() -> None:
    from novacode_cli.prompts import render_template

    out = render_template("core_agent_system.jinja")

    assert "<desktop_power>" in out
    assert "</desktop_power>" in out
    assert "sleep_desktop" in out
    # The window is the whole safety story, so it has to be stated outright.
    assert "23:00-07:00" in out
    # And the clock is not in context, so the agent must be told to look at it.
    assert "Get-Date" in out
    # The triggers are useless without the non-triggers.
    assert "Do not call it" in out


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(22, False), (23, True), (0, True), (3, True), (6, True), (7, False), (12, False)],
)
def test_the_overnight_window_wraps_midnight(*, hour: int, expected: bool) -> None:
    assert desktop_tools._in_overnight_window(hour) is expected


def test_it_refuses_in_the_daytime_without_force(
    spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, _at(14, 5))

    message = sleep_desktop.invoke({"reason": "test"})

    assert "Not sleeping the desktop" in message
    assert "14:05" in message
    assert "force=True" in message
    assert spawned == [], "a refusal must not schedule anything"


def test_force_schedules_it_outside_the_window(
    spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, _at(14, 5))

    message = sleep_desktop.invoke({"reason": "user asked", "force": True})

    assert len(spawned) == 1
    assert "pid 4242" in message


def test_the_overnight_call_suspends_with_a_grace_period(
    spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, _at(1, 30))

    message = sleep_desktop.invoke({"reason": "overnight session done"})

    assert len(spawned) == 1
    script = spawned[0][-1]
    # A true suspend. The usual `rundll32 powrprof.dll,SetSuspendState` one-liner
    # hibernates instead whenever hibernation is enabled, which loses the session.
    assert "SetSuspendState" in script
    assert "PowerState]::Suspend" in script
    assert "powrprof" not in script
    # The grace period is what lets the reply be read before the screen goes dark.
    assert f"Start-Sleep -Seconds {desktop_tools.DEFAULT_DELAY_SECONDS}" in script
    # And the transcript has to say why, and how to call it off.
    assert "overnight session done" in message
    assert "taskkill /PID 4242" in message


def test_an_unsupported_platform_refuses(
    spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, _at(1, 30))
    monkeypatch.setattr(desktop_tools, "_suspend_command", lambda *_a, **_k: None)

    message = sleep_desktop.invoke({})

    assert "no suspend command" in message
    assert spawned == []


def test_a_spawn_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine that refuses to be scheduled must not break the turn.

    The turn is the last one of the night, so an exception here would lose the
    closing message as well as the suspend.
    """
    _freeze(monkeypatch, _at(1, 30))

    def boom(_command: list[str], _log_path: Path) -> int:
        message = "powershell is missing"
        raise OSError(message)

    monkeypatch.setattr(desktop_tools, "_spawn", boom)

    message = sleep_desktop.invoke({})

    assert "Failed to schedule the suspend" in message
    assert "powershell is missing" in message


def test_the_per_platform_commands_are_right() -> None:
    macos = desktop_tools._suspend_command(0, on_windows=False, platform="darwin")
    linux = desktop_tools._suspend_command(5, on_windows=False, platform="linux")
    other = desktop_tools._suspend_command(0, on_windows=False, platform="aix")

    assert macos == ["sh", "-c", "sleep 0; pmset sleepnow"]
    assert linux == ["sh", "-c", "sleep 5; systemctl suspend"]
    assert other is None
