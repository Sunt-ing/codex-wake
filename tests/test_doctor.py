import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_wake import __version__
from codex_wake.core import AdapterError, Store, poll_due, process_token
from codex_wake.doctor import doctor


class DoctorTest(unittest.TestCase):
    def test_read_only_store_does_not_create_missing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "missing"
            with self.assertRaisesRegex(ValueError, "not installed"):
                Store(state, read_only=True)
            self.assertFalse(state.exists())

    def test_doctor_verifies_runtime_and_recorded_plugin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            manifest = release / "plugins/codex-wake/.codex-plugin/plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"version": "0.1.0+codex.test"}))
            with Store(root / "state") as store:
                token = process_token(os.getpid())
                store.runtime_start(os.getpid(), token, __version__)
                store.record_installation(
                    codex_home=root / "codex-home",
                    sqlite_home=root / "sqlite-home",
                    codex_bin=Path("/bin/codex"),
                    marketplace_name="codex-wake-local",
                    plugin_version="0.1.0+codex.test",
                    release_dir=release,
                )
                listing = json.dumps(
                    {
                        "installed": [
                            {
                                "name": "codex-wake",
                                "marketplaceName": "codex-wake-local",
                                "version": "0.1.0+codex.test",
                                "installed": True,
                            }
                        ]
                    }
                )
                with patch("codex_wake.doctor.subprocess") as commands:
                    run = commands.run
                    run.return_value.returncode = 0
                    run.return_value.stdout = listing
                    result = doctor(store)
                store.runtime_stop(os.getpid(), token)
            self.assertTrue(result["healthy"])
            self.assertEqual(
                [check["status"] for check in result["checks"]],
                ["PASS", "PASS", "PASS", "WARN", "PASS", "PASS"],
            )

    def test_polling_warning_reports_failure_and_clears_after_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Store(root) as store:
                store.register_target(
                    thread_id="test",
                    surface="cli",
                    codex_bin=Path("codex"),
                    codex_home=root,
                    sqlite_home=root,
                    pid=os.getpid(),
                    token=process_token(os.getpid()),
                )
                registration, _ = store.register(
                    source="test-source",
                    subject="job",
                    thread_id="test",
                    adapter_command=["check-job"],
                )
                adapter = Mock()
                adapter.check.side_effect = AdapterError("query timed out")
                poll_due(store, adapter)

                def polling():
                    with (
                        Store(root, read_only=True) as reader,
                        patch("codex_wake.doctor.QueueClient.probe"),
                    ):
                        return [
                            c
                            for c in doctor(reader)["checks"]
                            if c["name"] == "polling"
                        ]

                warning = polling()[0]
                self.assertEqual(warning["status"], "WARN")
                self.assertIn(registration["id"], warning["detail"])
                self.assertIn("test-source", warning["detail"])
                self.assertIn("query timed out", warning["detail"])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT last_check_error FROM registrations"
                    ).fetchone()[0],
                    "query timed out",
                )
                # Bring the failed check due without waiting for the retry delay.
                store.connection.execute(
                    "UPDATE registrations SET next_check_at_ms = 0"
                )
                store.connection.commit()
                adapter.check.side_effect = None
                adapter.check.return_value = {"state": "pending"}
                poll_due(store, adapter)
                self.assertEqual(polling()[0]["status"], "PASS")
                store.mark_check_error(registration["id"], "another failure", 0)
                store.discard_registration(registration["id"], "no longer needed")
                self.assertEqual(polling()[0]["status"], "PASS")

    def test_doctor_reports_database_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            (state / "state.sqlite3").write_bytes(b"not sqlite")
            with Store(state, read_only=True) as store:
                result = doctor(store)
            self.assertFalse(result["healthy"])
            self.assertEqual(result["checks"][0]["status"], "FAIL")
            self.assertIn("database", result["checks"][0]["name"])


if __name__ == "__main__":
    unittest.main()
