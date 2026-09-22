"""Shell module: ShellMiddleware class.

Provides ShellMiddleware, the LangGraph agent middleware that integrates
shell command execution with intelligent prompt detection, server management,
background process tracking, and sandbox execution support.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from langchain_core.messages import ToolMessage
from langchain_core.tools.base import ToolException

from novacode_cli.shell.os_sandbox import OSSandboxPolicy, detect_backend, wrap_command
from novacode_cli.shell.patterns import _NPX_YES_RE
from novacode_cli.shell.utils import (
    _convert_unix_command_to_windows,
    _sanitize_env,
    get_auto_answer,
    is_dangerous_command,
    is_interactive_command,
    is_interactive_prompt,
    is_long_running_command,
    is_server_ready,
)


# The shell's OWN "I don't understand this" diagnostics — the small, stable
# error vocabulary of cmd.exe / PowerShell / bash, NOT an enumeration of
# commands. When the chosen shell emits one of these, the command's syntax was
# rejected (so nothing meaningful ran) and it's safe to retry it in the other
# shell. This lets a bash command sent to the PowerShell `shell`/`execute` tool
# (the #1 Windows failure) succeed by re-running in bash, without predicting or
# hard-coding what "looks like" bash. Empirical, not heuristic.
_SHELL_REJECTION = re.compile(
    r"is not recognized"                                          # cmd.exe / pwsh: unknown command
    r"|command not found"                                         # bash: `foo: command not found`
    r"|CommandNotFoundException"                                  # pwsh
    r"|ParameterBindingException"                                 # pwsh: bash-style flags (`-rf`)
    r"|A parameter cannot be found that matches parameter name"   # pwsh
    r"|positional parameter cannot be found"                      # pwsh
    r"|Unexpected token|ParserError"                              # pwsh parse
    r"|syntax error near unexpected token|syntax error:",         # bash parse
    re.IGNORECASE,
)


def _result_text(result: "ToolMessage | str") -> str:
    if isinstance(result, str):
        return result
    content = getattr(result, "content", "")
    return content if isinstance(content, str) else str(content)


def _looks_like_shell_rejection(result: "ToolMessage | str") -> bool:
    """True when the shell rejected the command's SYNTAX (nothing meaningful ran,
    so retrying in the other shell is safe) — not when a command ran and returned
    a legitimate error. Keyed on the shell's own recognition/parse diagnostics."""
    # A ToolMessage that isn't an error can't be a rejection; a str carries no
    # status, so fall through to the pattern check.
    if not isinstance(result, str) and getattr(result, "status", "error") not in ("error", None):
        return False
    text = _result_text(result)
    return bool(text) and bool(_SHELL_REJECTION.search(text))


class ShellMiddleware(AgentMiddleware[AgentState, Any]):
    """Give basic shell access to agents via the shell.

    This shell will execute on the local machine and has NO safeguards except
    for the human in the loop safeguard provided by the CLI itself.

    When a sandbox backend is provided, commands will be executed in the sandbox
    instead of locally, enabling remote execution in isolated environments.
    """

    def __init__(
        self,
        *,
        workspace_root: str,
        timeout: float = 120.0,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        backend: Any = None,
        sandbox_working_dir: str | None = None,
        exec_sandbox: bool = False,
        allow_network: bool = True,
    ) -> None:
        """Initialize an instance of `ShellMiddleware`.

        Args:
            workspace_root: Working directory for shell commands.
            timeout: Maximum time in seconds to wait for command completion.
                Defaults to 120 seconds.
            max_output_bytes: Maximum number of bytes to capture from command output.
                Defaults to 100,000 bytes.
            env: Environment variables to pass to the subprocess. If None,
                uses the current process's environment. Defaults to None.
            backend: Optional sandbox backend for remote execution. When provided,
                commands will be executed in the sandbox instead of locally.
                Must implement SandboxBackendProtocol with an execute() method.
            sandbox_working_dir: Working directory inside the sandbox (e.g.
                "/workspace"), used only for the tool description shown to the
                model when running in a sandbox. Falls back to workspace_root.
            exec_sandbox: When True (Pattern A), wrap locally-executed commands in
                an OS kernel sandbox (bwrap/sandbox-exec) that confines filesystem
                writes to ``workspace_root``. Ignored for sandbox-backend
                execution. Degrades to no-op when no backend is available.
            allow_network: Whether the OS sandbox permits network egress. Defaults
                to True so dependency installs keep working.
        """
        super().__init__()
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._tool_name = "shell"
        self._env = _sanitize_env(env if env is not None else os.environ.copy())
        self._workspace_root = workspace_root
        self._backend = backend

        # OS-level shell confinement (Pattern A). Sets self._os_confined and
        # self._os_policy; probes the backend once, at build time.
        self._init_os_sandbox(exec_sandbox, allow_network)

        # Let the background-task registry restart jobs through this middleware
        # (re-run a job's command as a fresh background task). Last instance wins;
        # any working middleware can launch.
        try:
            from novacode_cli.shell import jobs as _jobs

            _jobs.get_registry().set_launcher(self._launch_background)
        except Exception:  # noqa: BLE001
            pass

        # Whether commands actually execute in a sandbox. A CompositeBackend is
        # always passed (for /skills/ etc. routing), so "backend is not None" is
        # not a reliable signal — check whether the *default* backend supports
        # remote execution.
        _is_sandbox = self._supports_sandbox_execution()
        # Directory shown to the model in the tool description.
        _display_dir = (
            (sandbox_working_dir or self._workspace_root) if _is_sandbox else self._workspace_root
        )

        # Determine shell and platform for this environment
        if _is_sandbox:
            # Sandboxes are always Linux/bash
            _shell_name = "bash"
            _platform_note = (
                "You are operating in an **isolated Linux sandbox** — always use bash syntax."
            )
        elif sys.platform == "win32":
            _shell_name = "PowerShell"
            _platform_note = (
                "The host is **Windows** — always use PowerShell syntax. "
                "NEVER use bash commands (rm -rf, chmod, sudo, export, etc.). "
                "Use `;` to chain commands, `$env:VAR` for env vars, "
                "`Remove-Item -Recurse -Force` instead of `rm -rf`."
            )
        elif sys.platform == "darwin":
            _shell_name = "zsh"
            _platform_note = "The host is **macOS** — use zsh/bash syntax."
        else:
            _shell_name = "bash"
            _platform_note = "The host is **Linux** — use bash syntax."

        # Determine execution context for description
        if _is_sandbox:
            execution_context = (
                f"You are operating in an **isolated sandbox environment** at {_display_dir}. "
                f"All commands execute inside the sandbox, not on the local machine. "
            )
        elif self._os_confined:
            _net = "Network is available" if self._os_policy.allow_network else "Network is blocked"
            execution_context = (
                f"Commands run in **{_shell_name}** in the working directory: {_display_dir}. "
                f"The shell is **confined by an OS sandbox**: filesystem writes are "
                f"restricted to the workspace ({_display_dir}) — writes elsewhere "
                f"(e.g. /etc, $HOME) will fail. {_net}. "
            )
        else:
            execution_context = (
                f"Commands run in **{_shell_name}** in the working directory: {_display_dir}. "
            )

        # Build description with working directory and platform information
        description = (
            f"Execute a {_shell_name} command. {_platform_note} {execution_context}"
            f"Each command runs in a fresh shell environment. Commands may "
            f"be truncated if they exceed the configured timeout or output limits. "
            f"Use interactive=True for commands that may prompt for user input "
            f"(e.g., npx create-next-app, npm init, git rebase -i). "
            f"Use background=True for long-running commands like dev servers "
            f"(e.g., npm run dev, vite, flask run) - returns when server is ready. "
            f"For scaffolding commands (create-next-app, ng new, npm init, etc.) "
            f"always pass non-interactive flags (--yes, --defaults, --no-interaction, "
            f"or explicit option flags) so arrow-key TUI menus never appear — "
            f"piped stdin cannot support TUI navigation."
        )

        # ── Resolve the program each tool ACTUALLY executes locally ──────
        # `shell` → the host's native interactive shell: **PowerShell on Windows**
        # (matching the description above), instead of cmd.exe — which is what
        # asyncio's create_subprocess_shell would otherwise use, silently
        # contradicting "use PowerShell syntax". `bash` → the **real bash** binary
        # (Git Bash / WSL on Windows). ``None`` ⇒ fall back to
        # create_subprocess_shell (POSIX sh on Unix, cmd.exe on Windows).
        import shutil

        self._native_prog: list[str] | None = None
        if sys.platform == "win32":
            _pwsh = shutil.which("pwsh") or shutil.which("powershell")
            if _pwsh:
                self._native_prog = [_pwsh, "-NoProfile", "-Command"]
        _bash_exe = shutil.which("bash")
        self._bash_prog: list[str] | None = [_bash_exe, "-c"] if _bash_exe else None
        # Auto-routing is only meaningful on Windows, where the native `shell`
        # (PowerShell) and `bash` are genuinely different dialects. Elsewhere the
        # native shell already IS a POSIX shell, so there's nothing to reroute.
        self._native_is_pwsh: bool = sys.platform == "win32" and self._native_prog is not None

        bash_description = (
            "Execute a command in the **bash** shell — use POSIX/bash syntax "
            "(`rm -rf`, `chmod`, `export`, `&&`, `$VAR`, `ls`, `cat`, …). Use this "
            "when you specifically want bash; for the host's native shell "
            f"(**{_shell_name}**) use `shell`. Same interactive=/background= options "
            "as `shell`."
        )

        def _make_impl(prog: list[str] | None, *, is_bash: bool = False) -> Callable[..., ToolMessage | str]:
            """Build a tool body bound to the program it should execute."""

            def _impl(
                command: str,
                runtime: ToolRuntime[None, AgentState],
                interactive: bool = False,  # noqa: FBT001, FBT002
                background: bool = False,  # noqa: FBT001, FBT002
            ) -> ToolMessage | str:
                """Execute a command (see the tool description for which shell + syntax)."""
                # `bash` with no bash on the host (and no sandbox to run it in) —
                # fail clearly instead of silently running it in another shell.
                if is_bash and prog is None and not self._supports_sandbox_execution():
                    return ToolMessage(
                        content=(
                            "bash is not available on this host. Install Git Bash or "
                            "WSL, or use the `shell` tool (PowerShell on Windows)."
                        ),
                        tool_call_id=runtime.tool_call_id,
                        name="bash",
                        status="error",
                    )
                return self._dispatch_local(
                    command,
                    tool_call_id=runtime.tool_call_id,
                    prog=prog,
                    interactive=interactive,
                    background=background,
                )

            async def _aimpl(
                command: str,
                runtime: ToolRuntime[None, AgentState],
                interactive: bool = False,  # noqa: FBT001, FBT002
                background: bool = False,  # noqa: FBT001, FBT002
            ) -> ToolMessage | str:
                """Execute a command (see the tool description for which shell + syntax)."""
                if is_bash and prog is None and not self._supports_sandbox_execution():
                    return _impl(command, runtime, interactive, background)
                return await self._adispatch_local(
                    command,
                    tool_call_id=runtime.tool_call_id,
                    prog=prog,
                    interactive=interactive,
                    background=background,
                )

            return _impl, _aimpl

        def _make_tool(name: str, desc: str, prog: list[str] | None, *, is_bash: bool = False):
            # Both bodies: the async agent awaits ``_aimpl``, which holds no
            # thread while the command runs. A sync-only tool made LangChain
            # park one pooled thread per running command (16 here, shared with
            # every other sync tool), so parallel shells could stall them all.
            sync_impl, async_impl = _make_impl(prog, is_bash=is_bash)
            return StructuredTool.from_function(
                func=sync_impl, coroutine=async_impl, name=name, description=desc
            )

        self._shell_tool = _make_tool(self._tool_name, description, self._native_prog)
        self.tools = [self._shell_tool]
        # `bash` is a real, separate tool (not a same-behavior alias) so Claude's
        # reflexive bash calls run actual bash. We intentionally do NOT register
        # `execute` — deepagents already provides one and a duplicate breaks build.
        if self._tool_name != "bash":
            self._bash_alias = _make_tool("bash", bash_description, self._bash_prog, is_bash=True)
            self.tools.append(self._bash_alias)

    def _alternate_prog(self, prog: "list[str] | None") -> "list[str] | None":
        """The interpreter to retry with when *prog* rejected the syntax.

        PowerShell ↔ bash; the ``execute`` default (``prog is None`` → cmd.exe)
        most often carries bash intent, so try bash first, then PowerShell.
        """
        if prog is self._bash_prog:
            return self._native_prog  # bash rejected → PowerShell
        if prog is self._native_prog:
            return self._bash_prog  # PowerShell rejected → bash
        return self._bash_prog or self._native_prog  # cmd.exe default → bash, else pwsh

    def _run_foreground_with_fallback(
        self, command: str, *, tool_call_id: str | None, prog: "list[str] | None"
    ) -> "ToolMessage | str":
        """Run a foreground command; if the shell rejected its SYNTAX, retry once
        in the other shell (Windows only). Empirical: the shell's own error is the
        signal, so this needs no command list and adapts to anything."""
        result = self._run_shell_command(command, tool_call_id=tool_call_id, prog=prog)
        if not self._native_is_pwsh or not _looks_like_shell_rejection(result):
            return result
        alt = self._alternate_prog(prog)
        if alt is None or alt is prog:
            return result
        retry = self._run_shell_command(command, tool_call_id=tool_call_id, prog=alt)
        # If the other shell also rejects it, the first error was the more honest
        # one (or neither shell has the tool) — return the original.
        return retry if not _looks_like_shell_rejection(retry) else result

    def _init_os_sandbox(self, exec_sandbox: bool, allow_network: bool) -> None:  # noqa: FBT001
        """Probe and configure the OS-level shell sandbox (Pattern A).

        Done once at build time so the per-command path stays cheap and never
        ``console.print``s (which would corrupt the TUI). If confinement was
        requested but no backend is available, it is disabled with a one-line
        warning — execution still runs, guarded by the dangerous-command
        blocklist + HITL.
        """
        self._os_confined = False
        self._os_policy = OSSandboxPolicy(
            workspace_root=self._workspace_root,
            allow_network=allow_network,
            enabled=False,
        )
        if not exec_sandbox:
            return

        from novacode_cli.config.config import boot_status

        detected = detect_backend()
        if detected is None:
            boot_status(
                "sandbox: no OS sandbox backend available — shell runs "
                "unconfined (blocklist + approval still apply)",
                "warn",
            )
            return

        self._os_policy = OSSandboxPolicy(
            workspace_root=self._workspace_root,
            allow_network=allow_network,
            enabled=True,
            backend=detected,
        )
        self._os_confined = True
        net = "network on" if allow_network else "network blocked"
        boot_status(f"sandbox: shell confined to workspace via {detected} ({net})", "ok")

    @staticmethod
    def _preprocess_command(command: str) -> str:
        """Inject helpers into commands before execution.

        - npx: add --yes to skip package-install prompts.
        - grep -r / --recursive: add --exclude-dir for .git and other noise dirs.
          A bare recursive grep walks .git/objects/pack files (100 MB+ binary) and
          node_modules, which can add hundreds of seconds per call. Skip injection
          when the caller already set any --exclude-dir flag.
        """
        command = _NPX_YES_RE.sub(r"\1 --yes ", command)

        if "--exclude-dir" not in command and re.search(
            r"\bgrep\b.*(-[a-zA-Z]*r[a-zA-Z]*|--recursive\b)",
            command,
            re.IGNORECASE,
        ):
            command = re.sub(
                r"\bgrep\b",
                (
                    "grep"
                    " --exclude-dir=.git"
                    " --exclude-dir=node_modules"
                    " --exclude-dir=__pycache__"
                    " --exclude-dir=.venv"
                    " --exclude-dir=venv"
                    " --exclude-dir=.next"
                    " --exclude-dir=dist"
                    " --exclude-dir=build"
                ),
                command,
                count=1,
                flags=re.IGNORECASE,
            )

        return command

    def _dispatch_local(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None,
        interactive: bool = False,
        background: bool = False,
    ) -> ToolMessage | str:
        """Route a command to the right local execution path: background task
        (server / long-running), interactive-prompt-aware, or plain foreground.
        Shared by the ``shell``/``bash`` tools and the intercepted ``execute``."""
        if not interactive and is_interactive_command(command):
            interactive = True
        if background or is_long_running_command(command):
            return self._run_background_shell_command(command, tool_call_id=tool_call_id, prog=prog)
        if interactive:
            return self._run_interactive_shell_command(command, tool_call_id=tool_call_id, prog=prog)
        return self._run_foreground_with_fallback(command, tool_call_id=tool_call_id, prog=prog)

    def _intercept_execute(self, request: Any) -> str | None:
        """Return the command string when ``request`` is a local ``execute`` call
        we should handle ourselves (to add background + Ctrl+B support), else None.

        Only intercepts in local mode — a real sandbox backend keeps its own
        ``execute`` so commands still run inside the sandbox.
        """
        try:
            tc = request.tool_call
            if tc.get("name") != "execute":
                return None
            if self._supports_sandbox_execution():
                return None  # sandbox owns execute
            cmd = (tc.get("args") or {}).get("command")
            return cmd if isinstance(cmd, str) and cmd.strip() else None
        except Exception:  # noqa: BLE001
            return None

    def wrap_tool_call(
        self, request: Any, handler: Callable[[Any], ToolMessage | Any]
    ) -> ToolMessage | Any:
        """Intercept local ``execute`` and run it through Nova's shell path so it
        gets background/Ctrl+B parity with ``shell``. Uses ``prog=None`` (the
        platform default shell) to match deepagents' ``execute`` semantics."""
        cmd = self._intercept_execute(request)
        if cmd is None:
            return handler(request)
        args = request.tool_call.get("args") or {}
        return self._dispatch_local(
            cmd,
            tool_call_id=request.tool_call.get("id"),
            prog=None,
            interactive=bool(args.get("interactive", False)),
            background=bool(args.get("background", False)),
        )

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[ToolMessage | Any]]
    ) -> ToolMessage | Any:
        """Async twin of :meth:`wrap_tool_call`. Runs the (blocking) local dispatch
        in a thread so the event loop keeps serving the ⚙ tasks / detach flow."""
        cmd = self._intercept_execute(request)
        if cmd is None:
            return await handler(request)
        args = request.tool_call.get("args") or {}
        return await self._adispatch_local(
            cmd,
            tool_call_id=request.tool_call.get("id"),
            prog=None,
            interactive=bool(args.get("interactive", False)),
            background=bool(args.get("background", False)),
        )

    def _run_async(
        self,
        make_coro: Callable[[], Awaitable[ToolMessage]],
        *,
        timeout: float,
        detach_future: Any = None,
    ) -> ToolMessage:
        """Run a coroutine to completion from this (synchronous) tool body.

        The agent runs async, so a sync tool needing asyncio must bridge: if an
        event loop is already running, execute in a worker thread with its own
        loop; otherwise run it directly. ``make_coro`` is a thunk so the coroutine
        is created exactly once, on whichever path is taken. Exceptions (including
        the worker's ``TimeoutError``) propagate to the caller.

        ``detach_future`` (a ``concurrent.futures.Future``) supports Ctrl+B
        backgrounding: when the coroutine sets it, we return that "backgrounded"
        result to the agent *immediately* while leaving the worker thread running
        so it keeps draining the process to completion. Only used on the in-loop
        (TUI) path — the direct path has no separate thread to keep alive.
        """
        import concurrent.futures

        # Detach-capable commands run on the shared background loop so a Ctrl+B
        # detach can return to the agent while the coroutine keeps draining the
        # process here. We just wait on it from this (worker) thread.
        if detach_future is not None:
            from novacode_cli.shell.jobs import get_background_loop

            main = asyncio.run_coroutine_threadsafe(make_coro(), get_background_loop())
            done, _ = concurrent.futures.wait(
                {main, detach_future},
                timeout=timeout,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if detach_future in done:
                # Backgrounded: hand the "job started" message to the agent now;
                # `main` keeps running on the background loop to completion.
                return detach_future.result()
            if main in done:
                return main.result()
            # Timed out waiting. The coroutine has its own self._timeout guard
            # and terminates the subprocess on its own; surface a TimeoutError.
            raise TimeoutError

        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if in_loop:
            # Don't use `with ThreadPoolExecutor()` — its __exit__ calls
            # shutdown(wait=True), which blocks until the submitted thread
            # finishes even when result() already raised TimeoutError. That
            # adds up to self._timeout extra seconds after the outer deadline.
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                return executor.submit(asyncio.run, make_coro()).result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise TimeoutError from None
            finally:
                executor.shutdown(wait=False)
        return asyncio.run(make_coro())

    def _supports_sandbox_execution(self) -> bool:
        """Check whether the backend can actually execute shell commands.

        This must use the same routing rule as ``CompositeBackend.execute``
        — it delegates to the *default* backend — but we intentionally restrict
        the check to real sandbox backends (subclasses of ``BaseSandbox``). Nova's
        local-mode backend, ``LocalShellBackend``, implements
        ``SandboxBackendProtocol`` but is **not** a sandbox; we want the
        ``ShellMiddleware`` to handle local execution itself so dangerous-command
        blocklists, OS confinement, and background-process management keep
        working. Remote sandbox wrappers such as ``WorkdirSandboxBackend`` extend
        ``BaseSandbox`` and are therefore treated as sandboxes.

        Returns:
            True only if the (default) backend is a real sandbox backend.
        """
        if self._backend is None:
            return False

        from deepagents.backends.sandbox import BaseSandbox

        # CompositeBackend: execution always delegates to the *default* backend,
        # so the sandbox question is really "is the default a real sandbox?".
        try:
            from deepagents.backends import CompositeBackend

            if isinstance(self._backend, CompositeBackend):
                return isinstance(self._backend.default, BaseSandbox)
        except ImportError:
            pass

        # Direct backend: it supports execution iff it is a real sandbox backend.
        return isinstance(self._backend, BaseSandbox)

    def _run_shell_command(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None = None,
    ) -> ToolMessage | str:
        """Execute a shell command and return the result.

        This method uses dynamic prompt detection:
        1. Run the command with a short initial timeout
        2. If output contains a prompt pattern, automatically switch to interactive mode
        3. Otherwise, continue with normal execution

        When a sandbox backend is configured, commands are executed in the sandbox
        instead of locally.

        Args:
            command: The shell command to execute.
            tool_call_id: The tool call ID for creating a ToolMessage.
            prog: argv prefix used to exec the command (e.g. PowerShell / bash); ``None`` runs it in the platform default shell.

        Returns:
            A ToolMessage with the command output or an error message.
        """
        prepared = self._prepare_foreground(command, tool_call_id=tool_call_id, prog=prog)
        if isinstance(prepared, ToolMessage):
            return prepared
        command = prepared

        # If sandbox backend is available, execute in sandbox
        if self._supports_sandbox_execution():
            return self._run_sandbox_command(command, tool_call_id=tool_call_id)

        # Local execution (original behavior)
        return self._run_local_command(command, tool_call_id=tool_call_id, prog=prog)

    def _prepare_foreground(
        self, command: str, *, tool_call_id: str | None, prog: list[str] | None
    ) -> str | ToolMessage:
        """Validate + convert a foreground command; a ToolMessage means "blocked".

        Shared by the sync and async paths so the checks can never drift apart.
        """
        if not command or not isinstance(command, str):
            msg = "Shell tool expects a non-empty command string."
            raise ToolException(msg)

        # Convert Unix→cmd.exe ONLY for the native fallback path (prog is None on
        # Windows ⇒ cmd.exe). PowerShell/bash (prog set) run the syntax as-is.
        if prog is None:
            command = _convert_unix_command_to_windows(command)

        dangerous, reason = is_dangerous_command(command)
        if dangerous:
            blocked_msg = (
                f"Command blocked: matches dangerous pattern `{reason}`. "
                "If intentional, run it manually in your terminal."
            )
            return ToolMessage(
                content=blocked_msg,
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

        # Snapshot files targeted by rm before they are deleted (local only)
        if self._backend is None and re.search(r"\brm\b", command):
            try:
                from novacode_cli.recovery import (
                    extract_rm_targets,
                    get_recovery_manager,
                )

                mgr = get_recovery_manager()
                if mgr:
                    for target in extract_rm_targets(command, Path(self._workspace_root)):
                        mgr.snapshot(target, reason="rm-command", command=command)
            except Exception:
                pass  # never block execution due to snapshot failure
        return command

    # ── async twins: await the command without holding a thread ─────────────

    async def _adispatch_local(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None,
        interactive: bool = False,
        background: bool = False,
    ) -> ToolMessage | str:
        """Async :meth:`_dispatch_local`: same routing, no thread held while a
        foreground command runs (it drains on the shared background loop)."""
        if not interactive and is_interactive_command(command):
            interactive = True
        if background or is_long_running_command(command) or self._supports_sandbox_execution():
            # Short, bounded waits (<=8s startup poll) or a sandbox round-trip.
            return await asyncio.to_thread(
                self._dispatch_local,
                command,
                tool_call_id=tool_call_id,
                prog=prog,
                interactive=interactive,
                background=background,
            )
        if interactive:
            return await self._arun_local(
                command, tool_call_id=tool_call_id, prog=prog, interactive=True
            )
        result = await self._arun_foreground(command, tool_call_id=tool_call_id, prog=prog)
        if not self._native_is_pwsh or not _looks_like_shell_rejection(result):
            return result
        alt = self._alternate_prog(prog)
        if alt is None or alt is prog:
            return result
        retry = await self._arun_foreground(command, tool_call_id=tool_call_id, prog=alt)
        return retry if not _looks_like_shell_rejection(retry) else result

    async def _arun_foreground(
        self, command: str, *, tool_call_id: str | None, prog: list[str] | None
    ) -> ToolMessage:
        prepared = self._prepare_foreground(command, tool_call_id=tool_call_id, prog=prog)
        if isinstance(prepared, ToolMessage):
            return prepared
        return await self._arun_local(prepared, tool_call_id=tool_call_id, prog=prog)

    async def _arun_local(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None,
        interactive: bool = False,
    ) -> ToolMessage:
        """Async :meth:`_run_local_command` / :meth:`_run_interactive_shell_command`."""
        import concurrent.futures

        if interactive:
            if not command or not isinstance(command, str):
                msg = "Shell tool expects a non-empty command string."
                raise ToolException(msg)
            if prog is None:
                command = _convert_unix_command_to_windows(command)
            dangerous, reason = is_dangerous_command(command)
            if dangerous:
                return ToolMessage(
                    content=(
                        f"Command blocked: matches dangerous pattern `{reason}`. "
                        "If intentional, run it manually in your terminal."
                    ),
                    tool_call_id=tool_call_id,
                    name=self._tool_name,
                    status="error",
                )
        command = self._preprocess_command(command)
        detach_future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            return await self._arun_on_bg_loop(
                lambda: self._async_local_shell(
                    command,
                    tool_call_id=tool_call_id,
                    prog=prog,
                    detach_future=detach_future,
                    **({"prompt_window": self._timeout} if interactive else {}),
                ),
                timeout=self._timeout + 15,
                detach_future=detach_future,
            )
        except TimeoutError:
            return ToolMessage(
                content=f"Error: Command timed out after {self._timeout:.0f} seconds.",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )
        except OSError as e:
            return ToolMessage(
                content=f"Error running command: {e}",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

    async def _arun_on_bg_loop(
        self,
        make_coro: Callable[[], Awaitable[ToolMessage]],
        *,
        timeout: float,
        detach_future: Any,
    ) -> ToolMessage:
        """Async :meth:`_run_async` (detach path): await instead of blocking.

        The command still runs on the shared background loop, so a Ctrl+B detach
        returns at once while the drain carries on there. Cancelling the caller
        (Esc ends the turn) does not cancel the command: Esc stops it through
        ``request_kill``, and a detached command must outlive its turn.
        """
        from novacode_cli.shell.jobs import get_background_loop

        main = asyncio.run_coroutine_threadsafe(make_coro(), get_background_loop())
        waiters = {asyncio.wrap_future(main), asyncio.wrap_future(detach_future)}
        await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if detach_future.done():
            return detach_future.result()
        if main.done():
            return main.result()
        raise TimeoutError

    def _run_sandbox_command(
        self,
        command: str,
        *,
        tool_call_id: str | None,
    ) -> ToolMessage:
        """Execute a shell command in the sandbox backend.

        Args:
            command: The shell command to execute.
            tool_call_id: The tool call ID for creating a ToolMessage.

        Returns:
            A ToolMessage with the command output or an error message.
        """
        try:
            # Call the sandbox backend's execute method
            result = self._backend.execute(command)

            # Format output for LLM consumption
            output = result.output or "<no output>"

            if len(output) > self._max_output_bytes:
                output = output[: self._max_output_bytes]
                output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."

            if result.exit_code is not None and result.exit_code != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.exit_code}"
                status = "error"
            else:
                status = "success"

            if result.truncated:
                output += "\n[Output was truncated due to size limits]"

            return ToolMessage(
                content=output,
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status=status,
            )
        except NotImplementedError as e:
            return ToolMessage(
                content=f"Error: Sandbox execution not available. {e}",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )
        except Exception as e:
            return ToolMessage(
                content=f"Error executing command in sandbox: {e}",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

    def _run_sandbox_background_command(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        settle_secs: float = 2.0,
    ) -> ToolMessage:
        """Launch a long-running command detached inside the sandbox.

        Sandbox ``execute`` waits for the command to exit, so a server that never
        exits hangs the call until the outer timeout. We instead ``nohup`` the
        command into the background (output redirected to a log, stdin closed) so
        ``execute`` returns immediately, wait a couple of seconds, then report
        whether it is still running — capturing the startup banner (e.g.
        ``Serving HTTP on ...``) or, if it died early (port in use, crash), the
        error output. The server keeps running for the rest of the task.

        Args:
            command: The shell command to launch (Linux/bash — sandboxes are Linux).
            tool_call_id: Tool call ID for the resulting ToolMessage.
            settle_secs: How long to wait before checking liveness.

        Returns:
            A ToolMessage describing the launch (status=error if it exited early).
        """
        import shlex
        import uuid

        cid = (tool_call_id or "").replace("/", "_")[:12] or uuid.uuid4().hex[:8]
        # /tmp inside the (Linux) sandbox container, namespaced per tool call.
        log_path = f"/tmp/nova-bg-{cid}.log"  # noqa: S108 — sandbox container path
        cmd_q = shlex.quote(command)
        log_q = shlex.quote(log_path)
        settle = max(1, int(settle_secs))

        # nohup … & detaches; redirecting std{out,err}→log and stdin←/dev/null
        # ensures the child doesn't hold the exec pipe open (which would re-hang).
        script = (
            f"nohup sh -c {cmd_q} > {log_q} 2>&1 < /dev/null & "
            f"NOVA_BG_PID=$!; "
            f"sleep {settle}; "
            f'if kill -0 "$NOVA_BG_PID" 2>/dev/null; then '
            f'echo "[background] started (pid $NOVA_BG_PID), still running after {settle}s"; '
            f'echo "[background] logs: {log_path}"; '
            f"echo '--- startup output ---'; tail -n 30 {log_q} 2>/dev/null; "
            f"else "
            f'echo "[background] process exited within {settle}s — it did not stay up:"; '
            f"cat {log_q} 2>/dev/null; "
            f"exit 1; "
            f"fi"
        )
        return self._run_sandbox_command(script, tool_call_id=tool_call_id)

    def _run_local_command(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None = None,
    ) -> ToolMessage:
        """Execute a shell command locally, streaming to completion.

        The command runs (on a single process — never re-executed) until it exits
        or the full ``self._timeout`` elapses. The first few seconds double as
        interactive-prompt detection: a known prompt is auto-answered on the
        process's stdin; an unanswerable prompt aborts with guidance instead of
        blocking. Output is returned in the ToolMessage — nothing is written to
        stdout (that corrupts the Textual TUI; see ``iterate_agent_events``).

        Args:
            command: The shell command to execute.
            tool_call_id: The tool call ID for creating a ToolMessage.
            prog: argv prefix used to exec the command (e.g. PowerShell / bash); ``None`` runs it in the platform default shell.

        Returns:
            A ToolMessage with the command output or an error message.
        """
        command = self._preprocess_command(command)
        import concurrent.futures

        detach_future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            return self._run_async(
                lambda: self._async_local_shell(
                    command, tool_call_id=tool_call_id, prog=prog, detach_future=detach_future
                ),
                timeout=self._timeout + 15,
                detach_future=detach_future,
            )
        except TimeoutError:
            return ToolMessage(
                content=f"Error: Command timed out after {self._timeout:.0f} seconds.",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )
        except OSError as e:
            return ToolMessage(
                content=f"Error running command: {e}",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

    async def _spawn(
        self,
        command: str,
        prog: list[str] | None,
        *,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        """Create the subprocess in the correct shell.

        When *prog* is set (e.g. ``[pwsh, "-NoProfile", "-Command"]`` or
        ``[bash, "-c"]``) the command runs in that exact shell via
        ``create_subprocess_exec`` — no cmd.exe wrapper, no quoting surprises.
        When *prog* is ``None`` it falls back to the platform default shell
        (POSIX ``sh`` on Unix, ``cmd.exe`` on Windows).
        """
        kwargs = {
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
            "cwd": self._workspace_root,
            "env": self._env,
        }
        # Put the child in its own process group / session so we can terminate the
        # WHOLE tree later (e.g. `npm run dev` → node children). We never rely on
        # terminal signal propagation — termination is always explicit — so this is
        # safe for foreground commands too.
        if sys.platform == "win32":
            import subprocess as _sp

            kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        if prog is not None:
            return await asyncio.create_subprocess_exec(*prog, command, **kwargs)
        return await asyncio.create_subprocess_shell(command, **kwargs)

    async def _terminate_tree(self, proc: asyncio.subprocess.Process, *, grace: float = 3.0) -> None:
        """Stop a process and its children: terminate, wait ``grace``, force-kill.

        Uses the process group created in ``_spawn`` so child processes die too
        (``proc.kill()`` alone kills only the shell: `npm run dev` left node
        running). ``grace=0`` force-kills at once (Esc, timeout). Fully async:
        it runs on the shared background loop that every running command and
        background task drains on, so nothing here may block that thread.
        """
        if proc.returncode is not None:
            return
        import os
        import signal

        if grace > 0:
            try:
                if sys.platform == "win32":
                    proc.terminate()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
                return
            except (TimeoutError, asyncio.TimeoutError):
                pass
        # Still alive → force-kill the whole tree.
        try:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001 — last resort
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _launch_background(self, command: str, prog: list[str] | None = None):
        """Registry launcher: start ``command`` as a fresh background job."""
        from novacode_cli.shell import jobs as _jobs

        reg = _jobs.get_registry()
        job = reg.add(command, self._tool_name, prog)
        asyncio.run_coroutine_threadsafe(
            self._bg_run(command, prog, job.id), _jobs.get_background_loop()
        )
        return job

    async def _bg_run(self, command: str, prog: list[str] | None, job_id: int) -> None:
        """Spawn ``command`` and drain it into an already-registered background job.

        Used for restart / start-in-background. Streams stdout+stderr into the
        job's bounded log buffer and honours terminate requests (``job.kill``).
        """
        from novacode_cli.shell import jobs as _jobs

        reg = _jobs.get_registry()
        job = reg.get(job_id)
        if job is None:
            return
        try:
            wrapped = wrap_command(command, self._os_policy)
            proc = await self._spawn(
                wrapped,
                prog,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            reg.attach_pid(job_id, proc.pid)
            while True:
                if job.kill.is_set():
                    await self._terminate_tree(proc)
                    reg.mark_terminated(job_id, proc.returncode)
                    return
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(1024), timeout=0.5)  # type: ignore[union-attr]
                except (TimeoutError, asyncio.TimeoutError):
                    if proc.returncode is not None:
                        break
                    continue
                if not chunk:
                    break
                reg.append_log(job_id, chunk.decode("utf-8", errors="replace"))
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (TimeoutError, asyncio.TimeoutError):
                    await self._terminate_tree(proc)
            reg.complete(job_id, proc.returncode)
        except Exception as e:  # noqa: BLE001 — never leave a job stuck "running"
            reg.complete(job_id, None, output=f"\n[background run error: {e}]")

    async def _async_local_shell(  # noqa: PLR0912, PLR0915
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prompt_window: float = 5.0,
        prog: list[str] | None = None,
        detach_future: Any = None,
    ) -> ToolMessage:
        """Stream a local command to completion (up to ``self._timeout``).

        Reads output on one long-lived process. During the first ``prompt_window``
        seconds, a stall with prompt-like output is treated as an interactive
        prompt — auto-answered if known, otherwise aborted with guidance (the agent
        can re-run with non-interactive flags) rather than hanging on console input.
        """
        import time

        # Confine the command to the workspace via the OS sandbox (no-op when
        # confinement is disabled or unavailable). Done here, at the single local
        # launch point, so interactive + non-interactive paths are both covered.
        command = wrap_command(command, self._os_policy)

        proc = await self._spawn(
            command,
            prog,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Publish a control handle so the TUI can reach this subprocess (it runs
        # in a detached thread+loop that Textual task-cancellation can't touch).
        # Esc sets ``kill`` → we terminate promptly instead of freezing for the
        # full timeout. Cleared in ``finally``.
        from novacode_cli.shell import jobs as _jobs

        _ctl = _jobs.set_current(command)

        out_parts: list[str] = []
        tail = ""  # rolling last-4KB window, for prompt detection only
        last_prompt = ""
        start = time.time()
        status = "success"
        note = ""
        detached = False
        _job = None

        try:
            while True:
                if _ctl.kill.is_set():
                    await self._terminate_tree(proc, grace=0)
                    status = "error"
                    note = "\n\n[Command killed by user (Esc).]"
                    break
                if detached and _job is not None and _job.kill.is_set():
                    # terminate_task / panel Stop → gracefully kill the tree.
                    await self._terminate_tree(proc)
                    _jobs.get_registry().mark_terminated(_job.id, proc.returncode)
                    break
                if not detached and _ctl.detach.is_set():
                    # Ctrl+B: hand this command to a background job and return a
                    # "backgrounded" result to the agent NOW. We keep looping to
                    # drain the process into the job's bounded log buffer.
                    detached = True
                    _reg = _jobs.get_registry()
                    _job = _reg.add(command, self._tool_name, prog)
                    _job.resume_on_done = True  # agent was mid-task → auto-resume
                    _reg.attach_pid(_job.id, proc.pid)
                    # Seed the job log with output captured before detaching.
                    _job.logs.extend(out_parts)
                    out_parts = []  # stop unbounded growth; logs are bounded
                    if detach_future is not None and not detach_future.done():
                        detach_future.set_result(
                            ToolMessage(
                                content=(
                                    f"[backgrounded: {_job.task_id}] `{command}` is running in the "
                                    f"background. Do NOT wait for it — continue with other work; "
                                    f"you'll be notified when it finishes. Check progress anytime "
                                    f"(non-blocking) with get_task_status('{_job.task_id}') / "
                                    f"get_task_logs('{_job.task_id}'), or list_background_tasks()."
                                ),
                                tool_call_id=tool_call_id,
                                name=self._tool_name,
                                status="success",
                            )
                        )
                if not detached and time.time() - start > self._timeout:
                    # A process still alive at the timeout that looks like a server
                    # (printed a "listening/serving/ready" banner) should NOT be
                    # killed — promote it to a background task so it keeps serving.
                    _looks_like_server = proc.returncode is None and any(
                        is_server_ready(ln) for ln in "".join(out_parts).splitlines()[-15:]
                    )
                    if _looks_like_server:
                        detached = True
                        _reg = _jobs.get_registry()
                        _job = _reg.add(command, self._tool_name, prog)
                        _reg.attach_pid(_job.id, proc.pid)
                        _job.logs.extend(out_parts)
                        out_parts = []
                        if detach_future is not None and not detach_future.done():
                            detach_future.set_result(
                                ToolMessage(
                                    content=(
                                        f"[{_job.task_id}] `{command}` is still running past the "
                                        f"{self._timeout:.0f}s foreground limit and looks like a "
                                        f"server — moved to the background so it keeps serving. "
                                        f"Inspect with get_task_logs('{_job.task_id}'), stop with "
                                        f"terminate_task('{_job.task_id}')."
                                    ),
                                    tool_call_id=tool_call_id,
                                    name=self._tool_name,
                                    status="success",
                                )
                            )
                        continue  # keep draining as a background task
                    await self._terminate_tree(proc, grace=0)
                    status = "error"
                    note = f"\n\n[Command exceeded {self._timeout:.0f}s and was terminated.]"
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(1024), timeout=0.5)  # type: ignore[union-attr]
                except TimeoutError:
                    # No output right now. Within the prompt window, check whether the
                    # process is blocked waiting on an interactive prompt.
                    if (
                        not detached
                        and tail
                        and tail != last_prompt
                        and (time.time() - start) <= prompt_window
                        and proc.returncode is None
                        and is_interactive_prompt(tail)
                    ):
                        last_prompt = tail
                        auto = get_auto_answer(tail)
                        if auto is not None and proc.stdin is not None:
                            proc.stdin.write((auto + "\n").encode())
                            try:
                                await proc.stdin.drain()
                            except (OSError, ConnectionResetError):
                                pass
                        else:
                            await self._terminate_tree(proc, grace=0)
                            status = "error"
                            note = (
                                "\n\n[This command is waiting for interactive input, which "
                                "can't be provided here. Re-run it non-interactively — pass "
                                "flags like --yes / --defaults / -y, or pipe the input via stdin.]"
                            )
                            break
                    continue
                if not chunk:
                    break  # stdout closed → process is exiting
                text = chunk.decode("utf-8", errors="replace")
                if detached and _job is not None:
                    # Detached: append to the job's BOUNDED log buffer (not the
                    # unbounded out_parts) and stop streaming to the tool widget.
                    _jobs.get_registry().append_log(_job.id, text)
                else:
                    out_parts.append(text)
                    tail = (tail + text)[-4096:]
                    if tool_call_id:
                        from novacode_cli.events import emit_tool_output
                        emit_tool_output(tool_call_id, text)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_tree(proc, grace=0))
            raise
        finally:
            _jobs.clear_current(_ctl)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except TimeoutError:
                    await self._terminate_tree(proc, grace=0)
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        output = "".join(out_parts).strip() or "<no output>"
        if len(output) > self._max_output_bytes:
            output = (
                output[: self._max_output_bytes]
                + f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            )
        rc = proc.returncode
        if status == "success" and rc not in (0, None):
            status = "error"
            output = f"{output}\n\nExit code: {rc}"

        if detached and _job is not None:
            # The agent already got the "backgrounded" message; finalize the job
            # (fires the TUI notify + observers). No-ops if it was terminated. The
            # return value below is discarded by _run_async on the detach path.
            _jobs.get_registry().complete(_job.id, rc, output=note or None)

        return ToolMessage(
            content=output + note,
            tool_call_id=tool_call_id,
            name=self._tool_name,
            status=status,
        )

    def _run_interactive_shell_command(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None = None,
    ) -> ToolMessage:
        """Run a command that may prompt for input, handling prompts non-blockingly.

        Shares the streaming path with ``_run_local_command`` — the only difference
        is that prompt detection stays active for the *whole* command (not just the
        first few seconds). Known prompts (npm/npx "ok to proceed?", …) are
        auto-answered on the process's stdin; an unanswerable prompt aborts with
        guidance rather than blocking on console input (there is no console to read
        from under the Textual TUI). Nothing is written to stdout.

        Args:
            command: The shell command to execute.
            tool_call_id: The tool call ID for creating a ToolMessage.
            prog: argv prefix used to exec the command (e.g. PowerShell / bash); ``None`` runs it in the platform default shell.

        Returns:
            A ToolMessage with the command output or an error message.
        """
        if not command or not isinstance(command, str):
            msg = "Shell tool expects a non-empty command string."
            raise ToolException(msg)

        if prog is None:
            command = _convert_unix_command_to_windows(command)
        command = self._preprocess_command(command)

        dangerous, reason = is_dangerous_command(command)
        if dangerous:
            return ToolMessage(
                content=(
                    f"Command blocked: matches dangerous pattern `{reason}`. "
                    "If intentional, run it manually in your terminal."
                ),
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

        import concurrent.futures

        detach_future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            return self._run_async(
                lambda: self._async_local_shell(
                    command,
                    tool_call_id=tool_call_id,
                    prompt_window=self._timeout,
                    prog=prog,
                    detach_future=detach_future,
                ),
                timeout=self._timeout + 15,
                detach_future=detach_future,
            )
        except TimeoutError:
            return ToolMessage(
                content=f"Error: Command timed out after {self._timeout:.0f} seconds.",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )
        except OSError as e:
            return ToolMessage(
                content=f"Error running interactive command: {e}",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

    def _run_background_shell_command(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        startup_timeout: float = 60.0,
        prog: list[str] | None = None,
    ) -> ToolMessage | str:
        """Execute a long-running shell command in the background.

        This method starts the command, watches output for "server ready" signals,
        and returns success once the server is up. The process continues running
        in the background.

        Note: In sandbox mode, background commands are executed synchronously
        since the sandbox doesn't support persistent background processes.

        Args:
            command: The shell command to execute.
            tool_call_id: The tool call ID for creating a ToolMessage.
            prog: argv prefix used to exec the command (e.g. PowerShell / bash); ``None`` runs it in the platform default shell.
            startup_timeout: Maximum time to wait for server to be ready.

        Returns:
            A ToolMessage with the startup output or an error message.
        """
        if not command or not isinstance(command, str):
            msg = "Shell tool expects a non-empty command string."
            raise ToolException(msg)

        # Convert Unix→cmd.exe only for the native fallback (prog is None).
        if prog is None:
            command = _convert_unix_command_to_windows(command)
        command = self._preprocess_command(command)

        dangerous, reason = is_dangerous_command(command)
        if dangerous:
            blocked_msg = (
                f"Command blocked: matches dangerous pattern `{reason}`. "
                "If intentional, run it manually in your terminal."
            )
            return ToolMessage(
                content=blocked_msg,
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error",
            )

        # In a sandbox, launch the process DETACHED inside the container so the
        # call returns immediately. Running it synchronously (the old behavior)
        # blocked forever on never-exiting servers (e.g. `python -m http.server`),
        # which looked like the agent hanging until the outer timeout fired.
        if self._supports_sandbox_execution():
            return self._run_sandbox_background_command(command, tool_call_id=tool_call_id)

        # Local execution: register a first-class background TASK (⚙ panel,
        # get_task_logs / terminate_task, cleanup on exit) and wait briefly for a
        # "server ready" banner or an early crash before returning to the agent.
        return self._start_background_task(
            command, tool_call_id=tool_call_id, prog=prog, startup_timeout=startup_timeout
        )

    def _start_background_task(
        self,
        command: str,
        *,
        tool_call_id: str | None,
        prog: list[str] | None,
        startup_timeout: float,
    ) -> ToolMessage:
        """Start a long-running command (e.g. a dev server) as a background task.

        Launches it on the shared background loop via ``_bg_run`` (bounded logs,
        terminable, tracked in the ⚙ Tasks panel), then polls up to
        ``startup_timeout`` for a server-ready line or an early exit so the agent
        gets a useful startup banner without its turn hanging on a process that
        never exits.
        """
        import time as _t

        from novacode_cli.shell import jobs as _jobs

        reg = _jobs.get_registry()
        job = reg.add(command, self._tool_name, prog)
        # The AGENT started this (background=True, or a command detected as
        # long-running), so its result is part of work in progress: the monitor
        # reports completion back into the conversation instead of leaving a
        # note that waits for the user to type. See BackgroundJob.agent_launched.
        job.agent_launched = True
        asyncio.run_coroutine_threadsafe(
            self._bg_run(command, prog, job.id), _jobs.get_background_loop()
        )

        # Wait only briefly: return as soon as a ready banner shows, the process
        # exits, or it has been alive a couple seconds (many servers buffer their
        # "listening" line, so don't block the agent's turn waiting for it).
        deadline = _t.time() + min(max(startup_timeout, 2.0), 8.0)
        ready = False
        while _t.time() < deadline:
            if job.status != "running":
                break
            if any(is_server_ready(line) for line in job.output.splitlines()[-10:]):
                ready = True
                break
            if job.runtime() > 2.5:  # alive and past the crash-on-startup window
                break
            _t.sleep(0.2)

        tail = "\n".join(job.output.splitlines()[-20:]) or "<no output yet>"
        if job.status != "running":
            # Exited during startup — a crash, a port already in use, or simply a
            # short command that finished. Surface the output so the agent can react.
            bad = job.exit_code not in (0, None)
            return ToolMessage(
                content=f"[{job.task_id}] `{command}` exited during startup (exit {job.exit_code}):\n{tail}",
                tool_call_id=tool_call_id,
                name=self._tool_name,
                status="error" if bad else "success",
            )
        note = "is up and ready" if ready else "is running (still starting up)"
        return ToolMessage(
            content=(
                f"[{job.task_id}] `{command}` {note} in the background.\n\n"
                f"Startup output:\n{tail}\n\n"
                f"Do NOT wait for it — continue with the user's request. It keeps "
                f"running on its own; check progress anytime (non-blocking) with "
                f"get_task_logs('{job.task_id}') / get_task_status('{job.task_id}'), "
                f"or stop it with terminate_task('{job.task_id}')."
            ),
            tool_call_id=tool_call_id,
            name=self._tool_name,
            status="success",
        )


__all__ = [
    "ShellMiddleware",
]
