"""Patch the MCP SDK's Windows stdio spawn to use a hidden console.

The SDK spawns stdio MCP servers with CREATE_NO_WINDOW: the child gets *no*
console at all, so any grandchild the server spawns later (browser-use's
periodic curl calls, for example) is handed a brand-new *visible* console by
Windows — a terminal window that pops up and disappears.

A hidden console fixes it: CREATE_NEW_CONSOLE + STARTF_USESHOWWINDOW(SW_HIDE)
gives the server a console that exists but is invisible, and every descendant
inherits it, so nothing further allocates a visible window.

Run it again after the `mcp` package is updated or reinstalled (uv sync):
    uv run python scripts/patch_mcp_hidden_console.py [python-env-root]

Without an argument it patches the project venv; pass the venv root to patch
another environment (e.g. the uv tools venv).

ruff: noqa: T201
"""

import re
import sys
from pathlib import Path

MARKER = "# nova:hidden-console"

PATCHED_CREATE = (
    "            creationflags=subprocess.CREATE_NEW_CONSOLE | "
    "subprocess.CREATE_NO_WINDOW,  # nova:hidden-console\n"
    "            startupinfo=_nova_hidden_console_startupinfo(),  # nova:hidden-console\n"
)

PATCHED_FALLBACK = (
    '            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,\n'
    '            startupinfo=_nova_hidden_console_startupinfo(),  # nova:hidden-console\n'
)

HELPER = '''

def _nova_hidden_console_startupinfo() -> "subprocess.STARTUPINFO":
    """A STARTUPINFO that hides the new console this child creates.

    nova:hidden-console patch — see scripts/patch_mcp_hidden_console.py. The
    child's console exists but is invisible, and grandchildren inherit it, so
    servers that spawn subprocesses (curl, git, ...) no longer flash terminal
    windows on Windows.
    """
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return startupinfo
'''


def patch(root: Path) -> bool:
    target = root / "Lib" / "site-packages" / "mcp" / "os" / "win32" / "utilities.py"
    if not target.is_file():
        target = root / "site-packages" / "mcp" / "os" / "win32" / "utilities.py"
    if not target.is_file():
        print(f"  ? {target} not found — skipped")
        return False
    text = target.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"  = already patched: {target}")
        return False

    # The async path: CREATE_NO_WINDOW -> hidden console.
    create_pattern = re.compile(
        r"creationflags=subprocess\.CREATE_NO_WINDOW[^\n]*\n"
        r"\s*if hasattr\(subprocess, \"CREATE_NO_WINDOW\"\)\n"
        r"\s*else 0,",
    )
    text, count = create_pattern.subn(PATCHED_CREATE.rstrip("\n"), text)

    # The fallback path: plain CREATE_NO_WINDOW flag.
    fallback_pattern = re.compile(
        r"creationflags=getattr\(subprocess, \"CREATE_NO_WINDOW\", 0\),"
    )
    text, fallback_count = fallback_pattern.subn(
        PATCHED_FALLBACK.rstrip("\n"), text
    )

    # The startupinfo helper, once, before the first function that uses it.
    if "_nova_hidden_console_startupinfo" in text and "def _nova_hidden_console_startupinfo" not in text:
        anchor = "\nasync def create_windows_process("
        if anchor in text:
            text = text.replace(anchor, HELPER + anchor, 1)

    if count == 0 and fallback_count == 0:
        print(f"  ! no spawn sites matched in {target} — the SDK changed; skipped")
        return False

    target.write_text(text, encoding="utf-8")
    print(f"  + patched {target} (async={count}, fallback={fallback_count})")
    return True


def main() -> None:
    roots = [Path(sys.argv[1])] if len(sys.argv) > 1 else [
        Path(r"B:\Summer Project 2026\NovaCode\NovaCode\.venv"),
        Path(r"C:\Users\Babit-PC\AppData\Roaming\uv\tools\novacode-cli"),
    ]
    for root in roots:
        print(f"patching {root}:")
        patch(root)


if __name__ == "__main__":
    main()
