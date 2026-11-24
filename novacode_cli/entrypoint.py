"""Lightweight console entry point for commands that do not need an agent."""

import sys
from contextlib import nullcontext

from novacode_cli.skills.upstream import add_arguments, run_skills_cli


def _startup_animation():
    """Animate the slow runtime import on an interactive terminal."""
    args = sys.argv[1:]
    if (
        not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))
        or {"--headless", "--serve", "--session-worker"}.intersection(args)
        or any(arg.startswith(("-p", "--print")) for arg in args)
        or "--mode=pipe" in args
        or any(arg == "--mode" and args[i + 1 : i + 2] == ["pipe"] for i, arg in enumerate(args))
    ):
        return nullcontext()

    from rich.console import Console
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text

    return Live(
        Spinner(
            "dots",
            Text.assemble(("NOVA", "bold cyan"), ("  Preparing workspace", "dim")),
            style="bright_magenta",
        ),
        console=Console(stderr=True),
        refresh_per_second=16,
        transient=True,
        redirect_stdout=False,
        redirect_stderr=False,
    )


def cli_main() -> None:
    """Forward skill installs before importing model setup or onboarding."""
    if sys.argv[1:] == ["--version"]:
        import io

        from rich.console import Console

        from novacode_cli._version import __version__
        from novacode_cli.brand import format_version_banner

        stream = (
            io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
            if sys.platform == "win32" and "pytest" not in sys.modules
            else sys.stdout
        )
        console = Console(highlight=False, file=stream)
        try:
            console.print(format_version_banner(__version__), markup=False, highlight=False)
        finally:
            if isinstance(stream, io.TextIOWrapper) and stream is not sys.stdout:
                stream.detach()  # this command borrows stdout; it does not own it
        return
    if sys.argv[1:2] == ["plugin"]:
        from novacode_cli.plugins.cli import main as plugin_main

        raise SystemExit(plugin_main(sys.argv[2:]))
    if sys.argv[1:2] == ["update"]:
        from novacode_cli.updates import update_main

        raise SystemExit(update_main(sys.argv[2:]))
    if sys.argv[1:2] == ["teammates"]:
        from novacode_cli.teammates.service import cli

        raise SystemExit(cli(sys.argv[2:]))
    # Upstream skills add owns its flags, including --help.
    if ("--help" in sys.argv[1:] or "-h" in sys.argv[1:]) and add_arguments(sys.argv[1:]) is None:
        from novacode_cli.cli_args import parse_args

        parse_args()
    arguments = add_arguments(sys.argv[1:])
    if arguments is not None:
        raise SystemExit(run_skills_cli("add", arguments))
    from novacode_cli.windows_launcher import supervise

    result = supervise(sys.argv[1:])
    if result is not None:
        raise SystemExit(result)
    from novacode_cli.update_guard import UpdateBlockedError, installation_lock

    try:
        with installation_lock(exclusive=False):
            with _startup_animation():
                from novacode_cli.main import cli_main as main

            main()
    except UpdateBlockedError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli_main()
