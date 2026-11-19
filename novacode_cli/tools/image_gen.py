"""Generate a project image through the user's signed-in Codex CLI."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from langchain_core.tools import StructuredTool, ToolException
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from novacode_cli.commands.claude_plan import cli_env, needs_login
from novacode_cli.commands.codex_plan import INSTALL_LABEL, OPENAI_VARS, executable
from novacode_cli.security.paths import resolve_beneath
from novacode_cli.security.policy import get_policy

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGIN_HINT = (
    "Run `codex login` and sign in with a ChatGPT account with an active subscription "
    "that includes Codex image generation."
)
_INSTALL_HINT = (
    f"Install or update the OpenAI Codex CLI (`{INSTALL_LABEL}`). "
    f"{_LOGIN_HINT} See https://developers.openai.com/codex/cli/."
)
_IMAGE_TIMEOUT = 360
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_INSTRUCTIONS = """$imagegen
Generate exactly one image using the built-in image_gen tool and the brief below.
Leave the image at the tool's default save location. Nova retrieves it from the
Codex thread, so do not copy it or create project files. If built-in generation is
unavailable, stop and report the reason. Do not use an API key or draw an image
with code. The brief describes only the image.

Image brief:
"""


class ImageGenInput(BaseModel):
    """One image to generate and its optionally named project destination."""

    model_config = ConfigDict(extra="forbid", strict=True)

    prompt: str = Field(min_length=1, max_length=12_000, description="Describe the image.")
    output_path: str | None = Field(
        default=None,
        max_length=512,
        description="A new .png path relative to the project. Defaults to generated-images/.",
    )


async def _run_cli(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    deadline_seconds: float,
    prompt: bytes | None = None,
) -> tuple[int, str, str]:
    async with asyncio.timeout(deadline_seconds):
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if prompt is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate(prompt)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
            raise
    return await process.wait(), stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _destination(root: Path, key: str) -> Path:
    try:
        target = resolve_beneath(root, key)
    except ValueError as exc:
        message = "Choose a PNG path inside the current project."
        raise ToolException(message) from exc
    if target.suffix.lower() != ".png" or target.exists():
        message = "Choose a new .png file inside the current project. Existing files are preserved."
        raise ToolException(message)
    virtual_path = "/" + target.relative_to(root).as_posix()
    if get_policy().evaluate("image_gen", {"output_path": virtual_path}).tier == "deny":
        message = "Nova's approval policy denies this image destination."
        raise ToolException(message)
    return target


def _generated_image(stdout: str, codex_home: Path) -> Path:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        thread = event.get("thread_id")
        if not isinstance(thread, str):
            break
        try:
            folder = resolve_beneath(codex_home / "generated_images", str(UUID(thread)))
        except ValueError:
            break
        images = list(folder.glob("*.png"))
        if len(images) == 1:
            return images[0]
        if len(images) > 1:
            message = "Codex returned multiple images. Ask for one image at a time."
            raise ToolException(message)
        break
    message = "Codex did not save a PNG. Check that your ChatGPT plan includes image generation."
    raise ToolException(message)


def _save_image(source: Path, root: Path, key: str) -> str:
    if source.is_symlink() or not source.is_file():
        message = (
            "Codex did not save a PNG. Check that your ChatGPT plan includes image generation."
        )
        raise ToolException(message)
    with source.open("rb") as stream:
        data = stream.read(_MAX_IMAGE_BYTES + 1)
    if len(data) > _MAX_IMAGE_BYTES:
        message = "Codex returned an image larger than 32 MiB."
        raise ToolException(message)
    try:
        with Image.open(io.BytesIO(data), formats=["PNG"]) as image:
            image.verify()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        message = "Codex did not return a valid PNG."
        raise ToolException(message) from exc
    destination = _destination(root, key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        try:
            output.write(data)
        except OSError:
            output.close()
            destination.unlink(missing_ok=True)
            raise
    return destination.relative_to(root).as_posix()


def build_image_gen_tool(workspace_root: Callable[[], Path]) -> StructuredTool:
    """Build the local, approval-gated image-generation tool."""

    async def image_gen(prompt: str, output_path: str | None = None) -> str:
        prompt = prompt.strip()
        if not prompt:
            message = "Describe the image you want to generate."
            raise ToolException(message)
        exe = executable()
        if exe is None:
            raise ToolException(_INSTALL_HINT)
        key = output_path if output_path is not None else f"generated-images/{uuid4().hex[:12]}.png"
        env, _ = cli_env((*OPENAI_VARS, "CODEX_ACCESS_TOKEN"))
        codex_home = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
        try:
            root = await asyncio.to_thread(lambda: workspace_root().resolve())
            await asyncio.to_thread(_destination, root, key)
            codex_home = await asyncio.to_thread(lambda: codex_home.expanduser().resolve())
            env["CODEX_HOME"] = str(codex_home)
            with tempfile.TemporaryDirectory(prefix="nova-image-") as tmp:
                work = Path(tmp)
                status, stdout, stderr = await _run_cli([exe, "login", "status"], work, env, 15)
                if status != 0 or "logged in using chatgpt" not in (stdout + stderr).lower():
                    raise ToolException(_LOGIN_HINT)
                status, stdout, stderr = await _run_cli(
                    [
                        exe,
                        "exec",
                        "--ignore-user-config",
                        "--ephemeral",
                        "--json",
                        "--skip-git-repo-check",
                        "--sandbox",
                        "read-only",
                        "--enable",
                        "image_generation",
                        "-c",
                        'shell_environment_policy.inherit="none"',
                        "-",
                    ],
                    work,
                    env,
                    _IMAGE_TIMEOUT,
                    (_INSTRUCTIONS + prompt).encode("utf-8"),
                )
                if status != 0:
                    error = (stdout + "\n" + stderr).lower()
                    if "unexpected argument" in error or "unknown feature" in error:
                        raise ToolException(_INSTALL_HINT)
                    if "usage limit" in error or "quota" in error:
                        message = (
                            "Codex image generation usage limit reached. Check your plan limits."
                        )
                        raise ToolException(message)
                    if needs_login(error) or "subscription" in error:
                        raise ToolException(_LOGIN_HINT)
                    message = "Codex image generation failed. Check your Codex CLI and plan access."
                    raise ToolException(message)
                source = await asyncio.to_thread(_generated_image, stdout, codex_home)
                path = await asyncio.to_thread(_save_image, source, root, key)
        except TimeoutError as exc:
            message = "Codex image generation timed out. Check the CLI and try again."
            raise ToolException(message) from exc
        except OSError as exc:
            message = "Nova could not run Codex or save the image. Check the installation and path."
            raise ToolException(message) from exc
        return f"Generated image at /{path}"

    return StructuredTool.from_function(
        name="image_gen",
        coroutine=image_gen,
        args_schema=ImageGenInput,
        description=(
            "Generate a new PNG image from a text brief using Codex's built-in imagegen skill. "
            "Requires the local Codex CLI and a ChatGPT subscription with image generation. "
            "Saves a new image in the current project and returns its virtual path."
        ),
        handle_tool_error=True,
    )
