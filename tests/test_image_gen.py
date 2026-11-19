"""Check Nova's Codex image tool without using a provider account."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from novacode_cli.agents.code_mode import code_mode_surface
from novacode_cli.tools import image_gen

if TYPE_CHECKING:
    from pathlib import Path


_THREAD = "4d15b73b-b6d7-4497-b61e-46cfdfb04a31"
_EVENT = json.dumps({"type": "thread.started", "thread_id": _THREAD})
_IMAGE = f"generated_images/{_THREAD}/exec-image.png"


async def _generate(root: Path, **args: str):
    tool = image_gen.build_image_gen_tool(lambda: root)
    return await tool.ainvoke(
        {"type": "tool_call", "id": "picture", "name": "image_gen", "args": args}
    )


@pytest.mark.asyncio
async def test_missing_cli_gives_install_and_subscription_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(image_gen, "executable", lambda: None)
    result = await _generate(tmp_path, prompt="A mountain")
    assert result.status == "error"
    assert "npm install -g @openai/codex" in result.content
    assert "codex login" in result.content
    assert "active subscription" in result.content
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [(1, "", "Not logged in"), (0, "", "Logged in using an API key")]
)
async def test_requires_chatgpt_login(
    status: tuple[int, str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run(argv, _cwd, _env, _deadline, prompt=None):
        assert argv == ["codex", "login", "status"]
        assert prompt is None
        return status

    monkeypatch.setattr(image_gen, "executable", lambda: "codex")
    monkeypatch.setattr(image_gen, "_run_cli", run)
    result = await _generate(tmp_path, prompt="A mountain")
    assert result.status == "error"
    assert "codex login" in result.content
    assert "active subscription" in result.content


@pytest.mark.asyncio
async def test_image_is_verified_and_saved_inside_the_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(image_gen, "executable", lambda: "codex")

    stale = codex_home / "generated_images/older-thread/wrong.png"
    stale.parent.mkdir(parents=True)
    Image.new("RGB", (5, 5)).save(stale)

    async def run(argv, cwd, env, _deadline, prompt=None):
        calls.append(argv)
        assert "OPENAI_API_KEY" not in env
        assert "OPENAI_BASE_URL" not in env
        assert "CODEX_ACCESS_TOKEN" not in env
        assert cwd != tmp_path
        if argv[1:] == ["login", "status"]:
            return 0, "", "Logged in using ChatGPT"
        assert argv[1] == "exec"
        assert argv[-1] == "-"
        assert "read-only" in argv
        assert "--json" in argv
        assert "image_generation" in argv
        assert prompt.startswith(b"$imagegen\n")
        assert prompt.endswith(b"A mountain")
        image = codex_home / _IMAGE
        image.parent.mkdir(parents=True)
        Image.new("RGB", (2, 3)).save(image)
        return 0, _EVENT, ""

    monkeypatch.setattr(image_gen, "_run_cli", run)
    result = await _generate(tmp_path, prompt="A mountain", output_path="/assets/peak.png")
    assert result.status == "success"
    assert result.content == "Generated image at /assets/peak.png"
    with Image.open(tmp_path / "assets" / "peak.png") as image:
        assert image.size == (2, 3)
        assert image.format == "PNG"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../escape.png", "C:/escape.png", "existing.png", "asset.svg"])
async def test_unsafe_or_existing_destination_is_rejected_before_running_codex(
    path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"original")
    monkeypatch.setattr(image_gen, "executable", lambda: "codex")

    async def unexpected(*_args):
        pytest.fail("Codex should not start for an invalid destination")

    monkeypatch.setattr(image_gen, "_run_cli", unexpected)
    result = await _generate(tmp_path, prompt="A mountain", output_path=path)
    assert result.status == "error"
    assert existing.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_policy_denied_default_destination_never_starts_codex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def evaluate(name, args):
        assert name == "image_gen"
        assert args["output_path"].startswith("/generated-images/")
        return SimpleNamespace(tier="deny")

    async def unexpected(*_args):
        pytest.fail("Codex should not start for a denied destination")

    monkeypatch.setattr(image_gen, "get_policy", lambda: SimpleNamespace(evaluate=evaluate))
    monkeypatch.setattr(image_gen, "executable", lambda: "codex")
    monkeypatch.setattr(image_gen, "_run_cli", unexpected)
    result = await _generate(tmp_path, prompt="A mountain")
    assert result.status == "error"
    assert "approval policy denies" in result.content
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unsupported", "no-plan", "missing", "invalid", "timeout"])
async def test_failed_runs_leave_no_project_image(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(image_gen, "executable", lambda: "codex")
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    async def run(argv, _cwd, _env, _deadline, _prompt=None):
        if argv[1] == "login":
            return 0, "Logged in using ChatGPT", ""
        if case == "unsupported":
            return 2, "", "unexpected argument --ephemeral"
        if case == "no-plan":
            return 1, "", "Subscription required"
        if case == "invalid":
            image = codex_home / _IMAGE
            image.parent.mkdir(parents=True)
            image.write_bytes(b"not an image")
        if case == "timeout":
            raise TimeoutError
        return 0, _EVENT, "No image"

    monkeypatch.setattr(image_gen, "_run_cli", run)
    result = await _generate(tmp_path, prompt="A mountain")
    assert result.status == "error"
    assert {
        "unsupported": "Install or update",
        "no-plan": "active subscription",
        "missing": "did not save a PNG",
        "invalid": "valid PNG",
        "timeout": "timed out",
    }[case] in result.content
    assert not (tmp_path / "generated-images").exists()


def test_image_tool_remains_direct_in_code_mode(tmp_path: Path) -> None:
    tool = image_gen.build_image_gen_tool(lambda: tmp_path)
    assert code_mode_surface([tool]) == [tool]
