"""Real detached process-tree lifecycle, runnable without pytest fixtures."""

import os
import runpy
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from novacode_cli.daemons import registry


class DaemonRuntimeTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux zombie process state")
    def test_exited_unreaped_child_is_not_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            state = ""
            for _ in range(100):
                stat = Path(f"/proc/{child.pid}/stat").read_text()
                state = stat[stat.rindex(")") + 2 :].split()[0]
                if state == "Z":
                    break
                time.sleep(0.01)
            self.assertEqual(state, "Z", "Child did not reach the unreaped state")
            self.assertFalse(registry.is_alive(child.pid))
        finally:
            child.wait(timeout=5)

    def test_daemon_survives_launcher_and_stops_its_children(self):
        source = Path(__file__).resolve().parents[1]
        tool_path = source / "novacode_cli/tools/daemon_tool.py"
        # Load the real tool without importing unrelated optional tools from
        # tools.__init__. LangChain still supplies the real tool schema/invoke.
        tool = runpy.run_path(str(tool_path))["daemon"]
        previous = registry.DAEMONS_DIR, registry.REGISTRY_PATH
        owned = {}
        with tempfile.TemporaryDirectory(prefix="nova daemon ") as folder:
            root = Path(folder)
            registry.DAEMONS_DIR = root
            registry.REGISTRY_PATH = root / "registry.json"
            worker = root / "worker with spaces.py"
            worker.write_text(
                "import os, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "print('READY', os.getpid(), child.pid, os.getcwd(), flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            argv = [sys.executable, "-u", str(worker)]
            command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            launch_args = {"action": "start", "name": "audit", "command": command, "cwd": str(root)}
            launcher = (
                f"import sys, runpy; sys.path.insert(0, {str(source)!r}); "
                "from pathlib import Path; from novacode_cli.daemons import registry; "
                f"registry.DAEMONS_DIR=Path({str(root)!r}); "
                f"registry.REGISTRY_PATH=Path({str(registry.REGISTRY_PATH)!r}); "
                f"tool=runpy.run_path({str(tool_path)!r})['daemon']; "
                f"print(tool.invoke({launch_args!r}))"
            )
            # The launcher's exit is deliberate: the daemon must stay alive.
            try:
                launched = subprocess.run(
                    [sys.executable, "-c", launcher], capture_output=True, text=True, timeout=15
                )
                self.assertEqual(launched.returncode, 0, launched.stderr)
                self.assertIn("Started daemon", launched.stdout)
                info = registry.get("audit")
                self.assertIsNotNone(info)
                owned[info.pid] = info.create_time
                log = ""
                for _ in range(100):
                    log = tool.invoke({"action": "logs", "name": "audit"})
                    if "READY " in log:
                        break
                    time.sleep(0.05)
                self.assertIn("READY ", log)
                line = next(line for line in log.splitlines() if line.startswith("READY "))
                _, parent, child, cwd = line.split(" ", 3)
                self.assertEqual(cwd, str(root))
                for pid in (int(parent), int(child)):
                    owned[pid] = registry.process_created_at(pid)
                self.assertIn("running", tool.invoke({"action": "status", "name": "audit"}))
                self.assertIn("audit", tool.invoke({"action": "list"}))
                result = tool.invoke({"action": "stop", "name": "audit"})
                self.assertIn("Stopped daemon", result)
                for _ in range(50):
                    if not any(registry.is_alive(pid) for pid in owned):
                        break
                    time.sleep(0.05)
                self.assertFalse(
                    any(registry.is_alive(pid) for pid in owned), "Daemon left a live descendant"
                )
                self.assertIsNone(registry.get("audit"))
            finally:
                # Clean only verified processes created by this test, even
                # when a failed stop leaves the shell or a child behind.
                for pid, created in owned.items():
                    if (
                        created
                        and registry.process_created_at(pid) == created
                        and registry.is_alive(pid)
                    ):
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(pid), "/T", "/F"],
                                capture_output=True,
                                timeout=10,
                            )
                        else:
                            os.kill(pid, 9)
                registry.DAEMONS_DIR, registry.REGISTRY_PATH = previous
