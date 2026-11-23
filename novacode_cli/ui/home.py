"""The home banner's saved layout, and the one description the client needs.

The terminal UI's home is fourteen layouts, each a Textual widget tree
(`novacode_cli/tui/home.py`). A socket client cannot mount those widgets, so the
shared part lives here — Textual-free, importable from the server: which layout
is saved, whether animation is on, and the facts the banner prints. The Rust
client draws its own banner from that description.

Nothing here imports Textual: `novacode_cli/tui/home.py` re-exports the tables,
so the two frontends read one definition and the no-Textual gate stays honest.
"""

from __future__ import annotations

import os

__all__ = [
    "HOME_STYLES",
    "home_payload",
    "home_style",
    "matrix_rain_enabled",
]

#: Every saved home layout: its menu label and its key.
HOME_STYLES: tuple[tuple[str, str], ...] = (
    ("Dashboard · navigation and system panels", "dashboard"),
    ("Classic · Matrix banner and chat", "classic"),
    ("Centered · wordmark and focused composer", "centered"),
    ("Banner · bold wordmark and open conversation", "banner"),
    ("Terminal · shell prompt and unboxed output", "terminal"),
    ("Workbench · character portrait and tool desk", "workbench"),
    ("Notebook · ruled entries and generous reading space", "notebook"),
    ("Outline · ASCII frames and plain controls", "outline"),
    ("Focus · narrow reading column and quiet chrome", "focus"),
    ("Studio · constellation portrait and soft controls", "studio"),
    ("Ledger · dense entries and compact command desk", "ledger"),
    ("Blueprint · scanning schematic and double rules", "blueprint"),
    ("Signal · Classic banner with Matrix3 rain", "signal"),
    ("Minimal · compact conversation and controls", "minimal"),
)

#: The layouts that show Nova's art before the first message.
BANNER_STYLES = ("dashboard", "classic", "signal")


def home_style(value: object, *, legacy_dashboard: object = True) -> str:
    """Resolve a saved style, retaining the previous Dashboard/Classic choice.

    Args:
        value: The saved `home_style` setting, as read from disk.
        legacy_dashboard: The retired boolean this setting replaced.

    Returns:
        A key from [`HOME_STYLES`], defaulting to `dashboard` (or `classic` when
        the legacy flag was explicitly turned off).
    """
    if isinstance(value, str) and value in {key for _, key in HOME_STYLES}:
        return value
    return "classic" if legacy_dashboard is False else "dashboard"


def matrix_rain_enabled(config: object) -> bool:
    """Whether the code rain animates, matching the terminal UI's rule.

    Args:
        config: Anything with `get(key, default)`; usually a `NovaConfig`.

    Returns:
        The saved `matrix_rain_enabled` flag, or `NOVA_ANIMATIONS` when unset.
    """
    saved = config.get("matrix_rain_enabled")  # type: ignore[attr-defined]
    if isinstance(saved, bool):
        return saved
    return os.environ.get("NOVA_ANIMATIONS", "").strip().lower() in {"1", "true", "yes"}


def home_payload(version: str) -> dict[str, object]:
    """The home description the client needs, for the `ready` frame.

    Args:
        version: Nova's version, printed under the wordmark.

    Returns:
        `{"style", "animation", "version"}`. Never raises: an unreadable config
        falls back to the default layout with animation off, because a client
        without a banner is better than a handshake that fails.
    """
    style = "dashboard"
    animation = False
    try:
        from novacode_cli.config.nova_config import NovaConfig

        config = NovaConfig()
        style = home_style(
            config.get("home_style"),
            legacy_dashboard=config.get("home_dashboard_enabled", True),
        )
        animation = matrix_rain_enabled(config)
    except (OSError, ValueError, TypeError):
        pass
    return {"style": style, "animation": animation, "version": version}
