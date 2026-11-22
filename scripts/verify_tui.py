"""Run every parity gate for the Textual → ratatui port, in order.

`rust/Plan.md` names `make rust-test`, `make rust-frames` and a pytest set. Those
targets assume `make`, which is not installed on every workstation this project is
built on, so the gates live here instead: one command, one exit code, and a summary
that says which gate failed rather than burying it in tool output.

Usage:
    uv run python scripts/verify_tui.py            # everything
    uv run python scripts/verify_tui.py --fast     # skip the Rust workspace
    uv run python scripts/verify_tui.py --list     # show the gates
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST = ROOT / "rust"

# (label, argv, cwd) — in dependency order: generators first, so a stale table
# fails before the tests that would otherwise "pass" against the stale data.
GATES: tuple[tuple[str, tuple[str, ...], Path], ...] = (
    (
        "catalogue drift",
        ("uv", "run", "python", "scripts/gen_tui_command_catalog.py", "--check"),
        ROOT,
    ),
    ("theme table drift", ("uv", "run", "python", "scripts/gen_theme_table.py", "--check"), ROOT),
    (
        "python: commands + protocol",
        (
            "uv",
            "run",
            "pytest",
            "tests/test_tui_command_catalog.py",
            "tests/test_tui_command_port.py",
            "tests/test_tui_port_commands.py",
            "tests/test_tui_auto_approve_command.py",
            "tests/test_tui_goal_command.py",
            "tests/test_tui_settings_command.py",
            "tests/test_tui_preferences_command.py",
            "tests/test_tui_tabs_command.py",
            "tests/test_tui_council_settings_command.py",
            "tests/test_tui_proactive_command.py",
            "tests/test_tui_council_view_command.py",
            "tests/test_tui_teammates_command.py",
            "tests/test_tui_rooms_command.py",
            "tests/test_tui_statusline_command.py",
            "tests/test_tui_theme_command.py",
            "tests/test_tui_screen_specs.py",
            "tests/test_tui_server_protocol.py",
            "tests/test_tui_server_transport.py",
            "tests/test_tui_child_sessions.py",
            "tests/test_tui_multi_session.py",
            "tests/test_tui_no_textual_in_server.py",
            "tests/test_ledger_gate.py",
            "-q",
        ),
        ROOT,
    ),
    ("rust: format", ("cargo", "fmt", "--check"), RUST),
    (
        "rust: lints",
        ("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"),
        RUST,
    ),
    ("rust: tests", ("cargo", "test", "--workspace"), RUST),
)

# A hung gate must fail the gate, not wedge the whole verification.
GATE_TIMEOUT = 1200

# The gates `--fast` leaves out, by label.
RUST_LABELS = ("rust: format", "rust: lints", "rust: tests")


def run(gate: tuple[str, tuple[str, ...], Path]) -> tuple[bool, float, str]:
    """Run one gate.

    Args:
        gate: Label, argv and working directory.

    Returns:
        `(passed, seconds, tail)`, where `tail` is the last part of the output.
    """
    _label, argv, cwd = gate
    started = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S603 — fixed argument lists, no shell
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GATE_TIMEOUT,
        )
    except FileNotFoundError:
        return False, time.monotonic() - started, f"{argv[0]} is not installed"
    except subprocess.TimeoutExpired:
        return (
            False,
            time.monotonic() - started,
            f"gate exceeded its {GATE_TIMEOUT:.0f}s timeout and was killed",
        )
    elapsed = time.monotonic() - started
    output = (done.stdout or "") + (done.stderr or "")
    return done.returncode == 0, elapsed, output.strip()


def main(argv: list[str] | None = None) -> int:
    """Run the gates and print a summary.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        `0` when every selected gate passed, `1` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="skip the Rust workspace gates")
    parser.add_argument("--list", action="store_true", help="print the gate names and exit")
    args = parser.parse_args(argv)

    selected = [gate for gate in GATES if not (args.fast and gate[0] in RUST_LABELS)]
    if args.list:
        for label, command, _ in selected:
            sys.stdout.write(f"{label:<28} {' '.join(command)}\n")
        return 0

    failures: list[str] = []
    for gate in selected:
        label = gate[0]
        sys.stdout.write(f"· {label:<28} ".ljust(32))
        sys.stdout.flush()
        passed, seconds, tail = run(gate)
        sys.stdout.write(f"{seconds:5.1f}s  {'ok' if passed else 'FAILED'}\n")
        if not passed:
            failures.append(f"{label}:\n{tail[-2000:]}")

    if failures:
        sys.stdout.write("\nfailed gates:\n")
        for failure in failures:
            sys.stdout.write(f"\n{failure}\n")
        return 1
    sys.stdout.write(f"\nall {len(selected)} gates passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
