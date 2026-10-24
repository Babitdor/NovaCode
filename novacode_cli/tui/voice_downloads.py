"""Compact, thread-message-driven progress for local voice models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.filesize import decimal
from rich.text import Text
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import ProgressBar, Static

from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from novacode_cli.audio.downloads import ModelDownload


class VoiceModelProgress(Message):
    """A model worker reports progress; only the UI thread changes widgets."""

    def __init__(self, progress: ModelDownload, owner: object | None = None) -> None:
        """Keep the owning pipeline with each worker update."""
        super().__init__()
        self.progress = progress
        self.owner = owner


class VoiceDownloads(Vertical):
    """One compact row per active STT/TTS model, hidden when inactive."""

    DEFAULT_CSS = """
    VoiceDownloads { display: none; height: auto; padding: 0 1; }
    VoiceDownloads .voice-download-entry { display: none; height: auto; }
    VoiceDownloads Static { height: 1; overflow: hidden hidden; }
    VoiceDownloads ProgressBar { height: 1; width: 100%; }
    VoiceDownloads Bar { width: 1fr; }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- Textual widget API.
        """Mount a hidden pair of progress rows."""
        super().__init__(id=id)
        self._operations: dict[str, ModelDownload] = {}

    def on_voice_model_progress(self, message: VoiceModelProgress) -> None:
        """Handle progress on this widget's pump even while an app action awaits STT."""
        message.stop()
        if message.owner is not None and message.owner is not getattr(
            self.app, "_voice_pipeline", None
        ):
            self.dismiss(message.progress.operation_id)
            return
        self.update_download(message.progress)

    def compose(self) -> ComposeResult:
        """Create one label and bar for each voice direction."""
        for kind in ("stt", "tts"):
            with Vertical(id=f"voice-{kind}-download", classes="voice-download-entry"):
                yield Static("", id=f"voice-{kind}-download-label")
                yield ProgressBar(total=None, show_eta=False, id=f"voice-{kind}-download-bar")

    def dismiss(self, operation_id: str) -> None:
        """Dismiss only the matching operation, never a newer model's progress."""
        for kind, current in self._operations.items():
            if current.operation_id == operation_id:
                self.query_one(f"#voice-{kind}-download").display = False
        self.display = any(row.display for row in self.query(".voice-download-entry"))

    def update_download(self, event: ModelDownload) -> None:
        """Render real totals, or an indeterminate bar when totals are unknown."""
        kind = event.kind.lower()
        if kind not in {"stt", "tts"}:
            return
        current = self._operations.get(kind)
        if current is not None and current.sequence > event.sequence:
            return
        if (current is None or current.operation_id != event.operation_id) and event.state not in {
            "loading",
            "downloading",
        }:
            return
        self._operations[kind] = event
        if event.state == "cancelled":
            self.dismiss(event.operation_id)
            return
        providers = STT_PROVIDERS if kind == "stt" else TTS_PROVIDERS
        provider = providers.get(event.provider, {}).get("name", event.provider)
        title = f"{event.kind} · {provider}"
        bar = self.query_one(f"#voice-{kind}-download-bar", ProgressBar)
        bar.display = event.state in {"loading", "downloading"}
        style = "cyan"
        if event.state == "error":
            title += f" — failed: {' '.join(event.error.split())}"
            style = "red"
        elif event.state == "ready":
            title += " — ready"
            style = "green"
            self.set_timer(3, lambda: self.dismiss(event.operation_id))
        elif event.state == "loading":
            title += " — preparing model…"
            bar.update(total=None, progress=0)
        else:
            title += f" · {event.filename} — {decimal(event.completed)}"
            if event.total:
                title += f" / {decimal(event.total)}"
            else:
                title += " downloaded"
            bar.update(total=event.total, progress=event.completed)
        self.query_one(f"#voice-{kind}-download-label", Static).update(Text(title, style=style))
        self.query_one(f"#voice-{kind}-download").display = True
        self.display = True
