"""Microphone capture via sounddevice → 16 kHz mono int16 frames.

The PortAudio callback runs on its own thread; frames are pushed onto a
thread-safe :class:`queue.Queue` and read back in async code with
``asyncio.to_thread(queue.get)``, so the Textual event loop is never blocked.

Heavy imports are deferred to :meth:`AudioCapture.start` so this module imports
cleanly when ``sounddevice`` isn't installed.
"""

from __future__ import annotations

import logging
import queue
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("nova.audio.capture")

#: Whisper and Silero both operate on 16 kHz mono audio.
SAMPLE_RATE = 16_000
#: ~32 ms per block — small enough for responsive VAD endpointing.
BLOCK_SIZE = 512
_QUEUE_MAX = 256  # cap backlog so a stalled consumer can't grow memory unbounded


class AudioCapture:
    """Streams microphone audio as int16 numpy blocks on a background thread."""

    def __init__(self, *, samplerate: int = SAMPLE_RATE, block_size: int = BLOCK_SIZE) -> None:
        """Configure capture; no device is opened until :meth:`start`."""
        self._samplerate = samplerate
        self._block_size = block_size
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=_QUEUE_MAX)
        self._stream: Any = None

    @property
    def samplerate(self) -> int:
        """The capture sample rate (Hz)."""
        return self._samplerate

    def start(self) -> None:
        """Open the input stream and begin filling the frame queue (idempotent)."""
        if self._stream is not None:
            return
        import sounddevice as sd

        # Pre-flight: force PortAudio DLL to load so a missing procedure
        # surfaces here with a clear message, not deep inside the callback.
        try:
            sd.check_input_settings(device=None)
        except Exception as exc:
            msg = str(exc)
            logger.error("PortAudio check failed: %s", msg)
            raise OSError(f"sounddevice / PortAudio check failed: {msg}") from exc

        # If no default input device exists, try finding one by name.
        no_device = sd.default.device is None or (
            isinstance(sd.default.device, tuple) and sd.default.device[0] is None
        )
        if no_device:
            msg = (
                "No default input device found. Try a specific device with:\n"
                '  python -c "import sounddevice as sd; sd.InputStream(device=1).start()"'
            )
            raise OSError(msg)

        def _callback(indata: np.ndarray, _frames: int, _time: object, status: object) -> None:
            if status:
                logger.warning("Audio capture status: %s", status)
            try:
                self._queue.put_nowait(indata.copy().reshape(-1))
            except queue.Full:
                # Drop the oldest block to stay realtime under backpressure.
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(indata.copy().reshape(-1))
                except queue.Empty:
                    pass

        try:
            self._stream = sd.InputStream(
                samplerate=self._samplerate,
                blocksize=self._block_size,
                channels=1,
                dtype="int16",
                callback=_callback,
            )
            self._stream.start()
        except Exception as exc:
            msg = str(exc)
            logger.error("PortAudio stream start failed: %s", msg)
            # WinError 127 means PortAudio's bundled DLL tried to call a Windows
            # API function that doesn't exist on this system. This usually happens
            # when the wrong Python/nova is on PATH (e.g. conda shadows uv tool).
            from novacode_cli.audio import diagnose_voice

            try:
                diag = "\n".join(diagnose_voice())
            except Exception:
                diag = "(diagnostics unavailable)"
            from novacode_cli.audio import install_hint, install_line

            hint = (
                f"Microphone stream failed: {msg}\n\n"
                "  Run /voice doctor for full diagnostics, or reinstall the voice packages:\n"
                f"    {install_line(install_hint())}\n\n"
                f"[dim]{diag}[/dim]"
            )
            raise OSError(hint) from exc

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        """Block (off the event loop) for the next audio block, or ``None`` on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        """Discard any buffered blocks (e.g. before a fresh utterance)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        """Stop and close the input stream and clear the queue."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self.drain()
