import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_wake.cli import lifecycle_target, output_doctor, parser, registration_spec
from codex_wake.core import Store


class CliTest(unittest.TestCase):
    def test_builtin_registration_flags_build_adapter_config(self):
        args = parser().parse_args(
            [
                "register",
                "github-actions",
                "--repository",
                "owner/repo",
                "--run-id",
                "123",
                "--thread",
                "thread-1",
                "--message",
                "Check the result and continue.",
            ]
        )
        source, subject, command, config = registration_spec(args)
        self.assertEqual(args.message, "Check the result and continue.")
        self.assertEqual(source, "github-actions")
        self.assertEqual(subject, "owner/repo Actions run 123")
        self.assertEqual(command[-2:], ["adapter-check", "github-actions"])
        self.assertEqual(config, {"repository": "owner/repo", "run_id": "123"})

    def test_doctor_text_is_one_check_per_line(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            output_doctor(
                {
                    "healthy": False,
                    "checks": [
                        {"status": "PASS", "name": "database", "detail": "ok"},
                        {"status": "FAIL", "name": "daemon", "detail": "stopped"},
                    ],
                },
                False,
            )
        self.assertEqual(stream.getvalue(), "PASS database: ok\nFAIL daemon: stopped\n")

    def test_status_command_keeps_register_dispatch_and_strips_separator(self):
        args = parser().parse_args(
            [
                "register",
                "status-command",
                "--thread",
                "thread-1",
                "--",
                "probe",
                "--id",
                "1",
            ]
        )
        self.assertEqual(args.command, "register")
        self.assertEqual(
            registration_spec(args)[3], {"command": ["probe", "--id", "1"]}
        )

    def test_lifecycle_target_uses_binary_recorded_for_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            with Store(root / "state") as store:
                store.record_installation(
                    codex_home=codex_home,
                    sqlite_home=root / "sqlite-home",
                    codex_bin=root / "codex-bin",
                    marketplace_name="codex-wake-local",
                    plugin_version="test",
                    release_dir=root / "release",
                )
                with (
                    patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}),
                    patch(
                        "codex_wake.lifecycle._windows_codex_ancestor",
                        return_value=os.getpid(),
                    ),
                ):
                    target = lifecycle_target(store, {"session_id": "thread-1"})
            self.assertEqual(target["codex_bin"], root / "codex-bin")


if __name__ == "__main__":
    unittest.main()
