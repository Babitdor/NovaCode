"""Restrictive atomic storage for credential-bearing JSON."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def write_secret_json(path: Path, data: Any) -> None:
    """Restrict the replacement file before writing any secret bytes."""
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
    temporary = Path(name)
    try:
        if os.name == "nt":
            executable = (
                Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "icacls.exe"
            )
            identity = subprocess.run(
                [str(executable.with_name("whoami.exe")), "/user", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            sid = next(csv.reader([identity.stdout.strip()]))[1]
            if not sid.startswith("S-1-") or any(c not in "S0123456789-" for c in sid):
                raise OSError("Could not determine credential file owner")
            result = subprocess.run(
                [
                    str(executable),
                    str(temporary),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:(F)",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if result.returncode:
                raise OSError("Could not restrict credential file access")
        else:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
