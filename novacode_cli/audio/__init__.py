"""Local, open-source voice I/O for Nova — STT + VAD + TTS.

Speak prompts and hear Nova's prose replies with **no cloud dependency**:

- STT  : Faster-Whisper (CUDA when available, CPU fallback)
- VAD  : Silero VAD (utterance endpointing)
- TTS  : Piper (CPU, low latency)
- Audio: sounddevice (PortAudio capture + playback)

Every heavy dependency is **optional**. This package imports cleanly without any
of them installed; :func:`is_voice_available` reports what's missing so the
``/voice`` command can print an install hint instead of crashing.

Install with the command :func:`install_hint` prints: it matches how this Nova
was installed (see :mod:`novacode_cli.install_commands`). From a checkout,
``uv sync --group dev`` includes all dev + voice deps."""

from __future__ import annotations

import importlib.util
import os


def _has(module: str) -> bool:
    """Return whether ``module`` is importable, without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# Probed at import time (cheap — find_spec doesn't execute the module).
_HAS_SOUNDDEVICE = _has("sounddevice")
_HAS_FASTER_WHISPER = _has("faster_whisper")
_HAS_SILERO_VAD = _has("silero_vad")
_HAS_PIPER = _has("piper")  # the `piper-tts` distribution imports as `piper`
# Optional — NOT part of the required base stack (heavy 3B LLM TTS, opt-in).
_HAS_ORPHEUS = _has("orpheus_cpp")
_HAS_POCKET = _has("pocket_tts")

#: Distribution name -> present? (distribution names are what users `pip install`).
_REQUIRED: dict[str, bool] = {
    "sounddevice": _HAS_SOUNDDEVICE,
    "faster-whisper": _HAS_FASTER_WHISPER,
    "silero-vad": _HAS_SILERO_VAD,
    "piper-tts": _HAS_PIPER,
}


def missing_deps() -> list[str]:
    """Return the pip-install names of any voice dependencies that are absent."""
    return [name for name, present in _REQUIRED.items() if not present]


def is_voice_available() -> bool:
    """Return whether the full local voice stack is importable."""
    return not missing_deps()


def is_orpheus_available() -> bool:
    """Return whether the optional Orpheus TTS backend (orpheus-cpp) is installed."""
    return _HAS_ORPHEUS


def is_pocket_available() -> bool:
    """Return whether the optional Pocket TTS backend (pocket-tts) is installed."""
    return _HAS_POCKET


def is_kokoro_available() -> bool:
    """Return whether the optional Kokoro runtime is installed."""
    return _has("kokoro_onnx")


def is_sano_available() -> bool:
    """Return whether the optional sanoTTS runtime is installed."""
    return _has("sanotts")


def _hint(what: str, packages: list[str], extra: str, **options: str) -> str:
    from novacode_cli.install_commands import install_command

    command = install_command(packages, extra, **options)
    return f"{what} Install with:\n  {command}\nthen restart Nova."


def install_line(hint: str) -> str:
    """The command alone, from one of the hints below."""
    return hint.splitlines()[1].strip()


def orpheus_install_hint() -> str:
    """User-facing hint for enabling the Orpheus TTS provider."""
    return _hint(
        "Orpheus needs orpheus-cpp and llama-cpp-python.",
        ["orpheus-cpp", "llama-cpp-python"],
        "voice-orpheus",
        index_url="https://abetlen.github.io/llama-cpp-python/whl/cpu",
    )


def pocket_install_hint() -> str:
    """User-facing hint for enabling the Pocket TTS provider."""
    return _hint("Pocket-TTS needs the pocket-tts package.", ["pocket-tts"], "voice-pocket")


def kokoro_install_hint() -> str:
    """User-facing hint for the optional CPU Kokoro runtime."""
    return _hint("Kokoro needs kokoro-onnx.", ["kokoro-onnx==0.4.9"], "voice-kokoro")


def sano_install_hint() -> str:
    """User-facing hint for the optional tiny NumPy TTS runtime."""
    return _hint("sanoTTS needs sanotts.", ["sanotts==0.3.0"], "voice-sano")


def parakeet_install_hint() -> str:
    """User-facing hint for enabling the Parakeet STT provider."""
    return _hint(
        "Parakeet needs the sherpa-onnx package.", ["sherpa-onnx>=1.10.0,<2"], "voice-parakeet"
    )


def whistle_install_hint() -> str:
    """User-facing hint for enabling the Whistle STT provider."""
    return _hint("Whistle needs the cactus-needle package.", ["cactus-needle"], "voice-whistle")


def install_hint() -> str:
    """User-facing hint for enabling voice: what is missing and one command."""
    missing = missing_deps() or list(_REQUIRED)
    return _hint(f"Nova voice needs {', '.join(missing)}.", missing, "voice")


def diagnose_voice() -> list[str]:
    """Return diagnostic lines about the voice stack.

    Safe to call even when voice deps are missing — probes importability
    first, then tries deeper checks (DLL loading, version, location).
    Each line is a pre-formatted string ready for ``rich.Text`` or ``print``.
    """
    import importlib
    import sys

    lines: list[str] = []

    # ── Python + nova location ──────────────────────────────────────────
    lines.append(f"  Python:  {sys.executable}")
    nova_exe = _which_nova()
    if nova_exe:
        lines.append(f"  nova:    {nova_exe}")
        # Check for PATH mismatch: nova resolves to a different env than
        # the Python we're running in.
        nova_lower = nova_exe.lower()
        py_lower = sys.executable.lower()
        if "uv" in py_lower and "conda" in nova_lower:
            lines.append(
                "  [WARN] PATH MISMATCH: 'nova' resolves to conda but Python is uv.\n"
                "    The conda nova.exe was deleted but your shell still\n"
                "    has it cached. Run: hash -r   (bash) or open a new terminal."
            )
        elif "conda" in py_lower and ".local" in nova_lower:
            lines.append(
                "  [WARN] PATH MISMATCH: 'nova' is uv tool but Python is conda.\n"
                "    Run 'nova' from a fresh terminal so uv tool's Python is used."
            )
        elif "conda" in nova_lower or "miniconda" in nova_lower:
            lines.append(
                "  [WARN] nova resolves to conda environment.\n"
                "    For voice to work, install with:\n"
                f"      {install_line(install_hint())}"
            )
    lines.append(f"  cwd:     {os.getcwd()}")

    # ── Dependency status ───────────────────────────────────────────────
    for dep_name, mod_name, pkg_name in [
        ("sounddevice", "sounddevice", "sounddevice"),
        ("Faster-Whisper", "faster_whisper", "faster-whisper"),
        ("silero-vad", "silero_vad", "silero-vad"),
        ("piper-tts", "piper", "piper-tts"),
        ("sherpa-onnx (optional)", "sherpa_onnx", "sherpa-onnx"),
        ("pocket-tts (optional)", "pocket_tts", "pocket-tts"),
    ]:
        present = _has(mod_name)
        if present:
            try:
                mod = importlib.import_module(mod_name)
                ver = getattr(mod, "__version__", "?")
                loc = getattr(mod, "__file__", "?")
                lines.append(f"  [OK] {dep_name} {ver}  ({loc})")
            except Exception:  # noqa: BLE001
                lines.append(f"  [OK] {dep_name}  (importable)")
        else:
            lines.append(f"  [..] {dep_name}  (not found)")

    # ── PortAudio DLL probe ─────────────────────────────────────────────
    if _HAS_SOUNDDEVICE:
        try:
            import sounddevice as sd  # type: ignore[import-untyped]

            dll_path = sd._libname
            _probe_portaudio_dll(dll_path, lines)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  \u2717 PortAudio probe failed: {exc}")

    return lines


def _probe_portaudio_dll(dll_path: str, lines: list[str]) -> None:
    """Probe the PortAudio DLL and append diagnostic lines."""
    import ctypes
    import platform

    lines.append(f"  PortAudio DLL: {dll_path}")

    # Does the file exist?
    from pathlib import Path

    p = Path(dll_path)
    if not p.exists():
        lines.append(f"    [..] file not found on disk")
        return

    lines.append(f"    size: {_fmt_bytes(p.stat().st_size)}")

    # Try loading with ctypes (catches WinError 127 early).
    try:
        handle = ctypes.CDLL(dll_path)
        lines.append(f"    [OK] DLL loaded (handle={handle._handle:#x})")
    except OSError as exc:
        err_code = getattr(exc, "winerror", None)
        if err_code == 127:
            bits = platform.architecture()[0]
            lines.append(
                f"    [FAIL] DLL failed to load (WinError 127)\n"
                f"    [red]The PortAudio DLL can't find a required Windows API.\n"
                f"    This usually means:\n"
                f"      1. You are running a different Python/nova than the one\n"
                f"         where sounddevice was installed (e.g. conda shadows uv tool).\n"
                f"      2. The PortAudio binary is too old for your Windows version.\n"
                f"    Fix:[/red]\n"
                f"      which nova  # check which nova is on PATH\n"
                f"      {install_line(install_hint())}"
            )
        else:
            lines.append(f"    [FAIL] DLL error (errno={err_code}): {exc}")
        return

    # Try opening the default input device.
    try:
        import sounddevice as sd  # type: ignore[import-untyped]

        sd.check_input_settings(device=None)
        lines.append(f"    [OK] default input device accessible")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"    [FAIL] default input device: {exc}")


def _which_nova() -> str | None:
    """Return the resolved path to the ``nova`` executable on PATH."""
    import shutil

    # shutil.which follows PATHEXT; filter out .bat/.cmd wrappers that
    # just delegate to pip-installed nova.
    for candidate in ("nova", "nova.exe", "nova.bat", "nova.cmd"):
        path = shutil.which(candidate)
        if path is None:
            continue
        _, ext = os.path.splitext(path.lower())
        # Prefer .exe over .bat/.cmd wrappers; only accept .bat if
        # no .exe exists.
        if ext == ".exe":
            return path
        if ext not in (".bat", ".cmd"):
            return path
    # Fallback — return whatever shutil.which finds first.
    return shutil.which("nova")


def _fmt_bytes(n: int) -> str:
    """Format a byte count as human-readable."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


__all__ = [
    "diagnose_voice",
    "is_kokoro_available",
    "is_orpheus_available",
    "is_pocket_available",
    "is_sano_available",
    "is_voice_available",
    "kokoro_install_hint",
    "missing_deps",
    "orpheus_install_hint",
    "pocket_install_hint",
    "sano_install_hint",
]
