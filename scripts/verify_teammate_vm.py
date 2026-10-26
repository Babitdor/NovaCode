"""Opt-in real Cua/QEMU persistence proof; never touches a user's existing guest.

Run prepare, then verify in a fresh process (and again after a host reboot).
Retain the manifest until explicit cleanup; errors never delete the guest.
"""
# ruff: noqa: S101, T201, EM101, EM102, TRY003

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from novacode_cli.computer.persistent import PersistentComputer

OWNER = "nova-persistence-smoke-v1"
MANIFEST = Path(__file__).resolve().parents[1] / ".tmp" / "teammate-smoke" / "computer.json"
SETUP = r"""
set -eu
printf 'nova-retained-sentinel-v1' > "$HOME/nova-retained-sentinel.txt"
cat > "$HOME/nova-browser-smoke.html" <<'HTML'
<html><body><script>
if(location.search) localStorage.setItem('nova-smoke','browser-retained-v1');
document.body.textContent=localStorage.getItem('nova-smoke')||'MISSING';
</script></body></html>
HTML
"""
BROWSER = r"""
set -eu
browser="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
test -n "$browser" || { echo 'No Chromium browser available for profile proof'; exit 1; }
"$browser" --headless --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --user-data-dir="$HOME/.config/nova-persistence-smoke" --dump-dom \
  "file://$HOME/nova-browser-smoke.htmlQUERY" 2>/dev/null
"""


async def guest_shell(computer: PersistentComputer, command: str) -> str:
    """Use SDK test harness access on the owning loop, never add agent shell tools."""

    async def run() -> str:
        result = await computer._desktop.sh(command, 60_000)
        if not result.exit.success:
            raise RuntimeError(f"Guest proof failed: {result.stderr}")
        return result.stdout.decode("utf-8")

    return await asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(run(), computer._event_loop())
    )


async def verify(action: str) -> None:
    """Write guest/browser sentinels, reconnect cold, or explicitly remove proof guest."""
    import cua

    if cua.__version__ != "0.9.0":
        raise RuntimeError("Persistence proof requires pinned cua==0.9.0")
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    if action != "prepare" and not MANIFEST.exists():
        raise RuntimeError("Run prepare first; no proof guest exists")
    computer = PersistentComputer(MANIFEST, OWNER)
    try:
        if action == "cleanup":
            await computer.call("delete")
            print("Verified owned proof guest deleted")
            return
        print("Starting retained QEMU proof guest", flush=True)
        await computer.call("start")
        if action == "prepare":
            await guest_shell(computer, SETUP)
        output = await guest_shell(
            computer, BROWSER.replace("QUERY", "?write=1" if action == "prepare" else "")
        )
        assert "browser-retained-v1" in output
        assert (
            await guest_shell(computer, 'cat "$HOME/nova-retained-sentinel.txt"')
            == "nova-retained-sentinel-v1"
        )
        status = await computer.call("stop")
        assert status["phase"] == "suspended"
        await computer.call("start")
        assert (
            await guest_shell(computer, 'cat "$HOME/nova-retained-sentinel.txt"')
            == "nova-retained-sentinel-v1"
        )
        assert "browser-retained-v1" in await guest_shell(computer, BROWSER.replace("QUERY", ""))
        await computer.call("stop")
        print(
            json.dumps(
                {
                    "result": "passed",
                    "phase": action,
                    "cua": cua.__version__,
                    "guest": json.loads(MANIFEST.read_text())["name"],
                }
            )
        )
    finally:
        if not await asyncio.to_thread(computer.close):
            print("Suspension failed; retained manifest preserved for inspection")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify", "cleanup"])
    asyncio.run(verify(parser.parse_args().action))
