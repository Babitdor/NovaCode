"""Fixed detached entry point; no agent-controlled launch arguments."""

from __future__ import annotations

import asyncio


def main() -> None:
    """Install normal import guards, then acquire profile ownership and serve."""
    from novacode_cli._lazy_heavy import install

    install()
    from novacode_cli.teammates.service import profile_directory, serve

    asyncio.run(serve(profile_directory()))


if __name__ == "__main__":
    main()
