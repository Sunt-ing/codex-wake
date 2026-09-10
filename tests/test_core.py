import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_wake.core import (
    QueueClient,
    QueueError,
    Store,
    deliver_due,
    poll_due,
    process_token,
    work_once,
)

FAKE_ADAPTER = """#!/usr/bin/env python3
import json
import sys

request = json.load(sys.stdin)
config = request["config"]
if config.get("fail"):
    print("probe failed", file=sys.stderr)
    raise SystemExit(3)
print(json.dumps(config["result"]))
"""


class CoreTest(unittest.TestCase):
    def test_daemon_closes_exited_target_without_registrations(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            target = self.target(pid=process.pid)
        finally:
            process.terminate()
            process.wait(timeout=10)
        work_once(self.store)
        self.assertEqual(self.store.target(target["id"])["state"], "closed")
        self.assertEqual(self.store.active_targets(), [])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake_codex = Path(sys.executable)
        self.fake_adapter = self.root / "adapter.py"
        self.fake_adapter.write_text(FAKE_ADAPTER)
        self.store = Store(self.root / "state")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def target(self, pid=None):
        pid = pid or os.getpid()
        return self.store.register_target(
            thread_id="01a00000-0000-7000-8000-000000000001",
            surface="cli",
            codex_bin=self.fake_codex,
            codex_home=self.root / "codex-home",
            sqlite_home=self.root / "sqlite-home",
            pid=pid,
            token=process_token(pid),
        )

    def test_register_emit_deliver_and_deduplicate(self):
        self.target()
        registration, created = self.store.register(
            source="test",
            subject="job-1",
            thread_id="01a00000-0000-7000-8000-000000000001",
        )
        self.assertTrue(created)
        duplicate, created = self.store.register(
            source="test",
            subject="job-1",
            thread_id="01a00000-0000-7000-8000-000000000001",
        )
        self.assertFalse(created)
        self.assertEqual(registration["id"], duplicate["id"])

        _, created = self.store.emit(
            registration_id=registration["id"],
            event_id="test:job-1:done",
            message="job-1 finished",
        )
        self.assertTrue(created)
        _, created = self.store.emit(
            registration_id=registration["id"],
            event_id="test:job-1:done",
            message="job-1 finished",
        )
        self.assertFalse(created)

        queue = Mock(spec=QueueClient)
        self.assertEqual(
            deliver_due(self.store, queue),
            {"delivered": 1, "discarded": 0, "retried": 0},
        )
        event = self.store.event("test:job-1:done")
        self.assertEqual(event["state"], "delivered")
        call = queue.deliver.call_args.args[0]
        self.assertEqual(call["thread_id"], "01a00000-0000-7000-8000-000000000001")
        self.assertEqual(call["codex_home"], str(self.root / "codex-home"))
        self.assertEqual(call["sqlite_home"], str(self.root / "sqlite-home"))
        _, created = self.store.emit(
            registration_id=registration["id"],
            event_id="test:job-1:done",
            message="job-1 finished",
        )
        self.assertFalse(created)

    def test_event_collision_does_not_block_other_deliveries(self):
        thread = self.target()["thread_id"]
        original, _ = self.store.register(
            source="custom", subject="original", thread_id=thread
        )
        self.store.emit(
            registration_id=original["id"], event_id="shared:done", message="original"
        )
        registrations = []
        for subject, event_id in (
            ("collision", "shared:done"),
            ("other", "other:done"),
        ):
            registration, _ = self.store.register(
                source="custom",
                subject=subject,
                thread_id=thread,
                adapter_command=[sys.executable, str(self.fake_adapter)],
                adapter_config={
                    "result": {
                        "state": "terminal",
                        "event_id": event_id,
                        "message": subject,
                    }
                },
            )
            registrations.append(registration)
        collision = registrations[0]
        queue = Mock(spec=QueueClient)
        with patch("codex_wake.core.QueueClient", return_value=queue):
            result = work_once(self.store)
            self.assertEqual(result["poll"]["failed"], 1)
            self.assertEqual(result["poll"]["terminal"], 1)
            self.assertEqual(result["delivery"]["delivered"], 2)
            row = self.store.connection.execute(
                "SELECT * FROM registrations WHERE id = ?", (collision["id"],)
            ).fetchone()
            self.assertEqual(row["state"], "active")
            self.assertEqual(row["check_attempts"], 1)
            self.assertIn("event ID collision: shared:done", row["last_check_error"])
            self.assertGreater(row["next_check_at_ms"], row["updated_at_ms"])
            self.assertEqual(work_once(self.store)["poll"]["failed"], 0)
            # Persisted conflicts must remain isolated after restart and retry.
            retry_at = row["next_check_at_ms"] + 1
            with (
                Store(self.root / "state") as reopened,
                patch("codex_wake.core.now_ms", return_value=retry_at),
            ):
                result = work_once(reopened)
                self.assertEqual(result["poll"]["failed"], 1)
                self.assertEqual(result["delivery"]["delivered"], 0)
        self.assertEqual(queue.deliver.call_count, 2)
        original_event = self.store.event("shared:done")
        self.assertEqual(original_event["registration_id"], original["id"])
        self.assertEqual(original_event["message"], "original")
        self.assertEqual(original_event["state"], "delivered")
        self.assertEqual(original_event["attempts"], 1)
        self.assertEqual(self.store.event("other:done")["state"], "delivered")

    def test_malformed_adapter_output_does_not_block_other_deliveries(self):
        thread = self.target()["thread_id"]
        broken = []
        for subject, command in (
            ("state-list", [sys.executable, "-c", "print('{\"state\": []}')"]),
            (
                "invalid-bytes",
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(bytes([255]))",
                ],
            ),
            ("nul-command", [sys.executable, "bad\0argument"]),
        ):
            registration, _ = self.store.register(
                source="custom",
                subject=subject,
                thread_id=thread,
                adapter_command=command,
            )
            broken.append(registration["id"])
        ready, _ = self.store.register(source="test", subject="ready", thread_id=thread)
        self.store.emit(
            registration_id=ready["id"], event_id="ready:done", message="done"
        )
        queue = Mock(spec=QueueClient)
        with patch("codex_wake.core.QueueClient", return_value=queue):
            result = work_once(self.store)
        self.assertEqual(result["poll"]["failed"], 3)
        self.assertEqual(result["delivery"]["delivered"], 1)
        for registration_id in broken:
            row = self.store.connection.execute(
                "SELECT * FROM registrations WHERE id = ?", (registration_id,)
            ).fetchone()
            self.assertEqual(row["state"], "active")
            self.assertEqual(row["check_attempts"], 1)
            self.assertTrue(row["last_check_error"])

    def test_queue_invalid_output_is_retried(self):
        thread = self.target()["thread_id"]
        registration, _ = self.store.register(
            source="test", subject="queue", thread_id=thread
        )
        self.store.emit(
            registration_id=registration["id"], event_id="queue:done", message="done"
        )
        error = UnicodeDecodeError("utf-8", bytes([255]), 0, 1, "invalid start byte")
        with patch("codex_wake.core.QueueClient._run", wraps=QueueClient()._run):
            with (
                patch("codex_wake.core.subprocess.run", side_effect=error),
                patch("codex_wake.core.process_is_alive", return_value=True),
            ):
                result = deliver_due(self.store)
        self.assertEqual(result["retried"], 1)
        self.assertEqual(self.store.event("queue:done")["state"], "pending")

    def test_dead_target_discards_instead_of_delivering(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            self.target(process.pid)
            registration, _ = self.store.register(
                source="test",
                subject="job-2",
                thread_id="01a00000-0000-7000-8000-000000000001",
            )
            self.store.emit(
                registration_id=registration["id"],
                event_id="test:job-2:done",
                message="job-2 finished",
            )
        finally:
            process.terminate()
            process.wait(timeout=5)

        self.assertEqual(
            deliver_due(self.store), {"delivered": 0, "discarded": 1, "retried": 0}
        )
        self.assertEqual(self.store.event("test:job-2:done")["state"], "discarded")

    def test_registration_rejects_changed_command_or_config(self):
        self.target()
        spec = dict(
            source="test",
            subject="job-1",
            thread_id="01a00000-0000-7000-8000-000000000001",
            adapter_command=["probe", "1"],
            adapter_config={"host": "a", "id": 1},
        )
        first, _ = self.store.register(**spec)
        duplicate, created = self.store.register(
            **{**spec, "adapter_config": {"id": 1, "host": "a"}}
        )
        self.assertFalse(created)
        self.assertEqual(first["id"], duplicate["id"])
        for changed in (
            {"adapter_command": ["probe", "2"]},
            {"adapter_command": None},
            {"adapter_config": {"host": "b", "id": 1}},
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "different --subject"):
                    self.store.register(**{**spec, **changed})
        self.assertEqual(self.store.register(**spec)[0], first)

    def test_repeated_session_start_refreshes_without_dropping_registration(self):
        first = self.target()
        registration, _ = self.store.register(
            source="test",
            subject="job-refresh",
            thread_id="01a00000-0000-7000-8000-000000000001",
        )
        refreshed = self.target()
        self.assertEqual(refreshed["id"], first["id"])
        row = self.store.connection.execute(
            "SELECT state FROM registrations WHERE id = ?", (registration["id"],)
        ).fetchone()
        self.assertEqual(row["state"], "active")

    def test_core_polls_adapters_concurrently_and_delivers_terminal_event(self):
        self.target()
        thread = "01a00000-0000-7000-8000-000000000001"
        pending, _ = self.store.register(
            source="test",
            subject="pending",
            thread_id=thread,
            adapter_command=[sys.executable, str(self.fake_adapter)],
            adapter_config={"result": {"state": "pending"}},
        )
        terminal, _ = self.store.register(
            source="test",
            subject="terminal",
            thread_id=thread,
            adapter_command=[sys.executable, str(self.fake_adapter)],
            adapter_config={
                "result": {
                    "state": "terminal",
                    "event_id": "test:terminal:done",
                    "message": "terminal finished",
                }
            },
        )
        failing, _ = self.store.register(
            source="test",
            subject="failing",
            thread_id=thread,
            adapter_command=[sys.executable, str(self.fake_adapter)],
            adapter_config={"fail": True},
        )

        self.assertEqual(
            poll_due(self.store, interval_seconds=0),
            {"pending": 1, "terminal": 1, "discarded": 0, "failed": 1},
        )
        self.assertEqual(self.store.event("test:terminal:done")["state"], "pending")
        rows = {
            row["id"]: row
            for row in self.store.connection.execute("SELECT * FROM registrations")
        }
        self.assertEqual(rows[pending["id"]]["state"], "active")
        self.assertEqual(rows[terminal["id"]]["state"], "triggered")
        self.assertEqual(rows[failing["id"]]["check_attempts"], 1)

        self.assertEqual(
            deliver_due(self.store, Mock(spec=QueueClient)),
            {"delivered": 1, "discarded": 0, "retried": 0},
        )
        self.assertEqual(self.store.event("test:terminal:done")["state"], "delivered")

    def test_poll_backlog_yields_to_delivery_between_batches(self):
        thread = self.target()["thread_id"]
        for index in range(9):
            self.store.register(
                source="test",
                subject=f"backlog-{index}",
                thread_id=thread,
                adapter_command=[sys.executable, str(self.fake_adapter)],
                adapter_config={"result": {"state": "pending"}},
            )
        ready, _ = self.store.register(source="test", subject="ready", thread_id=thread)
        self.store.emit(
            registration_id=ready["id"], event_id="ready:done", message="done"
        )
        queue = Mock(spec=QueueClient)
        with patch("codex_wake.core.QueueClient", return_value=queue):
            first = work_once(self.store)
            self.assertEqual(first["poll"]["pending"], 4)
            self.assertEqual(first["delivery"]["delivered"], 1)
            self.assertEqual(len(self.store.due_registrations()), 5)
            self.assertEqual(work_once(self.store)["poll"]["pending"], 4)
            self.assertEqual(work_once(self.store)["poll"]["pending"], 1)
        queue.deliver.assert_called_once()

    def test_slow_queue_operations_refresh_runtime_between_calls(self):
        thread = self.target()["thread_id"]
        pid, token = os.getpid(), process_token(os.getpid())
        clock = [100_000]
        with patch("codex_wake.core.now_ms", side_effect=lambda: clock[0]):
            self.store.runtime_start(pid, token, "test")
            for index in range(5):
                registration, _ = self.store.register(
                    source="test",
                    subject=f"event-{index}",
                    thread_id=thread,
                )
                self.store.emit(
                    registration_id=registration["id"],
                    event_id=f"event:{index}",
                    message="done",
                )

            def slow_call(event):
                clock[0] += 14_000
                self.assertTrue(self.store.runtime_status()["healthy"])

            queue = Mock(spec=QueueClient)
            queue.probe.side_effect = slow_call
            queue.deliver.side_effect = slow_call
            result = deliver_due(
                self.store, queue, progress=lambda: self.store.heartbeat(pid, token)
            )
            self.assertEqual(result["delivered"], 4)
            self.assertEqual(len(self.store.due_events()), 1)

    def test_custom_wake_message_survives_registration_and_delivery(self):
        target = self.target()
        registration, _ = self.store.register(
            source="test",
            subject="custom",
            thread_id=target["thread_id"],
            wake_message="Inspect the logs and continue with the next experiment.",
        )
        self.store.emit(
            registration_id=registration["id"],
            event_id="custom-event",
            message="job-1: succeeded",
        )
        with Store(self.root / "state") as reopened:
            event = reopened.due_events()[0]
        with patch.object(QueueClient, "_run") as run:
            QueueClient().deliver(event)
        self.assertEqual(
            run.call_args.args[0][-1],
            "job-1: succeeded\n\nInspect the logs and continue with the next experiment.",
        )
        with self.assertRaises(ValueError):
            self.store.register(
                source="test",
                subject="custom",
                thread_id=target["thread_id"],
                wake_message="different",
            )

    def test_existing_database_gains_optional_message_without_losing_watches(self):
        target = self.target()
        registration, _ = self.store.register(
            source="test", subject="old", thread_id=target["thread_id"]
        )
        self.store.connection.execute(
            "ALTER TABLE registrations DROP COLUMN wake_message"
        )
        with Store(self.root / "state") as upgraded:
            row = upgraded.connection.execute(
                "SELECT * FROM registrations WHERE id=?", (registration["id"],)
            ).fetchone()
            self.assertEqual(row["state"], "active")
            self.assertIsNone(row["wake_message"])

    def test_queue_client_uses_exact_binary_thread_and_homes(self):
        target = {
            "codex_bin": str(self.fake_codex),
            "thread_id": "thread-1",
            "message": "finished",
            "codex_home": str(self.root / "codex-home"),
            "sqlite_home": str(self.root / "sqlite-home"),
        }
        result = Mock(
            returncode=0,
            stdout="Usage: codex queue [OPTIONS] --thread <THREAD> --message <TEXT>",
            stderr="",
        )
        with patch("codex_wake.core.subprocess.run", return_value=result) as run:
            QueueClient().probe(target)
            QueueClient().deliver(target)
        self.assertEqual(
            run.call_args.args[0],
            [
                str(self.fake_codex),
                "queue",
                "--thread",
                "thread-1",
                "--message",
                "finished\n\nIf necessary, check the result and decide what to do next.",
            ],
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["CODEX_HOME"], target["codex_home"]
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["CODEX_SQLITE_HOME"], target["sqlite_home"]
        )

    def test_queue_probe_rejects_root_help_from_older_codex(self):
        target = {
            "codex_bin": str(self.fake_codex),
            "codex_home": str(self.root / "codex-home"),
            "sqlite_home": str(self.root / "sqlite-home"),
        }
        result = Mock(returncode=0, stdout="Usage: codex [OPTIONS] [PROMPT]", stderr="")
        with (
            patch("codex_wake.core.subprocess.run", return_value=result),
            self.assertRaisesRegex(QueueError, "does not support"),
        ):
            QueueClient().probe(target)

    def test_macos_process_token_uses_process_start_time(self):
        result = Mock(returncode=0, stdout="Mon Aug 25 12:34:56 2026\n")
        with (
            patch("codex_wake.core.sys.platform", "darwin"),
            patch("codex_wake.core.subprocess.run", return_value=result) as run,
        ):
            self.assertEqual(process_token(42), "Mon Aug 25 12:34:56 2026")
        self.assertEqual(run.call_args.args[0], ["ps", "-o", "lstart=", "-p", "42"])


if __name__ == "__main__":
    unittest.main()
