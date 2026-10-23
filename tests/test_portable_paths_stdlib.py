"""Real filesystem boundary checks runnable on Linux without the full app stack.

Run: python -m unittest discover -s tests -p test_portable_paths_stdlib.py
"""

import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from novacode_cli.security.paths import resolve_beneath
from novacode_cli.utils.backend_paths import virtual_to_real_path

# integrations.__init__ imports optional backend SDKs. Load this standalone
# helper directly so the Linux probe also works without those dependencies.
host_path_to_virtual = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "novacode_cli/integrations/host_path.py")
)["host_path_to_virtual"]


class PortablePathTests(unittest.TestCase):
    def test_rejects_escapes_and_foreign_anchors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for key in (
                "/../outside",
                "/x\\..\\..\\outside",
                "/C:/outside",
                "//host/share",
                "/\\host\\share",
                "/bad\x00name",
            ):
                with self.subTest(key=key):
                    self.assertIsNone(virtual_to_real_path(key, workspace_root=root))

    def test_permitted_nested_and_dotted_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for key in ("/src/file.py", "/version..txt", "/a\\b.txt"):
                with self.subTest(key=key):
                    result = resolve_beneath(root, key)
                    self.assertTrue(result.is_relative_to(root))
                    self.assertEqual(result, virtual_to_real_path(key, workspace_root=root))

    def test_actual_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "root"
            outside = Path(folder) / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation unavailable")
            self.assertIsNone(virtual_to_real_path("/link/secret", workspace_root=root))
            with self.assertRaises(ValueError):
                resolve_beneath(root, "/link/secret")

    def test_mounts_and_backend_denials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            sub = SimpleNamespace(root_dir=root / "memory", virtual_mode=True)
            backend = SimpleNamespace(
                default=SimpleNamespace(root_dir=root), routes={"/memories/": sub}
            )
            self.assertEqual(
                virtual_to_real_path("/memories/topic.md", backend=backend),
                root / "memory/topic.md",
            )
            self.assertIsNone(virtual_to_real_path("/memories/../secret", backend=backend))

            def reject(key):
                raise ValueError("Denied by backend")

            sub._resolve_path = reject
            self.assertIsNone(virtual_to_real_path("/memories/topic.md", backend=backend))

    def test_host_rewriting_preserves_destination(self):
        for root in ("/home/project", "C:/project"):
            with self.subTest(root=root):
                self.assertEqual(host_path_to_virtual(root + "/a.py", root), "/a.py")
                bad = root + "/../external/a.py"
                self.assertEqual(host_path_to_virtual(bad, root), bad)
