"""WorkdirSandboxBackend rebases virtual `/` paths onto the sandbox workdir.

Guards the fix for the sandbox path mismatch: the agent uses `/`-rooted virtual
project paths, but a raw sandbox backend resolves `/foo` at the container root
(project lives at e.g. /workspace), so file reads 404. The wrapper rebases onto
the workdir while still registering as a sandbox backend.
"""

from __future__ import annotations

import base64
import re

from deepagents.backends.protocol import (
    ExecuteResponse,
    SandboxBackendProtocol,
)
from deepagents.backends.sandbox import BaseSandbox

from novacode_cli.integrations.workdir_backend import WorkdirSandboxBackend


class _FakeSandbox(BaseSandbox):
    """Minimal BaseSandbox: records the script passed to execute / delegated args."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.dl: list[list[str]] = []
        self.ul: list[list[tuple[str, bytes]]] = []

    @property
    def id(self) -> str:
        return "fake"

    def execute(self, command: str, *, timeout=None) -> ExecuteResponse:
        # This fake stands in for an ordinary sandbox, which has python3; answer
        # the wrapper's probe so the inherited script path is exercised (the
        # python3-free fallbacks are covered in test_workdir_backend_no_python3).
        # Not recorded, so `commands` stays a log of real file ops.
        if "command -v python3" in command:
            return ExecuteResponse(output="__NOVA_PY3__", exit_code=0, truncated=False)
        self.commands.append(command)
        return ExecuteResponse(output="", exit_code=0, truncated=False)

    async def aexecute(self, command: str, *, timeout=None) -> ExecuteResponse:
        return self.execute(command, timeout=timeout)

    def download_files(self, paths):
        self.dl.append(list(paths))
        return []

    async def adownload_files(self, paths):
        return self.download_files(paths)

    def upload_files(self, files):
        self.ul.append(list(files))
        return []

    async def aupload_files(self, files):
        return self.upload_files(files)


def _wrap(workdir="/workspace"):
    inner = _FakeSandbox()
    return WorkdirSandboxBackend(inner, workdir=workdir), inner


def test_rebase_logic():
    w, _ = _wrap("/workspace")
    rb = w._rebase
    assert rb("/novacode_cli/x.py") == "/workspace/novacode_cli/x.py"  # virtual abs
    assert rb("rel/y.py") == "/workspace/rel/y.py"                      # relative
    assert rb("/workspace/z.py") == "/workspace/z.py"                   # already rooted
    assert rb("/workspace") == "/workspace"                            # the workdir itself
    assert rb("/") == "/workspace"                                     # root → workdir
    # Idempotent: rebasing an already-rebased path is a no-op.
    assert rb(rb("/a/b")) == rb("/a/b")
    assert w._rebase_opt(None) == "/workspace"                          # grep/glob default


def test_rebase_other_workdir():
    w, _ = _wrap("/home/user")
    assert w._rebase("/pkg/mod.py") == "/home/user/pkg/mod.py"
    assert w._rebase("/home/user/keep") == "/home/user/keep"


def test_registers_as_sandbox_backend():
    # Must still satisfy isinstance so _supports_sandbox_execution() stays True.
    w, _ = _wrap()
    assert isinstance(w, SandboxBackendProtocol)
    assert w.id == "fake"


def test_execute_is_not_rebased():
    # Shell commands run in the workdir already — their paths are left alone.
    # The command still gets the non-interactive env prefix (see
    # _noninteractive), so check the command itself is unaltered rather than
    # demanding a byte-identical string.
    w, inner = _wrap()
    w.execute("ls -la /")
    assert inner.commands[-1].endswith("ls -la /")
    assert "/workspace" not in inner.commands[-1]


def test_real_path_mode_leaves_absolute_paths_alone():
    """An agent told its real cwd means `/tmp/x` when it says `/tmp/x`.

    Rebasing sent `write_file("/git/server/hooks/post-receive")` to
    `/app/git/server/hooks/post-receive` and reported success at the path asked
    for; the shell, which is never rebased, then could not find the file.
    """
    w = WorkdirSandboxBackend(_FakeSandbox(), workdir="/app", virtual_root=False)
    for real in ("/tmp/test_run.py", "/etc/nginx/conf.d/site.conf", "/git/server/hooks/post-receive"):
        assert w._rebase(real) == real
    assert w._rebase("/app/main.py") == "/app/main.py"
    assert w._rebase("/app/../etc/hosts") == "/etc/hosts"  # normalised, not re-rooted
    # Relative paths still resolve against the working directory.
    assert w._rebase("notes.txt") == "/app/notes.txt"
    assert w._rebase("src/a.py") == "/app/src/a.py"
    assert w._rebase_opt(None) == "/app"


def test_real_path_mode_reaches_the_script_unrebased():
    inner = _FakeSandbox()
    w = WorkdirSandboxBackend(inner, workdir="/app", virtual_root=False)
    w.read("/tmp/frames/frame_0400.png")
    payload = re.search(r"b64decode\('([^']+)'\)", inner.commands[-1]).group(1)
    assert base64.b64decode(payload).decode() == "/tmp/frames/frame_0400.png"


def test_virtual_root_is_still_the_default():
    w, _ = _wrap()
    assert w._rebase("/src/x") == "/workspace/src/x"
    # Plausible project folders are still treated as the project's.
    assert w._rebase("/bin/tool") == "/workspace/bin/tool"
    assert w._rebase("/etc/app.conf") == "/workspace/etc/app.conf"


def test_os_only_paths_are_never_rebased():
    """`write_file("/tmp/t.py")` then `python /tmp/t.py` must be the same file."""
    w, _ = _wrap()
    for real in ("/tmp/t.py", "/tmp/a/b.txt", "/dev/null", "/proc/cpuinfo", "/sys/kernel/x"):
        assert w._rebase(real) == real
    # Only the directory itself, not a project folder that happens to start the same.
    assert w._rebase("/tmpl/page.html") == "/workspace/tmpl/page.html"


def test_download_upload_rebase_paths():
    w, inner = _wrap()
    w.download_files(["/novacode_cli/a.py", "rel/b.py", "/workspace/c.py"])
    assert inner.dl[-1] == [
        "/workspace/novacode_cli/a.py",
        "/workspace/rel/b.py",
        "/workspace/c.py",
    ]
    w.upload_files([("/out/x.json", b"data")])
    assert inner.ul[-1] == [("/workspace/out/x.json", b"data")]


def test_ls_runs_script_with_rebased_path():
    # ls builds a script that base64-encodes the path and calls self.execute;
    # the rebased path must be what reaches the sandbox.
    w, inner = _wrap()
    w.ls("/novacode_cli/utils")
    cmd = inner.commands[-1]
    m = re.search(r"b64decode\('([^']+)'\)", cmd)
    assert m, cmd
    decoded = base64.b64decode(m.group(1)).decode("utf-8")
    assert decoded == "/workspace/novacode_cli/utils"
