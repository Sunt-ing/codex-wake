import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_wake.lifecycle import _ancestor_named, hook_target


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        ancestor = patch(
            "codex_wake.lifecycle._windows_codex_ancestor", return_value=os.getpid()
        )
        ancestor.start()
        self.addCleanup(ancestor.stop)

    def test_hook_captures_thread_process_and_homes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_HOME": str(root / "codex-home"),
                        "CODEX_SQLITE_HOME": str(root / "sqlite-home"),
                    },
                    clear=False,
                ),
                patch("codex_wake.lifecycle.os.getppid", return_value=os.getpid()),
            ):
                target = hook_target(
                    {
                        "session_id": "01a00000-0000-7000-8000-000000000001",
                        "source": "cli",
                    },
                    codex_bin=root / "codex",
                )
            self.assertEqual(
                target["thread_id"], "01a00000-0000-7000-8000-000000000001"
            )
            self.assertEqual(target["surface"], "codex")
            self.assertEqual(target["codex_home"], root / "codex-home")
            self.assertEqual(target["sqlite_home"], root / "sqlite-home")
            self.assertTrue(target["codex_bin"].is_absolute())

    def test_hook_prefers_recorded_codex_binary_and_thread_source(self):
        target = hook_target(
            {
                "session_id": "01a00000-0000-7000-8000-000000000001",
                "source": "startup",
                "thread_source": "vscode",
            },
            codex_bin=Path("/opt/codex/bin/codex"),
        )
        self.assertEqual(target["surface"], "vscode")
        self.assertEqual(target["codex_bin"], Path("/opt/codex/bin/codex"))

    def test_windows_hook_finds_codex_beyond_the_transient_shell(self):
        self.assertEqual(
            _ancestor_named(
                30,
                {
                    30: (20, "cmd.exe"),
                    20: (10, "codex-x86_64-pc-windows-msvc.exe"),
                    10: (0, "cmd.exe"),
                },
                "codex-x86_64-pc-windows-msvc.exe",
            ),
            20,
        )

    def test_windows_hook_rejects_a_missing_codex_ancestor(self):
        with (
            patch("codex_wake.lifecycle.sys.platform", "win32"),
            patch("codex_wake.lifecycle.os.getppid", return_value=30),
            patch("codex_wake.lifecycle._windows_codex_ancestor", return_value=None),
            self.assertRaisesRegex(ValueError, "identify the Codex process"),
        ):
            hook_target(
                {"session_id": "thread-1"},
                codex_bin=Path("C:/codex.exe"),
                codex_home=Path("C:/codex-home"),
                sqlite_home=Path("C:/sqlite-home"),
            )


if __name__ == "__main__":
    unittest.main()
