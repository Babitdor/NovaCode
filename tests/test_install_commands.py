"""Install hints name a command that works for how this Nova was installed."""

from __future__ import annotations

import sys

import pytest

from novacode_cli import install_commands as ic

REPO = "B:/Summer Project 2026/Nova-Code/nova-code-cli/NovaCode"


def _receipt(tmp_path, requirement: str):
    (tmp_path / "uv-receipt.toml").write_text(
        f"[tool]\nrequirements = [{requirement}]\n", encoding="utf-8"
    )
    return tmp_path


def test_an_editable_uv_tool_install_is_reinstalled_with_the_extra_added(tmp_path):
    prefix = _receipt(
        tmp_path, f'{{ name = "novacode-cli", extras = ["voice", "browser"], editable = "{REPO}" }}'
    )
    command = ic.install_command(["pocket-tts"], "voice-pocket", prefix=prefix)
    assert command == f'uv tool install --reinstall -e "{REPO}[browser,voice,voice-pocket]"'
    assert "--with" not in command, "novacode-cli is not on PyPI"


def test_a_git_uv_tool_install_keeps_its_source(tmp_path):
    prefix = _receipt(
        tmp_path, '{ name = "novacode-cli", git = "https://github.com/Babitdor/NovaCode.git" }'
    )
    command = ic.install_command(["sherpa-onnx"], "voice-parakeet", prefix=prefix)
    assert command == (
        'uv tool install --reinstall '
        '"novacode-cli[voice-parakeet] @ git+https://github.com/Babitdor/NovaCode.git"'
    )


def test_an_extra_index_is_passed_on(tmp_path):
    prefix = _receipt(tmp_path, f'{{ name = "novacode-cli", editable = "{REPO}" }}')
    command = ic.install_command(
        ["orpheus-cpp"], "voice-orpheus", index_url="https://idx.example/cpu", prefix=prefix
    )
    assert command.endswith("[voice-orpheus]\" --extra-index-url https://idx.example/cpu")


def test_any_other_environment_installs_into_the_running_interpreter(monkeypatch):
    monkeypatch.setattr(ic.shutil, "which", lambda _name: "uv")
    command = ic.install_command(["pocket-tts"], "voice-pocket")
    assert command.startswith("uv pip install --python ")
    assert sys.executable in command and command.endswith(" pocket-tts")

    monkeypatch.setattr(ic.shutil, "which", lambda _name: None)
    assert ic.install_command(["pocket-tts"]).endswith("-m pip install pocket-tts")


def test_installer_flags_force_the_interpreter_form(tmp_path, monkeypatch):
    monkeypatch.setattr(ic.shutil, "which", lambda _name: "uv")
    prefix = _receipt(tmp_path, f'{{ name = "novacode-cli", editable = "{REPO}" }}')
    command = ic.install_command(
        ["llama-cpp-python"], "voice-orpheus", args="--force-reinstall --no-binary x", prefix=prefix
    )
    assert command.startswith("uv pip install --python ")
    assert command.endswith("llama-cpp-python --force-reinstall --no-binary x")


@pytest.mark.parametrize(
    "hint", ["install_hint", "pocket_install_hint", "orpheus_install_hint", "parakeet_install_hint"]
)
def test_voice_hints_name_one_working_command(hint):
    from novacode_cli import audio

    text = getattr(audio, hint)()
    assert "novacode-cli --reinstall" not in text and "--with " not in text
    assert text.endswith("then restart Nova.")
    assert audio.install_line(text).startswith(("uv ", '"', sys.executable))


def test_a_missing_core_package_names_the_interpreter_command(monkeypatch):
    monkeypatch.setattr(ic.shutil, "which", lambda _name: "uv")
    message = ic.missing_package("semble")
    assert message.startswith("semble is not installed. Install it with: uv pip install --python")
