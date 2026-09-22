"""Hook adapter that bridges Nova-Code hook events to the Vixie desktop pet.

This script is registered as a hook command in ~/.nova/hooks.json.
Nova-Code fires it for each matching event, piping a JSON payload on stdin.
The adapter forwards these events as WebSocket messages to the running Vixie
avatar, enabling rich real-time visual feedback beyond basic state changes.

Usage in ~/.nova/hooks.json:
    {
      "hooks": [{
        "command": ["python", "-m", "novacode_cli.vixie.hook_adapter"],
        "events": [
          "session.start", "session.end", "agent.message",
          "tool.call", "tool.result", "error", "user.message"
        ]
      }]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

try:
    import websockets
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _load_config() -> tuple[str, int]:
    config_path = Path.home() / ".nova" / "vixie.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            return data.get("host", DEFAULT_HOST), data.get("port", DEFAULT_PORT)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_HOST, DEFAULT_PORT


def _map_event(event_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if event_name == "session.start":
        return {"event_type": "state_update", "data": {"state": "idle"}, "message": "Nova session started", "popup_state": "idle"}
    elif event_name == "session.end":
        return {"event_type": "state_update", "data": {"state": "idle"}, "message": "Nova session ended", "popup_state": "idle"}
    elif event_name == "session.save":
        return {"event_type": "state_update", "data": {"state": "idle"}, "message": "Session saved", "popup_state": "idle"}
    elif event_name == "session.continue":
        return {"event_type": "state_update", "data": {"state": "idle"}, "message": "Session continued", "popup_state": "idle"}
    elif event_name == "agent.message":
        text = payload.get("message", "")
        display = text[:80] + "\u2026" if len(text) > 80 else text
        return {"event_type": "state_update", "data": {"state": "working"}, "message": display, "popup_state": "working"}
    elif event_name == "tool.call":
        tool = payload.get("tool", "unknown")
        return {"event_type": "state_update", "data": {"state": "working"}, "message": tool, "popup_state": "working"}
    elif event_name == "tool.result":
        status = payload.get("status", "")
        tool = payload.get("tool", "")
        if status == "error":
            return {"event_type": "state_update", "data": {"state": "error"}, "message": f"{tool} failed", "popup_state": "error"}
        return {"event_type": "task_completed", "data": {"task_name": tool}}
    elif event_name == "error":
        error_msg = payload.get("error", "Unknown error")
        display = error_msg[:60] + "\u2026" if len(error_msg) > 60 else error_msg
        return {"event_type": "state_update", "data": {"state": "error"}, "message": display, "popup_state": "error"}
    elif event_name == "user.message":
        return {"event_type": "state_update", "data": {"state": "user_input"}, "message": "Waiting for input\u2026", "popup_state": "user_input"}
    elif event_name == "model.switch":
        model = payload.get("model", "unknown")
        return {"event_type": "state_update", "data": {"state": "idle"}, "message": f"Switched to {model}", "popup_state": "idle"}
    elif event_name == "context.warning":
        level = payload.get("level", "warning")
        return {"event_type": "state_update", "data": {"state": "thinking"}, "message": f"Context {level}", "popup_state": "thinking"}
    return None


async def _send_via_websocket(url: str, message: str) -> None:
    if not _HAS_WEBSOCKETS:
        return
    try:
        # Local socket: fail fast when the pet is busy or absent (default open
        # timeout is 10s, and every tool call spawns one of these).
        async with websockets.connect(url, open_timeout=2, close_timeout=2) as ws:
            await ws.send(message)
    except (ConnectionRefusedError, OSError):
        pass
    except Exception:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    message_dict = _map_event(payload.get("event", ""), payload)
    if message_dict is None:
        return

    message = json.dumps(message_dict)
    host, port = _load_config()
    url = f"ws://{host}:{port}"

    try:
        asyncio.run(_send_via_websocket(url, message))
    except Exception:
        pass


if __name__ == "__main__":
    main()
