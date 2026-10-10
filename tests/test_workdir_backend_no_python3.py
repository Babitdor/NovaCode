"""File tools must work in a sandbox image that has no python3.

``BaseSandbox`` implements ls/read/write/edit/glob as python3 scripts run inside
the sandbox. On an image without python3, read/write/edit failed with a raw
"python3: command not found", and ls/glob were *worse*: the error text failed
their JSON parse and they returned a successful EMPTY listing, telling the agent
a populated directory was empty.

The fake sandbox below fakes only python3's absence; every other command runs in
a real shell against a real directory tree, so the fallbacks' generated commands
are exercised, not just their parsers.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from deepagents.backends.protocol import ExecuteResponse
from novacode_cli.integrations.workdir_backend import WorkdirSandboxBackend

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    BASH is None, reason="needs a POSIX shell to run the fallback commands"
)


def _bash(script: str) -> tuple[int, str, str]:
    """Run a script in bash, fed on stdin as bytes. Returns (rc, stdout, stderr).

    Two Windows-only traps, both of which silently corrupt the script rather
    than failing: ``bash -c <script>`` lets Git Bash's MSYS layer rewrite
    POSIX-looking argv entries (mangling an embedded glob into a no-op), and
    ``text=True`` encodes stdin through newline translation, so every ``\\n``
    becomes ``\\r\\n`` and each line ends up with a stray CR — which lands in
    the *filenames* a setup script creates. Bytes on stdin avoid both.
    """
    proc = subprocess.run(  # noqa: S603 - test-only, fixed interpreter
        [BASH, "-s"], input=script.encode("utf-8"), capture_output=True
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


class _ShellSandbox:
    """Runs commands in a real bash; pretends python3 is not installed."""

    def __init__(self, *, python3: bool) -> None:
        self._python3 = python3

    @property
    def id(self) -> str:
        return "shell"

    def execute(self, command: str, **_kw: object) -> ExecuteResponse:
        if not self._python3:
            if "command -v python3" in command:
                return ExecuteResponse(output="", exit_code=1)
            # Matched anywhere, not just at the start: sandbox commands carry a
            # non-interactive env prefix (see _noninteractive).
            if "python3 -c" in command:
                return ExecuteResponse(
                    output="bash: line 1: python3: command not found", exit_code=127
                )
        rc, stdout, stderr = _bash(command)
        out, err = stdout.strip(), stderr.strip()
        # Mirror HarborSandbox: stderr is appended to the output channel.
        if err:
            out = f"{out}\n stderr: {err}" if out else err
        return ExecuteResponse(output=out, exit_code=rc)

    async def aexecute(self, command: str, **kw: object) -> ExecuteResponse:
        return self.execute(command, **kw)


@pytest.fixture
def workdir():  # noqa: ANN201
    """A real tree at a POSIX path, built and removed through the shell."""
    base = f"/tmp/novacode-wb-{uuid.uuid4().hex[:8]}"
    setup = f"""
    set -e
    mkdir -p {base}/app/sub
    printf 'one\\ntwo\\nthree\\nfour\\n' > {base}/app/main.tex
    : > {base}/app/empty.txt
    printf 'deep\\n' > {base}/app/sub/deep.tex
    printf '\\000\\001\\002binary\\000' > {base}/app/logo.bin
    """
    rc, _, err = _bash(setup)
    assert rc == 0, f"could not build the test tree: {err}"
    yield f"{base}/app"
    _bash(f"rm -rf {base}")


def _backend(workdir: str, *, python3: bool) -> WorkdirSandboxBackend:
    return WorkdirSandboxBackend(_ShellSandbox(python3=python3), workdir=workdir)


# ── the probe ──────────────────────────────────────────────────────────────


def test_python3_is_detected_when_present(workdir) -> None:
    assert _backend(workdir, python3=True)._python3() is True


def test_python3_absence_is_detected(workdir) -> None:
    assert _backend(workdir, python3=False)._python3() is False


def test_the_probe_runs_once_per_sandbox(workdir) -> None:
    backend = _backend(workdir, python3=False)
    calls: list[str] = []
    inner = backend._inner.execute

    def _counting(command: str, **kw: object) -> ExecuteResponse:
        if "command -v python3" in command:
            calls.append(command)
        return inner(command, **kw)

    backend._inner.execute = _counting  # type: ignore[method-assign]
    backend.ls("/")
    backend.read("/main.tex")
    backend.glob("*.tex")
    assert len(calls) == 1, f"probed {len(calls)} times; it must be cached"


# ── ls: the silent-empty bug ───────────────────────────────────────────────


def test_ls_lists_the_directory_without_python3(workdir) -> None:
    out = _backend(workdir, python3=False).ls("/")
    assert out.error is None
    names = sorted(e["path"].rsplit("/", 1)[-1] for e in out.entries)
    assert names == ["empty.txt", "logo.bin", "main.tex", "sub"], names
    assert [e["is_dir"] for e in out.entries if e["path"].endswith("/sub")] == [True]


def test_ls_reports_a_missing_directory_rather_than_empty(workdir) -> None:
    out = _backend(workdir, python3=False).ls("/nope")
    assert out.error and "not_a_directory" in out.error
    assert out.entries is None, "an unreadable path must not look like an empty directory"


# ── read ───────────────────────────────────────────────────────────────────


def test_read_returns_content_and_pagination_without_python3(workdir) -> None:
    out = _backend(workdir, python3=False).read("/main.tex")
    assert out.error is None
    assert out.file_data["content"] == "one\ntwo\nthree\nfour"
    assert (out.start_line, out.end_line, out.total_lines) == (1, 4, 4)
    assert out.next_offset == out.end_line


def test_read_paginates_with_offset_and_limit(workdir) -> None:
    out = _backend(workdir, python3=False).read("/main.tex", offset=1, limit=2)
    assert out.file_data["content"] == "two\nthree"
    assert (out.start_line, out.end_line, out.total_lines) == (2, 3, 4)
    assert out.next_offset == 3


def test_read_past_the_end_is_empty_not_an_error(workdir) -> None:
    out = _backend(workdir, python3=False).read("/main.tex", offset=99)
    assert out.error is None and out.file_data["content"] == ""


def test_read_handles_empty_missing_and_binary_files(workdir) -> None:
    backend = _backend(workdir, python3=False)
    assert backend.read("/empty.txt").file_data["content"] == ""
    assert "not found" in (backend.read("/ghost.txt").error or "")
    assert "binary" in (backend.read("/logo.bin").error or "")


def test_read_of_zero_lines_is_flagged_not_inspected(workdir) -> None:
    out = _backend(workdir, python3=False).read("/main.tex", limit=0)
    assert out.no_lines_requested is True and out.error is None


# ── glob ───────────────────────────────────────────────────────────────────


def test_glob_finds_files_recursively_without_python3(workdir) -> None:
    out = _backend(workdir, python3=False).glob("**/*.tex")
    found = sorted(m["path"].rsplit("/", 1)[-1] for m in out.matches)
    assert found == ["deep.tex", "main.tex"], found
    assert out.truncated is False


def test_glob_with_no_match_is_an_empty_success(workdir) -> None:
    out = _backend(workdir, python3=False).glob("*.nope")
    assert out.error is None and out.matches == []


# ── write / edit keep the python path but explain the failure ──────────────


def test_edit_failure_tells_the_agent_to_use_the_shell(workdir) -> None:
    out = _backend(workdir, python3=False).edit("/main.tex", "one", "1")
    assert out.error and "heredoc" in out.error and "sed" in out.error
    # "Not installed yet" plus how to install it — never a flat "this sandbox
    # has no python3", which an agent read as "Python is unavailable here" and
    # so shipped untested code instead of installing an interpreter.
    assert "not installed in this sandbox yet" in out.error
    assert "apt-get install -y python3" in out.error
    assert "has no python3" not in out.error


# ── non-interactive env: an installer prompt is an unanswerable hang ───────


def test_commands_run_with_apt_prompts_suppressed(workdir) -> None:
    out = _backend(workdir, python3=False).execute("echo \"[$DEBIAN_FRONTEND][$TZ]\"")
    assert out.output.strip() == "[noninteractive][Etc/UTC]", out.output


def test_an_existing_tz_is_respected(workdir) -> None:
    out = _backend(workdir, python3=False).execute('TZ=Europe/Oslo; echo "[$DEBIAN_FRONTEND]"')
    assert "[noninteractive]" in out.output


def test_the_prefix_is_not_applied_twice(workdir) -> None:
    from novacode_cli.integrations.workdir_backend import _noninteractive

    once = _noninteractive("echo hi")
    assert _noninteractive(once) == once
    assert _noninteractive("") == ""


# ── the python3 path is untouched where python3 exists ─────────────────────


def test_with_python3_the_script_path_is_used(workdir) -> None:
    out = _backend(workdir, python3=True).read("/main.tex")
    assert out.error is None, out.error
    assert "one" in out.file_data["content"]
