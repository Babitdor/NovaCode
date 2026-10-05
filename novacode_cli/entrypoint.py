"""Lightweight console entry point for commands that do not need an agent."""

import sys

from novacode_cli.skills.upstream import add_arguments, run_skills_cli


def cli_main() -> None:
    """Forward skill installs before importing model setup or onboarding."""
    if sys.argv[1:2] == ["update"]:
        from novacode_cli.updates import update_main

        raise SystemExit(update_main(sys.argv[2:]))
    arguments = add_arguments(sys.argv[1:])
    if arguments is not None:
        raise SystemExit(run_skills_cli("add", arguments))
    from novacode_cli.main import cli_main as main

    main()


if __name__ == "__main__":
    cli_main()
