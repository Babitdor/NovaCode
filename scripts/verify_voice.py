"""Reproduce offline voice integration checks and assertion-killed negative controls."""
# ruff: noqa: T201, S603 -- diagnostic CLI with fixed local test commands.

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_audio",
    "tests/test_tui_voice_output.py",
    "tests/test_voice_catalog.py",
    "tests/test_voice_credentials.py",
    "tests/test_remote_voice_notes.py",
    "tests/test_telegram_voice.py",
    "tests/test_code_mode_toggle.py",
]
NEW_SOURCES = ["novacode_cli/audio/stt_whistle.py", "novacode_cli/audio/playback.py"]
SOURCES = [
    *NEW_SOURCES,
    *[
        f"novacode_cli/audio/{name}.py"
        for name in ["pipeline", "summarize", "tts", "tts_pocket", "tts_orpheus", "stt_elevenlabs"]
    ],
]
NEW_TESTS = [
    "tests/test_audio/test_whistle.py",
    "tests/test_audio/test_voice_safety.py",
    "tests/test_tui_voice_output.py",
]


def run(*args: str) -> None:
    """Run one local check, stopping on failure or timeout."""
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True, timeout=180)


def main() -> None:
    """Verify the focused voice contract and execute each negative control."""
    with tempfile.TemporaryDirectory(prefix="nova-voice-check-") as directory:
        data = "--data-file=" + str(Path(directory) / "coverage")
        run(
            "-m",
            "coverage",
            "run",
            data,
            "--branch",
            "--source=novacode_cli.audio.stt_whistle,novacode_cli.audio.playback,novacode_cli.audio.summarize",
            "-m",
            "pytest",
            *TESTS,
            "-q",
            "--tb=short",
            "--random-order",
            "--random-order-seed=24102026",
        )
        run("-m", "coverage", "report", data, "--fail-under=85", "-m")
    run(
        "-m",
        "ruff",
        "check",
        *NEW_SOURCES,
        *NEW_TESTS,
        __file__,
        "scripts/verify_whistle_native.py",
    )
    run(
        "-m",
        "ruff",
        "format",
        "--check",
        *NEW_SOURCES,
        *NEW_TESTS,
        __file__,
        "scripts/verify_whistle_native.py",
    )
    run("-m", "mypy", *SOURCES, "--follow-imports=silent")
    controls = [
        (
            "cancelled synthesis allowed to play",
            "import asyncio; import novacode_cli.audio.playback as m\n"
            "async def bypass(operation):\n"
            "    hits.append(True)\n"
            "    await asyncio.to_thread(operation)\n"
            "m.run_speech = bypass\n",
            "tests/test_audio/test_voice_safety.py::test_cancelled_synthesis_cannot_start_playback",
        ),
        (
            "Whistle native locking removed",
            "import novacode_cli.audio.stt_whistle as m\n"
            "class NoLock:\n"
            "    def __enter__(self): hits.append(True)\n"
            "    def __exit__(self, *args): pass\n"
            "m._RUNTIME_LOCK = NoLock()\n",
            "tests/test_audio/test_whistle.py::test_whistle_calls_are_serialized_across_instances",
        ),
        (
            "structured output stringified for speech",
            "import novacode_cli.audio.summarize as m\n"
            "m.BaseMessage.text = property(lambda self: hits.append(True) or str(self.content))\n",
            "tests/test_audio/test_summarize.py::TestSummarize::test_structured_content_speaks_only_text",
        ),
    ]
    for name, mutation, test in controls:
        code = (
            "import pytest\nhits = []\n"
            + mutation
            + f"result = pytest.main([{test!r}, '-q', '--tb=short'])\n"
            + "if not hits: raise SystemExit(3)\nraise SystemExit(result)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=60, check=False)
        if result.returncode != 1:
            msg = f"Negative control {name!r} did not execute and fail an assertion"
            raise RuntimeError(msg)
        print("Negative control killed:", name, flush=True)


if __name__ == "__main__":
    main()
