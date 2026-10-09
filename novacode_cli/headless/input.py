"""Bounded UTF-8 prompts and CLI limit validation for unattended runs."""

import argparse
import math

MAX_PROMPT_SIZE = 1024 * 1024


def require_workspace_approval(manager, cwd, *, explicit_trust=False):
    if manager.is_path_approved(cwd):
        return
    if explicit_trust:
        manager.approve_path(cwd, recursive=False)
        return
    raise PermissionError(
        "Workspace is not approved. Approve it in Nova or explicitly pass --trust-workspace."
    )


def positive_int(value):
    try:
        result = int(value)
        if result > 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def positive_seconds(value):
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a finite positive number of seconds")


def read_prompt(value, stdin):
    if isinstance(value, str):
        prompt = value.strip()
    elif stdin.isatty():
        raise ValueError("--print requires a prompt argument or piped stdin")
    else:
        source = getattr(stdin, "buffer", stdin)
        raw = source.read(MAX_PROMPT_SIZE + 1)
        if len(raw) > MAX_PROMPT_SIZE:
            raise ValueError("Headless prompt exceeds the 1 MiB input limit")
        try:
            prompt = (raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw).strip()
        except UnicodeDecodeError as exc:
            raise ValueError("Piped input must be UTF-8 text") from exc
    if not prompt:
        raise ValueError("--print requires a non-empty prompt")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_SIZE:
        raise ValueError("Headless prompt exceeds the 1 MiB input limit")
    return prompt
