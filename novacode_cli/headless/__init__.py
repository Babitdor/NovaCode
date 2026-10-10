"""Headless (non-interactive) Nova: ``nova -p "<prompt>"``."""


def __getattr__(name: str) -> object:
    """Keep argument validators independent of the execution runtime."""
    if name == "HeadlessOutput":
        from novacode_cli.headless.output import HeadlessOutput

        return HeadlessOutput
    if name == "run_headless":
        from novacode_cli.headless.runner import run_headless

        return run_headless
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)


__all__ = ["HeadlessOutput", "run_headless"]
