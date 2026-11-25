"""Bounded UTF-8 prompts and CLI limit validation for unattended runs."""

import argparse
import base64
import math
from pathlib import Path

MAX_PROMPT_SIZE = 10 * 1024 * 1024

#: Claude-Code-style tool-name aliases so `--allowed-tools "Read,Edit,Bash"`
#: works for callers coming from other coding CLIs.
_TOOL_ALIASES = {
    "bash": "execute",
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "glob": "glob",
    "grep": "grep",
}
_IMAGE_FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def parse_allowed_tools(value: str | None) -> frozenset[str] | None:
    """Parse `--allowed-tools "Read,Edit,Bash(git diff *)"` into Nova tool names.

    Only the tool name is matched (argument-level patterns are a policy-file
    concern); `Bash(git diff *)`-style entries contribute their tool name.
    """
    if not value or not value.strip():
        return None
    tools = set()
    for part in value.split(","):
        name = part.strip()
        if not name:
            continue
        name = name.split("(", 1)[0].strip().lower()
        if not name:
            continue
        tools.add(_TOOL_ALIASES.get(name, name))
    return frozenset(tools) or None


def load_image_attachments(paths: list[str] | None) -> list[dict]:
    """Read `--image` files into validated attachment dicts for the agent."""
    attachments = []
    for index, path in enumerate(paths or []):
        source = Path(path)
        if not source.is_file():
            raise ValueError(f"--image {path}: file not found")
        image_format = _IMAGE_FORMATS.get(source.suffix.lower().lstrip("."))
        if image_format is None:
            raise ValueError(f"--image {path}: unsupported format (use png, jpeg, webp or gif)")
        raw = source.read_bytes()
        if len(raw) > _MAX_IMAGE_BYTES:
            raise ValueError(f"--image {path}: exceeds the 20MB size limit")
        attachments.append(
            {
                "base64_data": base64.b64encode(raw).decode("ascii"),
                "format": image_format,
                "placeholder": f"[image {index + 1}]",
            }
        )
    return attachments


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
            raise ValueError("Headless prompt exceeds the 10 MiB input limit")
        try:
            prompt = (raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw).strip()
        except UnicodeDecodeError as exc:
            raise ValueError("Piped input must be UTF-8 text") from exc
    if not prompt:
        raise ValueError("--print requires a non-empty prompt")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_SIZE:
        raise ValueError("Headless prompt exceeds the 10 MiB input limit")
    return prompt
