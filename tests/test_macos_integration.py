"""Opt-in real launchd tests: CODEX_WAKE_MACOS_E2E=1 python -m unittest
 discover -s tests -p test_macos_integration.py -v

Install the package into the test interpreter first. Each test uses a unique
service label and temporary state/Codex homes; no production service is touched.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from codex_wake import service
from codex_wake.core import QueueClient, Store, process_token
from codex_wake.doctor import doctor
from codex_wake.installer import install_plugin, uninstall_plugins


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("CODEX_WAKE_MACOS_E2E") == "1",
    "requires macOS GUI login and CODEX_WAKE_MACOS_E2E=1",
)
class MacOSIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-wake-macos-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "paths with spaces café"
        self.root.mkdir()
        self.state = self.root / "state"
        self.label = f"com.sunting.codex-wake.test-{uuid.uuid4().hex}"
        self.agent = self.root / f"{self.label}.plist"
        for mock in (
            patch.object(service, "LAUNCH_AGENT_LABEL", self.label),
            patch.object(service, "launch_agent_path", return_value=self.agent),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        # Runs before removing the directory or restoring the production label.
        self.addCleanup(service.uninstall_macos_service)
        self.domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "print", self.domain], check=True, capture_output=True
        )
        # A clean child must import the installed wheel/editable package without
        # inheriting the test runner's source path or current working directory.
        environment = {**os.environ}
        environment.pop("PYTHONPATH", None)
        subprocess.run(
            [sys.executable, "-c", "import codex_wake"],
            cwd=self.root,
            env=environment,
            check=True,
        )

    def wait_until(self, predicate, detail, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        logs = self.state / "daemon.stderr.log"
        with Store(self.state, read_only=True) as store:
            rows = [
                dict(row)
                for row in store.connection.execute("SELECT * FROM registrations")
            ]
            events = store.events()
        self.fail(
            f"{detail}\n{logs.read_text() if logs.exists() else 'no daemon log'}\nregistrations={rows}\nevents={events}"
        )

    def runtime(self):
        with Store(self.state, read_only=True) as store:
            return store.runtime_status()

    def test_launchd_delivery_restart_rollback_and_uninstall(self):
        fixture_bin = self.root / "fixture bin"
        fixture_bin.mkdir()
        checker = fixture_bin / "wake-test-status"
        marker = self.root / "complete"
        checker.write_text(
            f"#!/bin/sh\n[ -f {shlex.quote(str(marker))} ] || exit 75\nprintf 'finished café\\n'\n"
        )
        checker.chmod(0o755)
        queue_log = self.root / "queue.jsonl"
        queue_script = self.root / "queue.py"
        queue_script.write_text(
            "import json, os, sys\n"
            "if sys.argv[1:] == ['queue', '--help']:\n"
            "    print('Usage: codex queue --thread <THREAD> --message <TEXT>')\n"
            "    raise SystemExit(0)\n"
            f"with open({str(queue_log)!r}, 'a') as output:\n"
            "    output.write(json.dumps({'args': sys.argv[1:], 'home': os.environ['CODEX_HOME'], "
            "'sqlite': os.environ['CODEX_SQLITE_HOME']}) + '\\n')\n"
        )
        queue = self.root / "fake-codex"
        queue.write_text(
            f'#!/bin/sh\nexec {shlex.join([sys.executable, str(queue_script)])} "$@"\n'
        )
        queue.chmod(0o755)
        with patch.dict(
            os.environ, {"PATH": str(fixture_bin) + os.pathsep + os.environ["PATH"]}
        ):
            service.install_macos_service(self.state)
        first = self.runtime()
        self.assertTrue(first["healthy"])
        with Store(self.state) as store:
            store.register_target(
                thread_id="macos-test-thread",
                surface="test",
                codex_bin=queue,
                codex_home=self.root / "codex home",
                sqlite_home=self.root / "sqlite home",
                pid=os.getpid(),
                token=process_token(os.getpid()),
            )
            registration, _ = store.register(
                source="status-command",
                subject="launchd PATH test",
                thread_id="macos-test-thread",
                adapter_command=[
                    sys.executable,
                    "-m",
                    "codex_wake",
                    "adapter-check",
                    "status-command",
                ],
                adapter_config={"command": ["wake-test-status"]},
            )

        def checked_pending():
            with Store(self.state, read_only=True) as store:
                row = store.connection.execute(
                    "SELECT * FROM registrations WHERE id = ?", (registration["id"],)
                ).fetchone()
                return (
                    row["updated_at_ms"] > row["created_at_ms"]
                    and row["last_check_error"] is None
                )

        self.wait_until(checked_pending, "daemon did not poll the PATH-only adapter")
        self.assertFalse(queue_log.exists())
        # Reinstall a disabled service and verify a different live daemon.
        subprocess.run(
            ["launchctl", "disable", f"{self.domain}/{self.label}"], check=True
        )
        with patch.dict(
            os.environ, {"PATH": str(fixture_bin) + os.pathsep + os.environ["PATH"]}
        ):
            service.install_macos_service(self.state)
        second = self.runtime()
        self.assertNotEqual(first["identity"], second["identity"])
        # Suspend only the test daemon: due work must survive an execution gap.
        # This is not a substitute for a physical macOS sleep/wake test.
        os.kill(second["pid"], signal.SIGSTOP)
        try:
            marker.touch()
            # Do not write SQLite while the daemon may be holding a transaction.
            # After resuming, its normal polling schedule picks up the marker.
            time.sleep(2)
            self.assertFalse(queue_log.exists())
        finally:
            os.kill(second["pid"], signal.SIGCONT)
        event_id = f"status-command:{registration['id']}:terminal"

        def delivered():
            with Store(self.state, read_only=True) as store:
                event = store.connection.execute(
                    "SELECT state FROM events WHERE event_id = ?", (event_id,)
                ).fetchone()
                return event and event["state"] == "delivered"

        self.wait_until(delivered, "terminal event was not delivered")
        queued = json.loads(queue_log.read_text().strip())
        self.assertEqual(queued["args"][:3], ["queue", "--thread", "macos-test-thread"])
        self.assertIn("finished café", queued["args"][-1])
        self.assertEqual(queued["home"], str(self.root / "codex home"))
        self.assertEqual(queued["sqlite"], str(self.root / "sqlite home"))
        # launchd must recover from a crash without replaying delivered events.
        os.kill(second["pid"], signal.SIGKILL)
        self.wait_until(
            lambda: (
                self.runtime().get("healthy")
                and self.runtime().get("identity") != second["identity"]
            ),
            "KeepAlive did not restart the daemon",
        )
        previous_agent = self.agent.read_bytes()
        broken_agent = plistlib.loads(previous_agent)
        broken_agent["ProgramArguments"] = ["/usr/bin/false"]
        with patch.object(
            service, "launch_agent", return_value=plistlib.dumps(broken_agent)
        ):
            with self.assertRaisesRegex(RuntimeError, "new process"):
                service.install_macos_service(self.state, startup_timeout=3)
        self.assertEqual(self.agent.read_bytes(), previous_agent)
        self.wait_until(
            lambda: self.runtime().get("healthy"), "rollback did not restore the daemon"
        )
        self.assertEqual(len(queue_log.read_text().splitlines()), 1)
        self.assertTrue(service.uninstall_macos_service())
        self.assertFalse(self.agent.exists())
        self.wait_until(
            lambda: not self.runtime()["running"], "uninstall left the daemon alive"
        )
        self.assertFalse(service.uninstall_macos_service())
        self.assertNotEqual(
            subprocess.run(
                ["launchctl", "print", f"{self.domain}/{self.label}"],
                capture_output=True,
            ).returncode,
            0,
        )

    def test_real_codex_plugin_install_update_doctor_uninstall(self):
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("real Codex binary is not installed")
        homes = {
            "codex_home": self.root / "codex home",
            "sqlite_home": self.root / "sqlite home",
        }
        QueueClient().probe(
            {"codex_bin": codex, **{key: str(value) for key, value in homes.items()}}
        )
        service.install_macos_service(self.state)
        with Store(self.state) as store:
            try:
                first = install_plugin(store, codex_bin=Path(codex), **homes)
                self.assertTrue(doctor(store)["healthy"])
                second = install_plugin(store, codex_bin=Path(codex), **homes)
                self.assertNotEqual(first["plugin_version"], second["plugin_version"])
                report = doctor(store)
                self.assertTrue(report["healthy"], report)
            finally:
                uninstall_plugins(store)
            self.assertEqual(store.installations(), [])
        result = subprocess.run(
            [codex, "plugin", "list", "--json"],
            env={
                **os.environ,
                "CODEX_HOME": str(homes["codex_home"]),
                "CODEX_SQLITE_HOME": str(homes["sqlite_home"]),
            },
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertFalse(
            any(
                item["name"] == "codex-wake"
                for item in json.loads(result.stdout).get("installed", [])
            )
        )

    @unittest.skipUnless(
        os.environ.get("CODEX_WAKE_CODEX_E2E") == "1",
        "requires CODEX_WAKE_CODEX_E2E=1 and the test extra",
    )
    def test_real_codex_hooks_and_queue_to_live_thread(self):
        from websockets.sync.client import unix_connect

        codex = shutil.which("codex")
        self.assertIsNotNone(codex, "install a Codex binary supporting queue")

        block_next_response = threading.Event()
        busy_started = threading.Event()
        release_busy = threading.Event()
        self.addCleanup(release_busy.set)

        class ResponseFixture(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if block_next_response.is_set():
                    block_next_response.clear()
                    busy_started.set()
                    release_busy.wait(timeout=20)
                # Finish each turn locally, without model inference or credentials.
                event = {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_wake_test",
                        "status": "completed",
                        "output": [],
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 0,
                            "total_tokens": 1,
                        },
                    },
                }
                body = ("data: " + json.dumps(event) + "\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), ResponseFixture)
        http_thread = threading.Thread(target=http.serve_forever, daemon=True)
        http_thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        # macOS Unix socket paths must fit in sockaddr_un; the default macOS
        # temporary directory plus Codex's socket suffix is too long.
        with tempfile.TemporaryDirectory(prefix="cw-", dir="/tmp") as temporary:
            home = Path(temporary) / "h"
            home.mkdir()
            (home / "config.toml").write_text(
                'model_provider="wake_test"\n[model_providers.wake_test]\n'
                'name="Wake test"\nwire_api="responses"\n'
                f'base_url="http://127.0.0.1:{http.server_port}/v1"\n'
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("CODEX_")
            }
            environment.update(CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(home))
            with (
                Store(self.state) as store,
                (self.root / "codex.log").open("w+") as log,
            ):
                install_plugin(
                    store, codex_bin=Path(codex), codex_home=home, sqlite_home=home
                )
                server = subprocess.Popen(
                    [codex, "app-server", "--listen", "unix://"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                )
                try:
                    socket = home / "app-server-control/app-server-control.sock"
                    self.wait_until(
                        socket.exists, "Codex did not create its control socket"
                    )
                    with unix_connect(
                        str(socket), compression=None, open_timeout=10
                    ) as ws:
                        notifications = []
                        sequence = 0

                        def rpc(method, params):
                            nonlocal sequence
                            sequence += 1
                            ws.send(
                                json.dumps(
                                    {"id": sequence, "method": method, "params": params}
                                )
                            )
                            deadline = time.monotonic() + 15
                            while True:
                                value = json.loads(
                                    ws.recv(
                                        timeout=max(0.01, deadline - time.monotonic())
                                    )
                                )
                                if value.get("id") == sequence:
                                    self.assertNotIn("error", value, value)
                                    return value["result"]
                                notifications.append(value)

                        def wait_notification(predicate):
                            deadline = time.monotonic() + 20
                            while True:
                                for value in notifications:
                                    if predicate(value):
                                        return value
                                notifications.append(
                                    json.loads(
                                        ws.recv(
                                            timeout=max(
                                                0.01, deadline - time.monotonic()
                                            )
                                        )
                                    )
                                )

                        rpc(
                            "initialize",
                            {
                                "clientInfo": {
                                    "name": "codex_wake_test",
                                    "version": "0.1.0",
                                },
                                "capabilities": {"experimentalApi": True},
                            },
                        )
                        hooks = rpc("hooks/list", {"cwds": [temporary]})["data"][0][
                            "hooks"
                        ]
                        self.assertEqual(len(hooks), 2)
                        # Trust only the two generated test hooks, using the same
                        # config API as Codex's /hooks UI, in this temporary home.
                        rpc(
                            "config/batchWrite",
                            {
                                "edits": [
                                    {
                                        "keyPath": "hooks.state",
                                        "value": {
                                            hook["key"]: {
                                                "trusted_hash": hook["currentHash"]
                                            }
                                            for hook in hooks
                                        },
                                        "mergeStrategy": "upsert",
                                    }
                                ],
                                "reloadUserConfig": True,
                            },
                        )
                        thread = rpc(
                            "thread/start",
                            {
                                "cwd": temporary,
                                "approvalPolicy": "never",
                                "sandbox": "read-only",
                            },
                        )["thread"]["id"]
                        turn = rpc(
                            "turn/start",
                            {
                                "threadId": thread,
                                "input": [
                                    {
                                        "type": "text",
                                        "text": "Initialize the transport test.",
                                    }
                                ],
                            },
                        )["turn"]["id"]
                        wait_notification(
                            lambda value: (
                                value.get("method") == "turn/completed"
                                and value["params"]["turn"]["id"] == turn
                            )
                        )
                        # A newer live thread must not steal the older thread's wake.
                        decoy = rpc(
                            "thread/start",
                            {
                                "cwd": temporary,
                                "approvalPolicy": "never",
                                "sandbox": "read-only",
                            },
                        )["thread"]["id"]
                        decoy_turn = rpc(
                            "turn/start",
                            {
                                "threadId": decoy,
                                "input": [
                                    {
                                        "type": "text",
                                        "text": "Initialize a second thread.",
                                    }
                                ],
                            },
                        )["turn"]["id"]
                        wait_notification(
                            lambda value: (
                                value.get("method") == "turn/completed"
                                and value["params"]["turn"]["id"] == decoy_turn
                            )
                        )
                        targets = store.active_targets()
                        self.assertEqual(
                            {target["thread_id"] for target in targets}, {thread, decoy}
                        )
                        self.assertTrue(
                            all(target["pid"] == server.pid for target in targets)
                        )
                        service.install_macos_service(self.state)
                        registration, _ = store.register(
                            source="status-command",
                            subject="real queue test",
                            thread_id=thread,
                            adapter_command=[
                                sys.executable,
                                "-m",
                                "codex_wake",
                                "adapter-check",
                                "status-command",
                            ],
                            adapter_config={
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "print('wake transport verified')",
                                ]
                            },
                        )
                        event_id = f"status-command:{registration['id']}:terminal"

                        def received(value):
                            if (
                                value.get("method") != "item/started"
                                or value["params"].get("threadId") != thread
                            ):
                                return False
                            item = value["params"].get("item", {})
                            return item.get(
                                "type"
                            ) == "userMessage" and event_id in json.dumps(item)

                        message = wait_notification(received)
                        wake_turn = message["params"]["turnId"]
                        wait_notification(
                            lambda value: (
                                value.get("method") == "turn/completed"
                                and value["params"]["turn"]["id"] == wake_turn
                            )
                        )
                        self.wait_until(
                            lambda: store.event(event_id)["state"] == "delivered",
                            "real queue delivery was not recorded",
                        )
                        self.assertFalse(
                            any(
                                value.get("method") == "item/started"
                                and value.get("params", {}).get("threadId") == decoy
                                and event_id in json.dumps(value)
                                for value in notifications
                            )
                        )
                        # A wake arriving while the original thread is busy must
                        # also reach that thread and leave it able to finish.
                        block_next_response.set()
                        busy_turn = rpc(
                            "turn/start",
                            {
                                "threadId": thread,
                                "input": [
                                    {"type": "text", "text": "Review another change."}
                                ],
                            },
                        )["turn"]["id"]
                        self.assertTrue(busy_started.wait(timeout=5))
                        busy_registration, _ = store.register(
                            source="test", subject="busy-thread", thread_id=thread
                        )
                        event_id = f"busy:{busy_registration['id']}:done"
                        store.emit(
                            registration_id=busy_registration["id"],
                            event_id=event_id,
                            message=f"CODEX_WAKE {event_id}\nTask finished while reviewing.",
                        )
                        try:
                            self.wait_until(
                                lambda: store.event(event_id)["state"] == "delivered",
                                "queue did not accept the wake while the thread was busy",
                            )
                        finally:
                            release_busy.set()
                        busy_completion = wait_notification(
                            lambda value: (
                                value.get("method") == "turn/completed"
                                and value["params"]["turn"]["id"] == busy_turn
                            )
                        )
                        self.assertEqual(
                            busy_completion["params"]["turn"]["status"], "completed"
                        )
                        busy_message = wait_notification(received)
                        wait_notification(
                            lambda value: (
                                value.get("method") == "turn/completed"
                                and value["params"]["turn"]["id"]
                                == busy_message["params"]["turnId"]
                            )
                        )
                        self.assertFalse(
                            any(
                                value.get("method") == "item/started"
                                and value.get("params", {}).get("threadId") == decoy
                                and event_id in json.dumps(value)
                                for value in notifications
                            )
                        )
                        rpc("thread/archive", {"threadId": thread})
                        self.wait_until(
                            lambda: (
                                {
                                    target["thread_id"]
                                    for target in store.active_targets()
                                }
                                == {decoy}
                            ),
                            "SessionEnd did not release only the intended target",
                        )
                        rpc("thread/archive", {"threadId": decoy})
                        self.wait_until(
                            lambda: not store.active_targets(),
                            "SessionEnd did not release the second target",
                        )
                finally:
                    service.uninstall_macos_service()
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
                    uninstall_plugins(store)


if __name__ == "__main__":
    unittest.main()
