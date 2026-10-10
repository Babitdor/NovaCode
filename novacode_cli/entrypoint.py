"""Lightweight console entry point for commands that do not need an agent."""

import sys

from novacode_cli.skills.upstream import add_arguments, run_skills_cli


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
    if sys.argv[1:2] == ["update"]:
        from novacode_cli.updates import update_main

        raise SystemExit(update_main(sys.argv[2:]))
    # Upstream skills add owns its flags, including --help.
    if ("--help" in sys.argv[1:] or "-h" in sys.argv[1:]) and add_arguments(sys.argv[1:]) is None:
        from novacode_cli.cli_args import parse_args

        parse_args()
    arguments = add_arguments(sys.argv[1:])
    if arguments is not None:
        raise SystemExit(run_skills_cli("add", arguments))
    from novacode_cli.main import cli_main as main

    main()


if __name__ == "__main__":
    cli_main()
