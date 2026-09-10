import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_wake import __version__
from codex_wake.core import Store, process_token
from codex_wake.service import (
    LAUNCH_AGENT_LABEL,
    _bootstrap_macos_service,
    _stop_windows_runtime,
    _runtime,
    install_linux_service,
    install_macos_service,
    launch_agent,
    run_daemon,
    systemd_unit,
    uninstall_macos_service,
    windows_startup_script,
)


class ServiceTest(unittest.TestCase):
    def test_one_daemon_cycle_updates_and_releases_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Store(Path(temporary)) as store:
                run_daemon(store, interval_seconds=0, max_cycles=1)
                self.assertEqual(
                    store.runtime_status(), {"running": False, "healthy": False}
                )

    def test_systemd_unit_uses_current_python_and_state(self):
        python = Path("/opt/codex wake/python").absolute()
        state = Path("/tmp/codex wake").absolute()
        unit = systemd_unit(state, python)
        escaped_python = str(python).replace("\\", "\\\\")
        escaped_state = str(state).replace("\\", "\\\\")
        self.assertIn(f'ExecStart="{escaped_python}"', unit)
        self.assertIn(f'--state-dir "{escaped_state}" daemon', unit)
        self.assertIn("Restart=on-failure", unit)

    def test_macos_launch_agent_uses_current_python_and_state(self):
        agent = plistlib.loads(
            launch_agent(Path("/tmp/codex wake"), Path("/opt/codex wake/python"))
        )
        self.assertEqual(agent["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            agent["ProgramArguments"][0], str(Path("/opt/codex wake/python").absolute())
        )
        self.assertEqual(
            agent["ProgramArguments"][4], str(Path("/tmp/codex wake").absolute())
        )
        self.assertTrue(agent["KeepAlive"])

    def test_runtime_probe_tolerates_database_schema_being_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            connection = sqlite3.connect(state / "state.sqlite3")
            connection.close()
            self.assertEqual(_runtime(state), {})

    def test_macos_launch_agent_preserves_cli_path_and_records_logs(self):
        state = Path("/tmp/café state").absolute()
        with patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}):
            agent = plistlib.loads(launch_agent(state))
        self.assertEqual(
            agent["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin:/bin"
        )
        self.assertEqual(agent["StandardErrorPath"], str(state / "daemon.stderr.log"))
        self.assertEqual(agent["StandardOutPath"], str(state / "daemon.stdout.log"))

    def test_macos_bootstrap_retries_only_transient_eio(self):
        results = [
            subprocess.CompletedProcess([], 5, "", "Input/output error"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with (
            patch("codex_wake.service.subprocess.run", side_effect=results) as run,
            patch("codex_wake.service.time.sleep"),
        ):
            _bootstrap_macos_service("gui/501", Path("agent.plist"), 1)
        self.assertEqual(run.call_count, 2)

    def test_macos_bootstrap_error_is_bounded_and_actionable(self):
        for code in (5, 1):
            with (
                self.subTest(code=code),
                patch(
                    "codex_wake.service.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], code, "", "bootstrap failed"
                    ),
                ) as run,
                self.assertRaisesRegex(RuntimeError, f"bootstrap failed.*exit {code}"),
            ):
                _bootstrap_macos_service("gui/501", Path("agent.plist"), 0)
            self.assertEqual(run.call_count, 1)

    def test_macos_failed_first_install_removes_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Path(temporary) / "agent.plist"
            with (
                patch("codex_wake.service.launch_agent_path", return_value=agent),
                patch("codex_wake.service.os.getuid", return_value=501, create=True),
                patch("codex_wake.service.subprocess.run"),
                patch("codex_wake.service._bootstrap_macos_service"),
                patch(
                    "codex_wake.service._wait_for_runtime",
                    side_effect=RuntimeError("startup failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "startup failed; daemon log:"),
            ):
                install_macos_service(Path(temporary) / "new state")
            self.assertFalse(agent.exists())

    def test_macos_failed_update_restores_previous_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Path(temporary) / "agent.plist"
            previous = launch_agent(Path(temporary) / "old state")
            agent.write_bytes(previous)
            with (
                patch("codex_wake.service.launch_agent_path", return_value=agent),
                patch("codex_wake.service.os.getuid", return_value=501, create=True),
                patch("codex_wake.service.subprocess.run"),
                patch("codex_wake.service._bootstrap_macos_service") as bootstrap,
                patch(
                    "codex_wake.service._wait_for_runtime",
                    side_effect=RuntimeError("startup failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "startup failed"),
            ):
                install_macos_service(Path(temporary) / "new state")
            self.assertEqual(agent.read_bytes(), previous)
            self.assertEqual(bootstrap.call_count, 2)

    def test_macos_uninstall_retains_definition_if_loaded_service_cannot_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Path(temporary) / "agent.plist"
            agent.write_bytes(b"previous definition")
            with (
                patch("codex_wake.service.launch_agent_path", return_value=agent),
                patch("codex_wake.service.os.getuid", return_value=501, create=True),
                patch(
                    "codex_wake.service.subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess([], 1, "", "permission denied"),
                        subprocess.CompletedProcess([], 0, "loaded", ""),
                    ],
                ),
                self.assertRaisesRegex(
                    RuntimeError, "could not stop.*permission denied"
                ),
            ):
                uninstall_macos_service()
            self.assertEqual(agent.read_bytes(), b"previous definition")

    def test_windows_startup_uses_current_python_and_state(self):
        script = windows_startup_script(
            Path("/tmp/Codex Wake/state"), Path("/opt/Codex Wake/python.exe")
        )
        self.assertIn(
            subprocess.list2cmdline(
                [str(Path("/opt/Codex Wake/python.exe").absolute())]
            ),
            script,
        )
        self.assertIn(
            subprocess.list2cmdline([str(Path("/tmp/Codex Wake/state").absolute())]),
            script,
        )

    def test_windows_stop_clears_runtime_without_printing_taskkill(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            token = process_token(process.pid)
            with Store(state) as store:
                store.runtime_start(process.pid, token, __version__)

            def taskkill(*_, **__):
                process.terminate()
                process.wait(timeout=5)

            with patch("codex_wake.service.subprocess") as commands:
                run = commands.run
                run.side_effect = taskkill
                _stop_windows_runtime(state)
            self.assertTrue(run.call_args.kwargs["capture_output"])
            with Store(state, read_only=True) as store:
                self.assertEqual(
                    store.runtime_status(), {"running": False, "healthy": False}
                )

    def test_install_waits_for_exact_live_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Store(root / "state") as store:
                token = process_token(os.getpid())
                store.runtime_start(os.getpid(), token, __version__)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            try:

                def restart(command, **_):
                    if command[-2:] == ["restart", "codex-wake.service"]:
                        with Store(root / "state") as store:
                            store.runtime_stop(os.getpid(), token)
                            store.runtime_start(
                                process.pid, process_token(process.pid), __version__
                            )

                with (
                    patch(
                        "codex_wake.service.systemd_user_dir",
                        return_value=root / "systemd",
                    ),
                    patch("codex_wake.service.subprocess") as commands,
                ):
                    commands.run.side_effect = restart
                    unit = install_linux_service(root / "state", startup_timeout=0.2)
            finally:
                process.terminate()
                process.wait(timeout=5)
            self.assertTrue(unit.is_file())

    def test_install_restores_unit_when_restart_does_not_replace_daemon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            unit = unit_dir / "codex-wake.service"
            unit.write_text("old unit\n")
            with Store(root / "state") as store:
                token = process_token(os.getpid())
                store.runtime_start(os.getpid(), token, __version__)
            with (
                patch("codex_wake.service.systemd_user_dir", return_value=unit_dir),
                patch("codex_wake.service.subprocess"),
                self.assertRaisesRegex(RuntimeError, "new process"),
            ):
                install_linux_service(root / "state", startup_timeout=0.01)
            self.assertEqual(unit.read_text(), "old unit\n")


if __name__ == "__main__":
    unittest.main()
