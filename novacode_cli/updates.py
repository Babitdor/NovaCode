"""Installation-aware update checks and explicit updates, without agent imports."""

# Explicit CLI output, fixed HTTPS endpoints, and subprocess argument lists.
# ruff: noqa: T201, S603, S607, S310

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

REPOSITORY = "Babitdor/NovaCode"
REPO_URL = f"https://github.com/{REPOSITORY}.git"
CHECK_INTERVAL = 3600


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


@dataclass(frozen=True)
class Installation:
    """The running installation, never the user's current working directory."""

    version: str
    kind: str
    revision: str = ""
    branch: str = "main"
    root: Path | None = None


@dataclass(frozen=True)
class UpdateStatus:
    """A check result suitable for both CLI and background notification."""

    available: bool
    current: str
    latest: str
    url: str = ""
    error: str = ""


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments],
        text=True,
        stderr=subprocess.PIPE,
        timeout=15,
    ).strip()


def _official_remote(url: str) -> bool:
    return url.lower().removesuffix(".git").rstrip("/") in {
        f"https://github.com/{REPOSITORY}".lower(),
        f"git@github.com:{REPOSITORY}".lower(),
        f"ssh://git@github.com/{REPOSITORY}".lower(),
    }


def detect_installation() -> Installation:
    """Use distribution provenance, falling back to an actual source checkout."""
    dist = importlib.metadata.distribution("novacode-cli")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    root = Path(__file__).resolve().parent.parent
    if direct.get("dir_info", {}).get("editable"):
        parsed = urlparse(direct.get("url", ""))
        local = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", local):
            local = local[1:]
        root = Path(local)
    if (root / "pyproject.toml").is_file() and (root / ".git").exists():
        remote = _git(root, "remote", "get-url", "origin")
        if not _official_remote(remote):
            _fail("This checkout uses a different origin; update it with Git.")
        branch = _git(root, "symbolic-ref", "--short", "HEAD")
        return Installation(dist.version, "source", _git(root, "rev-parse", "HEAD"), branch, root)
    vcs = direct.get("vcs_info", {})
    if vcs:
        if not _official_remote(direct.get("url", "")):
            _fail("This installation uses a different repository; use its installer.")
        ref = vcs.get("requested_revision", "main")
        if ref != "main":
            _fail("This installation is pinned to a Git ref; use its installer to change it.")
        kind = "uv-tool" if (Path(sys.prefix) / "uv-receipt.toml").is_file() else "git"
        return Installation(dist.version, kind, vcs.get("commit_id", ""))
    if direct:
        _fail("This local/archive installation must be updated from its original source.")
    kind = "uv-tool" if (Path(sys.prefix) / "uv-receipt.toml").is_file() else "package"
    return Installation(dist.version, kind)


def _get_json(url: str) -> dict:
    request = Request(
        url, headers={"User-Agent": "NovaCode-update-check", "Accept": "application/json"}
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def _cache_path() -> Path:
    return Path.home() / ".nova" / "cache" / "update-check.json"


def check_for_update(*, force: bool = False) -> UpdateStatus:
    """Check at most hourly. Network/cache failures never interrupt a session."""
    try:
        installation = detect_installation()
        current = installation.revision or installation.version
        key = f"{installation.kind}:{installation.branch}:{current}"
        cache_path = _cache_path()
        if not force:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                age = time.time() - cached["checked_at"]
                if cached["key"] == key and 0 <= age < CHECK_INTERVAL:
                    return UpdateStatus(**cached["status"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if installation.revision:
            from urllib.parse import quote

            latest = _get_json(
                f"https://api.github.com/repos/{REPOSITORY}/commits/"
                f"{quote(installation.branch, safe='')}"
            )["sha"]
            comparison = _get_json(
                f"https://api.github.com/repos/{REPOSITORY}/compare/"
                f"{installation.revision}...{latest}"
            )
            status = UpdateStatus(
                comparison["status"] in {"ahead", "diverged"},
                current,
                latest,
                comparison.get("html_url", f"https://github.com/{REPOSITORY}"),
            )
        else:
            from packaging.version import Version

            latest = _get_json("https://pypi.org/pypi/novacode-cli/json")["info"]["version"]
            status = UpdateStatus(
                Version(latest) > Version(current),
                current,
                latest,
                "https://pypi.org/project/novacode-cli/",
            )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(f"update-check-{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"key": key, "checked_at": time.time(), "status": asdict(status)}),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
        except OSError:
            pass
        return status  # noqa: TRY300
    except Exception as error:  # noqa: BLE001 - checks never prevent startup
        return UpdateStatus(available=False, current="", latest="", error=str(error))


def _run(arguments: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


def _start_windows_update(arguments: list[str]) -> Path:
    """Run uv from base Python after the calling launcher has exited."""
    log = _cache_path().with_name(f"update-{uuid.uuid4().hex}.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "command": arguments,
        "caller_pid": os.getpid(),
        "parent_pid": os.getppid(),
        "cache": str(_cache_path()),
    }
    interpreter = getattr(sys, "_base_executable", sys.executable)
    helper = Path(__file__).with_name("_windows_update.py")
    with log.open("w", encoding="utf-8") as output:
        subprocess.Popen(
            [interpreter, "-I", "-X", "utf8", str(helper), json.dumps(payload)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    return log


def install_update() -> Path | None:
    """Update with the installation's manager; never reset or stash a checkout."""
    installation = detect_installation()
    if installation.kind == "source":
        root = installation.root
        if root is None:
            _fail("Cannot locate Nova's source checkout.")
        if _git(root, "status", "--porcelain"):
            _fail("Checkout has local changes. Commit or stash them before updating.")
        upstream = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
        if upstream != f"origin/{installation.branch}":
            _fail("Branch must track the matching origin branch before updating.")
        uv = shutil.which("uv")
        use_uv = (
            uv is not None
            and (root / "uv.lock").is_file()
            and Path(sys.prefix).resolve() == (root / ".venv").resolve()
        )
        if not use_uv and not importlib.util.find_spec("pip") and uv is None:
            _fail("Install uv or pip in this environment before updating.")
        _run(["git", "-C", str(root), "fetch", "origin", installation.branch])
        _run(["git", "-C", str(root), "merge", "--ff-only", upstream])
        if use_uv:
            _run(
                [
                    uv,
                    "sync",
                    "--inexact",
                ],
                cwd=root,
            )
        else:
            target = str(root)
            _run([*_pip_command(), "--upgrade", "-e", target])
    elif installation.kind == "uv-tool":
        uv = shutil.which("uv")
        if uv is None:
            _fail("This installation is managed by uv; install uv to update it.")
        arguments = [uv, "tool", "upgrade", "novacode-cli"]
        if sys.platform == "win32":
            # Reinstall just Nova so uv repairs a launcher even when an earlier
            # failed update already advanced the installed package's version.
            arguments.extend(["--reinstall-package", "novacode-cli"])
            return _start_windows_update(arguments)
        _run(arguments)
    else:
        target = "novacode-cli"
        arguments = ["--upgrade"]
        if installation.kind == "git":
            target += f" @ git+{REPO_URL}@main"
            arguments.append("--force-reinstall")
        _run([*_pip_command(), *arguments, target])
    with suppress(OSError):
        _cache_path().unlink(missing_ok=True)
    return None


def _pip_command() -> list[str]:
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    _fail("Install pip in this environment or install uv to update Nova.")


def update_main(arguments: list[str]) -> int:
    """Handle nova update independently of onboarding and model credentials."""
    parser = argparse.ArgumentParser(
        prog="nova update", description="Update the running Nova installation"
    )
    parser.add_argument("--check", action="store_true", help="Check without installing")
    args = parser.parse_args(arguments)
    try:
        if args.check:
            status = check_for_update(force=True)
            if status.error:
                print(f"Could not check for updates: {status.error}", file=sys.stderr)
                return 1
            print(
                f"Update available: {status.current[:12]} → {status.latest[:12]}. Run nova update."
                if status.available
                else "NovaCode is up to date."
            )
        else:
            status = check_for_update(force=True)
            if not status.error and not status.available:
                print("NovaCode is up to date.")
                return 0
            log = install_update()
            if log is not None:
                print(
                    "Update handed off; it starts after this launcher exits. "
                    f"Progress and result: {log}"
                )
            else:
                print("Nova updated. Restart Nova to use the new code.")
        return 0  # noqa: TRY300
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
