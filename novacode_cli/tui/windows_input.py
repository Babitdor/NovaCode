"""Preserve Textual's console input after Windows child programs change it."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.driver import Driver

logger = logging.getLogger(__name__)


def restore_input_mode(driver: Driver | None) -> None:
    """Repair an active Windows driver; leave suspended and headless terminals alone.

    Cua's cleanup can run Git's MSYS kill.exe, which resets the shared console
    input mode to 0x7. Textual requires virtual-terminal input for mouse events.
    Keep its existing shutdown restorer intact and write only when mode changed.
    """
    if sys.platform != "win32" or driver is None or driver.is_headless:
        return
    from textual.drivers import win32
    from textual.drivers.windows_driver import WindowsDriver

    if not isinstance(driver, WindowsDriver) or driver._event_thread is None:
        return
    if sys.__stdin__ is None:
        return
    try:
        expected = win32.ENABLE_VIRTUAL_TERMINAL_INPUT
        if win32.get_console_mode(sys.__stdin__) != expected and win32.set_console_mode(
            sys.__stdin__, expected
        ):
            driver._enable_mouse_support()
    except (OSError, ValueError):
        logger.debug("Could not restore Windows TUI input mode", exc_info=True)
