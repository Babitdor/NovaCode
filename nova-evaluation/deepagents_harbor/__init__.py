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
from pathlib import Path

_real_home = Path.home()
_eval_home = Path(
    os.environ.get("NOVA_EVAL_HOME") or Path(__file__).resolve().parent.parent / ".eval-home"
)
_eval_home.mkdir(parents=True, exist_ok=True)
# The docker/modal subprocesses harbor spawns still need the real credentials.
os.environ.setdefault("DOCKER_CONFIG", str(_real_home / ".docker"))
if (_real_home / ".modal.toml").exists():
    os.environ.setdefault("MODAL_CONFIG_PATH", str(_real_home / ".modal.toml"))
os.environ["USERPROFILE"] = os.environ["HOME"] = str(_eval_home)

from deepagents_harbor.backend import HarborSandbox
from deepagents_harbor.deepagents_wrapper import DeepAgentsWrapper
from deepagents_harbor.novacode_wrapper import NovaCodeWrapper

__all__ = [
    "DeepAgentsWrapper",
    "HarborSandbox",
    "NovaCodeWrapper",
]
