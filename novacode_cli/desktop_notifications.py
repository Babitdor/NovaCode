"""Windows notifications with a click target belonging to the emitting TUI.

Uses Shell_NotifyIcon directly: no command strings, protocol registration, or
second Nova process is needed. The native window and icon live on one thread.
"""

from __future__ import annotations

import io
import logging
import queue
import struct
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
ICON_PATH = Path(__file__).parent / "assets" / "Nova.png"


def icon_resource(path: Path = ICON_PATH) -> bytes:
    """Convert the packaged PNG to a Windows icon resource in memory."""
    from PIL import Image

    with Image.open(path) as image:
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="ICO", sizes=[(32, 32)], bitmap_format="bmp")
    data = output.getvalue()
    length, offset = struct.unpack_from("<II", data, 14)
    return data[offset : offset + length]


class DesktopNotifier:
    """Own a bounded set of native notifications until click, timeout, or exit."""

    def __init__(self, activate, *, native=None):
        self.activate = activate
        self.commands = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.native = native
        self.thread = None
        self.window = None

    def start(self):
        if self.thread is not None or sys.platform != "win32":
            return
        if self.native is None:
            self.native = _WindowsShell()
        self.window = self.native.capture_terminal()
        self.thread = threading.Thread(target=self._run, name="nova-notifications", daemon=True)
        self.thread.start()

    def show(self, sid: str, notification_id: str, title: str, message: str):
        if self.closed.is_set():
            return
        try:
            self.commands.put_nowait((sid, notification_id, title, message))
        except queue.Full:
            logger.debug("Desktop notification queue full; retained in TUI")
            return
        if self.ready.is_set():
            self.native.wake()

    def close(self):
        self.closed.set()
        if self.ready.is_set():
            self.native.stop()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def _clicked(self, sid, notification_id):
        if self.closed.is_set():
            return
        self.native.focus(self.window)
        self.activate(sid, notification_id)

    def _run(self):
        try:
            self.native.open(icon_resource(), self._clicked)
            self.ready.set()
            if self.closed.is_set():
                return
            self.native.wake()
            self.native.run(self.commands)
        except Exception:  # noqa: BLE001 — retain the in-terminal notification
            logger.warning("Windows desktop notifications unavailable", exc_info=True)
        finally:
            self.closed.set()
            self.native.close()


class _WindowsShell:
    """Win32 message-loop adapter. All pointer-sized arguments are declared."""

    CALLBACK = 0x8001
    WAKE = 0x8002

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        self.c, self.w = c, w
        self.user = c.WinDLL("user32", use_last_error=True)
        self.shell = c.WinDLL("shell32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.hwnd = None
        self.icon = None
        self.items = {}
        self.next_id = 1
        self.proc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WindowClass(c.Structure):
            _fields_ = [
                ("style", w.UINT),
                ("proc", self.proc_type),
                ("class_extra", c.c_int),
                ("window_extra", c.c_int),
                ("instance", w.HINSTANCE),
                ("icon", w.HICON),
                ("cursor", w.HANDLE),
                ("background", w.HBRUSH),
                ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR),
            ]

        class NotifyData(c.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("hwnd", w.HWND),
                ("id", w.UINT),
                ("flags", w.UINT),
                ("callback", w.UINT),
                ("icon", w.HICON),
                ("tip", w.WCHAR * 128),
                ("state", w.DWORD),
                ("state_mask", w.DWORD),
                ("info", w.WCHAR * 256),
                ("version", w.UINT),
                ("title", w.WCHAR * 64),
                ("info_flags", w.DWORD),
                ("guid", c.c_byte * 16),
                ("balloon_icon", w.HICON),
            ]

        self.WindowClass, self.NotifyData = WindowClass, NotifyData
        signatures = {
            "GetForegroundWindow": ([], w.HWND),
            "GetClassNameW": ([w.HWND, w.LPWSTR, c.c_int], c.c_int),
            "GetWindowThreadProcessId": ([w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            "IsWindow": ([w.HWND], w.BOOL),
            "ShowWindow": ([w.HWND, c.c_int], w.BOOL),
            "SetForegroundWindow": ([w.HWND], w.BOOL),
            "RegisterClassW": ([c.POINTER(WindowClass)], w.WORD),
            "UnregisterClassW": ([w.LPCWSTR, w.HINSTANCE], w.BOOL),
            "CreateWindowExW": (
                [
                    w.DWORD,
                    w.LPCWSTR,
                    w.LPCWSTR,
                    w.DWORD,
                    c.c_int,
                    c.c_int,
                    c.c_int,
                    c.c_int,
                    w.HWND,
                    w.HMENU,
                    w.HINSTANCE,
                    w.LPVOID,
                ],
                w.HWND,
            ),
            "DefWindowProcW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], c.c_ssize_t),
            "PostMessageW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
            "GetMessageW": ([c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], c.c_int),
            "TranslateMessage": ([c.POINTER(w.MSG)], w.BOOL),
            "DispatchMessageW": ([c.POINTER(w.MSG)], c.c_ssize_t),
            "DestroyWindow": ([w.HWND], w.BOOL),
            "DestroyIcon": ([w.HICON], w.BOOL),
            "CreateIconFromResourceEx": (
                [c.POINTER(c.c_byte), w.DWORD, w.BOOL, w.DWORD, c.c_int, c.c_int, w.UINT],
                w.HICON,
            ),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = w.HINSTANCE
        self.shell.Shell_NotifyIconW.argtypes = [w.DWORD, c.POINTER(NotifyData)]
        self.shell.Shell_NotifyIconW.restype = w.BOOL

    def capture_terminal(self):
        hwnd = self.user.GetForegroundWindow()
        name = self.c.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, name, len(name))
        if name.value not in {
            "CASCADIA_HOSTING_WINDOW_CLASS",
            "ConsoleWindowClass",
            "VirtualConsoleClass",
        }:
            return None
        pid = self.w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, self.c.byref(pid))
        return hwnd, pid.value

    def focus(self, window):
        if not window:
            return
        hwnd, expected_pid = window
        pid = self.w.DWORD()
        if self.user.IsWindow(hwnd):
            self.user.GetWindowThreadProcessId(hwnd, self.c.byref(pid))
            if pid.value == expected_pid:
                self.user.ShowWindow(hwnd, 9)  # SW_RESTORE
                self.user.SetForegroundWindow(hwnd)

    def open(self, resource, clicked):
        self.clicked = clicked
        raw = (self.c.c_byte * len(resource)).from_buffer_copy(resource)
        self.icon = self.user.CreateIconFromResourceEx(raw, len(resource), True, 0x30000, 32, 32, 0)
        if not self.icon:
            raise self.c.WinError(self.c.get_last_error())
        self.instance = self.kernel.GetModuleHandleW(None)
        self.name = f"NovaNotifications-{id(self)}"
        self.callback = self.proc_type(self._message)
        wc = self.WindowClass(proc=self.callback, instance=self.instance, name=self.name)
        if not self.user.RegisterClassW(self.c.byref(wc)):
            raise self.c.WinError(self.c.get_last_error())
        self.hwnd = self.user.CreateWindowExW(
            0, self.name, "NovaCode notifications", 0, 0, 0, 0, 0, None, None, self.instance, None
        )
        if not self.hwnd:
            raise self.c.WinError(self.c.get_last_error())

    def _message(self, hwnd, message, uid, event):
        if message == self.CALLBACK:
            if event == 0x405 and uid in self.items:  # NIN_BALLOONUSERCLICK
                sid, nid = self.items[uid]
                try:
                    self.clicked(sid, nid)
                except Exception:
                    logger.warning("Notification click could not be routed", exc_info=True)
            if event in (0x403, 0x404, 0x405):  # hide / timeout / click
                self._remove(uid)
            return 0
        return self.user.DefWindowProcW(hwnd, message, uid, event)

    def _data(self, uid):
        return self.NotifyData(size=self.c.sizeof(self.NotifyData), hwnd=self.hwnd, id=uid)

    def _remove(self, uid):
        self.shell.Shell_NotifyIconW(2, self.c.byref(self._data(uid)))
        self.items.pop(uid, None)

    def wake(self):
        self.user.PostMessageW(self.hwnd, self.WAKE, 0, 0)

    def stop(self):
        self.user.PostMessageW(self.hwnd, 0x12, 0, 0)  # WM_QUIT

    def run(self, commands):
        msg = self.w.MSG()
        while (result := self.user.GetMessageW(self.c.byref(msg), None, 0, 0)) > 0:
            if msg.message == self.WAKE:
                while True:
                    try:
                        sid, nid, title, text = commands.get_nowait()
                    except queue.Empty:
                        break
                    if len(self.items) >= 32:
                        self._remove(next(iter(self.items)))
                    uid = self.next_id
                    self.next_id += 1
                    data = self._data(uid)
                    data.flags = 1 | 2 | 4 | 16  # MESSAGE | ICON | TIP | INFO
                    data.callback, data.icon, data.balloon_icon = (
                        self.CALLBACK,
                        self.icon,
                        self.icon,
                    )
                    data.tip = "NovaCode"
                    data.title, data.info = title[:63], text[:255]
                    data.info_flags = 4 | 16 | 32 | 128  # USER | NOSOUND | LARGE_ICON | QUIET_TIME
                    self.items[uid] = sid, nid
                    if not self.shell.Shell_NotifyIconW(0, self.c.byref(data)):
                        self.items.pop(uid, None)
            else:
                self.user.TranslateMessage(self.c.byref(msg))
                self.user.DispatchMessageW(self.c.byref(msg))
        if result < 0:
            raise self.c.WinError(self.c.get_last_error())

    def close(self):
        for uid in list(self.items):
            self._remove(uid)
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
        if self.icon:
            self.user.DestroyIcon(self.icon)
        if getattr(self, "name", None):
            self.user.UnregisterClassW(self.name, self.instance)
