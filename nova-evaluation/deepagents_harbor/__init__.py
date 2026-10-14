# Fix Windows asyncio subprocess issue - must be set before any asyncio imports
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Hermetic eval home. NovaCode reads ~/.nova (MCP servers, personal skills,
# prompt overrides, the memory store) via Path.home() in ~27 places, so point the
# process home at an empty directory: a developer's personal setup stays out of
# benchmark runs, and benchmark runs stay out of their memory store. Must happen
# before novacode_cli is imported (it resolves its home at import time).
import os
import tomllib
from pathlib import Path

_real_home = Path.home()
_eval_home = Path(
    os.environ.get("NOVA_EVAL_HOME") or Path(__file__).resolve().parent.parent / ".eval-home"
).resolve()  # absolute: a child process started in another directory must land in the same home
_eval_home.mkdir(parents=True, exist_ok=True)
# The docker/modal subprocesses harbor spawns still need the real credentials.
os.environ.setdefault("DOCKER_CONFIG", str(_real_home / ".docker"))
if (_real_home / ".modal.toml").exists():
    os.environ.setdefault("MODAL_CONFIG_PATH", str(_real_home / ".modal.toml"))
    # Harbor's Modal preflight checks Path.home(), which becomes the isolated
    # eval home below. Pass the active profile through Modal's supported env
    # variables so both Harbor and the SDK can authenticate without copying
    # the user's credential file into the eval home.
    if not os.environ.get("MODAL_TOKEN_ID") and not os.environ.get("MODAL_TOKEN_SECRET"):
        try:
            profiles = tomllib.loads((_real_home / ".modal.toml").read_text(encoding="utf-8"))
            active = next(
                (profile for profile in profiles.values()
                 if isinstance(profile, dict) and profile.get("active")),
                None,
            )
            if active and active.get("token_id") and active.get("token_secret"):
                os.environ.setdefault("MODAL_TOKEN_ID", active["token_id"])
                os.environ.setdefault("MODAL_TOKEN_SECRET", active["token_secret"])
        except (OSError, tomllib.TOMLDecodeError):
            pass  # Modal reports its own authentication error during preflight.
os.environ["USERPROFILE"] = os.environ["HOME"] = str(_eval_home)

from deepagents_harbor.backend import HarborSandbox
from deepagents_harbor.deepagents_wrapper import DeepAgentsWrapper
from deepagents_harbor.novacode_wrapper import NovaCodeWrapper

__all__ = [
    "DeepAgentsWrapper",
    "HarborSandbox",
    "NovaCodeWrapper",
]
