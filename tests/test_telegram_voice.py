"""The Telegram bridge turns a voice note into an agent prompt.

The load-bearing property is the ordering: a voice note from a chat that is not
allowlisted must never cause a getFile/download, or an unauthorized stranger
could make Nova fetch and decode arbitrary audio just by sending it. The rest
pins that no failure path leaves the sender in silence.
"""

from __future__ import annotations

import asyncio

import pytest

from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
from novacode_cli.remote.telegram_bridge import TelegramBridge


def _bridge(chat_id: int = 99, allowed: set | None = None):
    cfg = BridgeConfig(
        platform=RemotePlatform.TELEGRAM,
        token="tok",
        chat_id=chat_id,
        allowed_ids=allowed or set(),
    )
    return TelegramBridge(cfg, asyncio.Queue())


def _voice_update(chat_id: int, *, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "chat": {"id": chat_id},
            "from": {"username": "babit", "first_name": "Babit"},
            "voice": {"file_id": "FILE123", "duration": 3, "mime_type": "audio/ogg"},
        },
    }


class _Recorder:
    """Stands in for _api_call, recording methods and serving canned results."""

    def __init__(self, bridge, updates):
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[str] = []
        self._updates = updates
        self._served = False
        bridge._api_call = self._api_call

    async def _api_call(self, method, payload, **kw):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"result": {"username": "novabot"}}
        if method == "getUpdates":
            if self._served:
                raise asyncio.CancelledError  # stop the poll loop after one pass
            self._served = True
            return {"result": self._updates}
        if method == "sendMessage":
            self.sent.append(payload.get("text", ""))
            return {"result": {"message_id": 1}}
        if method == "getFile":
            return {"result": {"file_path": "voice/f.oga"}}
        return {"result": {}}

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


async def _run_once(bridge, updates):
    rec = _Recorder(bridge, updates)
    try:
        await bridge.run()
    except asyncio.CancelledError:
        pass
    return rec


# ── The authorization gate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_voice_note_from_an_unauthorized_chat_is_never_downloaded():
    """The property that matters: no fetch/decode for a chat we don't trust."""
    bridge = _bridge(chat_id=99)
    downloaded = []
    bridge._download_file = lambda fid: downloaded.append(fid)  # noqa: ARG005

    rec = await _run_once(bridge, [_voice_update(chat_id=12345)])

    assert downloaded == [], "downloaded audio for a non-allowlisted chat"
    assert "getFile" not in rec.methods
    assert bridge._queue.empty()


@pytest.mark.asyncio
async def test_a_voice_note_from_an_allowlisted_chat_is_transcribed_and_queued(
    monkeypatch,
):
    bridge = _bridge(chat_id=99)

    async def _dl(_fid):
        return b"OGGDATA"

    async def _tx(data, duration=None, stt=None):
        return "add a dry run flag"

    bridge._download_file = _dl
    monkeypatch.setattr(
        "novacode_cli.remote.voice_notes.transcribe_voice_note", _tx
    )

    rec = await _run_once(bridge, [_voice_update(chat_id=99)])

    assert not bridge._queue.empty(), "the transcript never reached the agent"
    msg = bridge._queue.get_nowait()
    assert msg.text == "add a dry run flag"
    assert msg.platform is RemotePlatform.TELEGRAM
    # The sender sees what was heard before it is acted on.
    assert any("add a dry run flag" in s for s in rec.sent)


@pytest.mark.asyncio
async def test_a_plain_text_message_still_works_untouched():
    """The voice branch must not disturb the path every message already takes."""
    bridge = _bridge(chat_id=99)
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 99},
            "from": {"username": "babit"},
            "text": "hello nova",
        },
    }
    rec = await _run_once(bridge, [update])
    assert "getFile" not in rec.methods
    assert bridge._queue.get_nowait().text == "hello nova"


@pytest.mark.asyncio
async def test_a_message_with_neither_text_nor_voice_is_ignored():
    bridge = _bridge(chat_id=99)
    update = {"update_id": 1, "message": {"chat": {"id": 99}, "from": {}, "sticker": {}}}
    await _run_once(bridge, [update])
    assert bridge._queue.empty()


# ── Every failure path tells the sender ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_download_is_reported_not_swallowed():
    bridge = _bridge(chat_id=99)

    async def _dl(_fid):
        return None

    bridge._download_file = _dl
    sent: list[str] = []
    bridge._send_message = lambda cid, text, **_kw: sent.append(text) or asyncio.sleep(0)

    out = await bridge._transcribe_voice(99, {"file_id": "F", "duration": 2})
    assert out == ""
    assert sent and "download" in sent[0].lower()


@pytest.mark.asyncio
async def test_an_overlong_note_tells_the_sender_the_limit(monkeypatch):
    from novacode_cli.remote import voice_notes as vn

    bridge = _bridge(chat_id=99)

    async def _dl(_fid):
        return b"DATA"

    bridge._download_file = _dl
    sent: list[str] = []

    async def _send(cid, text, **_kw):
        sent.append(text)

    bridge._send_message = _send

    out = await bridge._transcribe_voice(
        99, {"file_id": "F", "duration": vn.MAX_DURATION_SECONDS + 5}
    )
    assert out == ""
    assert sent and str(vn.MAX_DURATION_SECONDS) in sent[0]


@pytest.mark.asyncio
async def test_silence_is_reported_rather_than_queued_as_an_empty_prompt(monkeypatch):
    bridge = _bridge(chat_id=99)

    async def _dl(_fid):
        return b"DATA"

    async def _tx(data, duration=None, stt=None):
        return ""

    bridge._download_file = _dl
    monkeypatch.setattr("novacode_cli.remote.voice_notes.transcribe_voice_note", _tx)
    sent: list[str] = []

    async def _send(cid, text, **_kw):
        sent.append(text)

    bridge._send_message = _send

    assert await bridge._transcribe_voice(99, {"file_id": "F", "duration": 2}) == ""
    assert sent and "speech" in sent[0].lower()


@pytest.mark.asyncio
async def test_an_unexpected_transcription_error_does_not_kill_the_bridge(monkeypatch):
    """A crash here must not take down the poll loop for every later message."""
    bridge = _bridge(chat_id=99)

    async def _dl(_fid):
        return b"DATA"

    async def _boom(data, duration=None, stt=None):
        raise RuntimeError("cuda exploded")

    bridge._download_file = _dl
    monkeypatch.setattr("novacode_cli.remote.voice_notes.transcribe_voice_note", _boom)
    sent: list[str] = []

    async def _send(cid, text, **_kw):
        sent.append(text)

    bridge._send_message = _send

    assert await bridge._transcribe_voice(99, {"file_id": "F", "duration": 2}) == ""
    assert sent, "the sender was left in silence after a crash"


@pytest.mark.asyncio
async def test_a_missing_file_id_is_a_quiet_no_op():
    bridge = _bridge(chat_id=99)
    assert await bridge._transcribe_voice(99, {}) == ""


@pytest.mark.asyncio
async def test_end_to_end_real_ogg_through_the_bridge(monkeypatch):
    """The production path with only the model stubbed.

    Everything else is real: a genuine OGG/Opus payload, the real download
    handoff, the real decode and resample, the real reply. The unit tests each
    pass in isolation; this is what proves they join up.
    """
    from tests.test_remote_voice_notes import _ogg_opus_bytes

    ogg = _ogg_opus_bytes(0.5)
    if ogg is None:
        pytest.skip("libopus encoder unavailable in this PyAV build")

    captured = {}

    class _STT:
        async def transcribe(self, pcm):
            captured["pcm"] = pcm
            return "ship the dry run flag"

    monkeypatch.setattr("novacode_cli.remote.voice_notes._load_stt", lambda: _STT())

    bridge = _bridge(chat_id=99)

    async def _dl(_fid):
        return ogg

    bridge._download_file = _dl
    sent: list[str] = []

    async def _send(cid, text, **_kw):
        sent.append(text)

    bridge._send_message = _send

    async def _typing(cid):
        return None

    bridge._trigger_typing = _typing

    text = await bridge._transcribe_voice(99, {"file_id": "F", "duration": 1})

    assert text == "ship the dry run flag"
    # The model really was handed decoded 16 kHz mono int16, not raw OGG bytes.
    pcm = captured["pcm"]
    assert pcm.dtype.name == "int16" and pcm.ndim == 1
    assert 7000 < pcm.size < 9000, f"0.5 s should be ~8000 samples, got {pcm.size}"
    assert any("ship the dry run flag" in s for s in sent)
