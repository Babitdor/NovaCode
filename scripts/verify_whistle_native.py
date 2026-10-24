"""Opt-in native Whistle smoke test; downloads only into a disposable cache."""
# ruff: noqa: T201, S603, PLR2004 -- proof output and a fixed child-process CLI.

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(root: Path) -> None:
    """Load the actual native runtime and verify a silent one-second utterance."""
    os.environ["HF_HOME"] = str(root / "hf")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ.pop("NEEDLE_WHISTLE_WEIGHTS", None)
    os.environ.pop("NEEDLE3_LIB_PATH", None)

    import numpy as np
    from needle.agent import fetch

    from novacode_cli.audio.stt_whistle import WhistleTranscriber

    def cache_dir(generation: int | str = 2) -> str:
        path = root / "runtime" / str(generation)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    fetch.cache_dir = cache_dir
    print("cactus-needle", version("cactus-needle"), flush=True)
    transcriber = WhistleTranscriber()
    result = asyncio.run(transcriber.transcribe(np.zeros(16_000, dtype=np.int16)))
    if result:
        msg = f"Silence unexpectedly produced a transcript: {result!r}"
        raise AssertionError(msg)
    print("Native Whistle inference passed: one second of silence -> empty text.", flush=True)


def main() -> None:
    """Use a child process so Windows releases its DLL before cache cleanup."""
    with tempfile.TemporaryDirectory(prefix="nova-whistle-proof-") as directory:
        subprocess.run(
            [sys.executable, __file__, "--worker-cache", directory],
            check=True,
            timeout=180,
        )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker-cache":
        verify(Path(sys.argv[2]))
    else:
        main()
