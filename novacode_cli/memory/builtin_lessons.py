"""Lessons every installation starts with.

Running the learning loop across a dozen unrelated tasks, the only things that
earned a place in the shared pool were environment facts that are true for
everyone: run ``apt-get update`` first, how to get Python into a bare container.
There is no reason for each user to pay to rediscover those, so they ship here.

They are recalled exactly like learned lessons — only when what is on screen
matches — and are never injected wholesale. Each is written symptom-first,
because the symptom is what a session is looking at when it needs the lesson.

Every entry below was hit by an agent on Terminal-Bench and resolved the way it
says. Keep it that way: add something only once it has been seen to work.

This is a Python module rather than a data file because the package ships only
``*.jinja`` as package data.
"""

from __future__ import annotations

import os

TOPIC = "builtin-environment"

LESSONS: tuple[str, ...] = (
    "- **Symptom:** `E: Unable to locate package <name>` from `apt-get install` in a fresh "
    "container. **Cause:** the image ships with no package lists. **Fix:** run `apt-get update` "
    "first, then install.",
    "- **Symptom:** `apt-get install` stops at a `Geographic area:` / `Configuring tzdata` menu, or "
    "hangs with no output. **Cause:** debconf is waiting for an answer and there is no terminal. "
    "**Fix:** `DEBIAN_FRONTEND=noninteractive apt-get install -y <packages>`.",
    "- **Symptom:** `bash: python3: command not found` or `python: command not found`. **Cause:** "
    "the image has no Python (or only `python3`, not `python`). **Fix:** `apt-get update && "
    "apt-get install -y python3 python3-pip`, and call it as `python3`.",
    "- **Symptom:** `error: externally-managed-environment` from `pip install`. **Cause:** PEP 668 — "
    "the system Python refuses global installs. **Fix:** `pip install --break-system-packages "
    "<pkg>` in a throwaway container, or create a venv with `python3 -m venv`.",
    "- **Symptom:** `E: Could not get lock /var/lib/dpkg/lock` or `lock-frontend`. **Cause:** "
    "another apt/dpkg process is still running. **Fix:** wait for it to finish (`while pgrep -x "
    "apt-get >/dev/null || pgrep -x dpkg >/dev/null; do sleep 2; done`); do not delete the lock.",
    "- **Symptom:** `Could not find a suitable TLS CA certificate bundle` or SSL certificate "
    "errors from pip/curl in a minimal image. **Cause:** no CA certificates installed. **Fix:** "
    "`apt-get install -y ca-certificates`.",
    "- **Symptom:** `curl: command not found` (or `timeout: failed to run command 'curl'`). "
    "**Cause:** minimal image. **Fix:** `apt-get install -y curl`, or use `wget` or Python's "
    "`urllib` if one of those is present.",
    "- **Symptom:** a command takes minutes and its output comes back truncated. **Cause:** it "
    "printed megabytes (a binary dump, a runaway loop, a huge log). **Fix:** bound the output — "
    "`| head -c 2000`, `| tail -n 40`, or compare hashes/sizes instead of printing content.",
)


def enabled() -> bool:
    """Off with ``NOVA_BUILTIN_LESSONS=0``."""
    return os.environ.get("NOVA_BUILTIN_LESSONS", "").strip().lower() not in {"0", "false", "off"}


def body() -> str:
    """The lessons as a topic-file body."""
    return "# Environment basics\n\n" + "\n".join(LESSONS) + "\n"
