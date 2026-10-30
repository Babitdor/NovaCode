"""UI-level configuration state slice.

Owns all fields that affect UI behavior independent of agent or runtime:
auto-approve, plan mode, verbose output, exit hints,
and the no_splash flag.
"""

from __future__ import annotations

from typing import Any


class UISettings:
    """UI-level configuration independent of agent or runtime.

    This slice owns fields that change UI/notification behavior, regardless
    of which agent or bridge is active.
    """

    def __init__(
        self,
        auto_approve: bool = False,
        no_splash: bool = False,
    ) -> None:
        self.auto_approve = auto_approve
        self.no_splash = no_splash
        self.exit_hint_until: float | None = None
        self.exit_hint_handle: Any = None
        self.plan_mode_enabled: bool = False
        self.browser_mode_enabled: bool = False
        self.verbose: bool = False
        self.active_goal: str | None = None

    # -- toggles ---------------------------------------------------------------

    def toggle_auto_approve(self) -> bool:
        """Toggle auto-approve and return new state."""
        self.auto_approve = not self.auto_approve
        return self.auto_approve

    def toggle_plan_mode(self) -> bool:
        """Toggle plan mode and return new state."""
        self.plan_mode_enabled = not self.plan_mode_enabled
        return self.plan_mode_enabled

    def toggle_verbose(self) -> bool:
        """Toggle verbose mode and return new state."""
        self.verbose = not self.verbose
        return self.verbose
