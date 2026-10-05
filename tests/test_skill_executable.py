"""Executable-package skills: resolution + execution + /skill --run wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from novacode_cli.skills.executable import (
    find_executable,
    is_executable,
    run_executable,
)

if TYPE_CHECKING:
    import pytest


class TestFindExecutable:
    def test_detects_skill_py(self, tmp_path: Path):
        (tmp_path / "skill.py").write_text("print('hi')")
        exe = find_executable(tmp_path)
        assert exe is not None
        assert exe.kind == "script"
        assert exe.command[-1].endswith("skill.py")

    def test_detects_package(self, tmp_path: Path):
        pkg = tmp_path / "package"
        pkg.mkdir()
        (pkg / "__main__.py").write_text("print('pkg')")
        exe = find_executable(tmp_path)
        assert exe is not None
        assert exe.kind == "package"
        assert exe.command[-2:] == ["-m", "package"]

    def test_detects_pyproject_entrypoint(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text('[project]\nscripts = { mytool = "mymod:main" }\n')
        exe = find_executable(tmp_path)
        assert exe is not None
        assert exe.kind == "entrypoint"
        assert exe.command[-2:] == ["-m", "mymod"]

    def test_markdown_only_returns_none(self, tmp_path: Path):
        (tmp_path / "SKILL.md").write_text("# skill")
        assert find_executable(tmp_path) is None
        assert is_executable(tmp_path) is False

    def test_precedence_script_over_package(self, tmp_path: Path):
        (tmp_path / "skill.py").write_text("print('script')")
        pkg = tmp_path / "package"
        pkg.mkdir()
        (pkg / "__main__.py").write_text("print('pkg')")
        exe = find_executable(tmp_path)
        assert exe.kind == "script"


class TestRunExecutable:
    def test_runs_skill_py_and_captures_output(self, tmp_path: Path):
        (tmp_path / "skill.py").write_text("print('hello from skill')")
        result = run_executable(tmp_path)
        assert result["ok"] is True
        assert "hello from skill" in result["output"]

    def test_passes_args(self, tmp_path: Path):
        (tmp_path / "skill.py").write_text("import sys; print(sys.argv[1])")
        result = run_executable(tmp_path, ["world"])
        assert result["ok"] is True
        assert "world" in result["output"]

    def test_timeout_kills_sleeping_script(self, tmp_path: Path):
        (tmp_path / "skill.py").write_text("import time; time.sleep(999)")
        result = run_executable(tmp_path, timeout=1.0)
        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_nonzero_exit_reports_error(self, tmp_path: Path):
        (tmp_path / "skill.py").write_text("import sys; sys.exit(3)")
        result = run_executable(tmp_path)
        assert result["ok"] is False
        assert "exit code 3" in result["error"]

    def test_no_executable_returns_error(self, tmp_path: Path):
        (tmp_path / "SKILL.md").write_text("# skill")
        result = run_executable(tmp_path)
        assert result["ok"] is False
        assert "no executable" in result["error"]


class TestSkillInvokeRunFlag:
    """The /skill:<name> --run path resolves and runs the executable."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, skill_dir: Path) -> None:
        import novacode_cli.commands.skill_invoke as si

        class _FakeSettings:
            project_root = None

            @staticmethod
            def from_environment() -> _FakeSettings:
                return _FakeSettings()

            def ensure_user_skills_dir(self, _assistant_id: str | None = None) -> Path:
                return Path()

            @staticmethod
            def get_shared_skills_dir() -> Path:
                return Path("no-such-shared-skills-dir-xyz")

            def get_project_skills_dirs(self) -> list[Path]:
                return []

            def get_project_skills_dir(self) -> None:
                return None

            @staticmethod
            def get_global_claude_skills_dir() -> Path:
                return Path("no-such-claude-skills-dir-xyz")

        monkeypatch.setattr(si, "Settings", _FakeSettings)
        monkeypatch.setattr(
            si,
            "list_skills",
            lambda **_k: [
                {
                    "name": "runnable",
                    "description": "a runnable skill",
                    "source": "global",
                    "path": str(skill_dir),
                }
            ],
        )
        monkeypatch.setattr(si, "_get_supporting_files", lambda _d: {})
        monkeypatch.setattr(si, "_read_skill_content", lambda *_a, **_k: "# Runnable\nbody")

    def test_run_flag_executes_skill(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        (tmp_path / "skill.py").write_text("print('executed directly')")
        self._patch(monkeypatch, tmp_path)

        from novacode_cli.commands.skill_invoke import _try_skill_invocation

        res = asyncio.run(_try_skill_invocation("runnable", "--run", object(), "nova"))
        assert res is not None
        assert "executed directly" in res.prompt
        assert res.executable is not None

    def test_run_flag_with_args(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        (tmp_path / "skill.py").write_text("import sys; print('arg=' + sys.argv[1])")
        self._patch(monkeypatch, tmp_path)

        from novacode_cli.commands.skill_invoke import _try_skill_invocation

        res = asyncio.run(_try_skill_invocation("runnable", "--run hello", object(), "nova"))
        assert res is not None
        assert "arg=hello" in res.prompt

    def test_without_run_flag_returns_markdown_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        (tmp_path / "skill.py").write_text("print('executed directly')")
        self._patch(monkeypatch, tmp_path)

        from novacode_cli.commands.skill_invoke import _try_skill_invocation

        res = asyncio.run(_try_skill_invocation("runnable", None, object(), "nova"))
        assert res is not None
        assert "executed directly" not in res.prompt  # markdown path, not run
        assert "# Runnable" in res.prompt
