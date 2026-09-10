"""Isolated local fixtures for interactive Codex surface acceptance on macOS.

Run with the package and its test extra installed. This helper never changes
production Codex configuration or starts a real model request.
"""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import uuid

from codex_wake import service
from codex_wake.core import Store
from codex_wake.installer import install_plugin, uninstall_plugins


class SurfaceFixture:
    def __init__(self, codex=None):
        self.codex = str(codex or shutil.which("codex"))
        self.stack = ExitStack()

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.stack.close()
            raise

    def start(self):
        self.root = Path(
            self.stack.enter_context(
                tempfile.TemporaryDirectory(prefix="cw-ui-", dir="/tmp")
            )
        )
        self.home = self.root / "h"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.count = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.count += 1
                text = f"LOCAL_TEST_ACK_{fixture.count}"
                message = {
                    "id": f"msg_{fixture.count}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
                events = [
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**message, "status": "in_progress", "content": []},
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": message["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": message,
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": f"resp_{fixture.count}",
                            "status": "completed",
                            "output": [message],
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        },
                    },
                ]
                body = "".join(
                    "data: " + json.dumps(event) + "\n\n" for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.stack.callback(self.http.server_close)
        self.stack.callback(self.http.shutdown)
        (self.home / "config.toml").write_text(
            'model_provider="wake_test"\nmodel="gpt-6-astra"\n'
            'cli_auth_credentials_store="file"\n'
            f'[projects.{json.dumps(str(self.workspace))}]\ntrust_level="trusted"\n'
            '[model_providers.wake_test]\nname="Wake test"\nwire_api="responses"\n'
            f'base_url="http://127.0.0.1:{self.http.server_port}/v1"\n'
        )
        (self.home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "local-fixture-not-a-real-key"})
        )
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CODEX_")
        }
        self.env.update(
            CODEX_HOME=str(self.home),
            CODEX_SQLITE_HOME=str(self.home),
            TERM="xterm-256color",
        )
        # The wrapper is selected only in the temporary VS Code profile.
        self.wrapper = self.root / "codex-test"
        self.wrapper.write_text(
            "#!/bin/sh\n"
            + f'export CODEX_HOME={shlex.quote(str(self.home))}\nexport CODEX_SQLITE_HOME={shlex.quote(str(self.home))}\nunset CODEX_THREAD_ID\nexec {shlex.quote(self.codex)} "$@"\n'
        )
        self.wrapper.chmod(0o755)
        self.store = self.stack.enter_context(Store(self.state))
        install_plugin(
            self.store,
            codex_bin=Path(self.codex),
            codex_home=self.home,
            sqlite_home=self.home,
        )
        self.stack.callback(uninstall_plugins, self.store)
        self.trust_hooks()
        label = f"com.sunting.codex-wake.test-{uuid.uuid4().hex}"
        self.stack.enter_context(patch.object(service, "LAUNCH_AGENT_LABEL", label))
        self.stack.enter_context(
            patch.object(
                service, "launch_agent_path", return_value=self.root / f"{label}.plist"
            )
        )
        self.stack.callback(service.uninstall_macos_service)
        service.install_macos_service(self.state)
        (self.root / "fixture.json").write_text(
            json.dumps(
                {
                    "state": str(self.state),
                    "home": str(self.home),
                    "workspace": str(self.workspace),
                    "wrapper": str(self.wrapper),
                    "label": label,
                }
            )
        )
        return self

    def trust_hooks(self):
        from websockets.sync.client import unix_connect

        with (self.root / "bootstrap.log").open("w") as log:
            server = subprocess.Popen(
                [self.codex, "app-server", "--listen", "unix://"],
                env=self.env,
                stdout=log,
                stderr=log,
            )
            try:
                socket = self.home / "app-server-control/app-server-control.sock"
                deadline = time.monotonic() + 10
                while not socket.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("control socket did not start")
                    time.sleep(0.05)
                with unix_connect(str(socket), compression=None) as ws:
                    sequence = 0

                    def rpc(method, params):
                        nonlocal sequence
                        sequence += 1
                        ws.send(
                            json.dumps(
                                {"id": sequence, "method": method, "params": params}
                            )
                        )
                        while True:
                            value = json.loads(ws.recv(timeout=10))
                            if value.get("id") == sequence:
                                if "error" in value:
                                    raise RuntimeError(value["error"])
                                return value["result"]

                    rpc(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "codex_wake_surface_test",
                                "version": "1",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                    hooks = rpc("hooks/list", {"cwds": [str(self.workspace)]})["data"][
                        0
                    ]["hooks"]
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
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

    def watch(self, thread):
        registration, _ = self.store.register(
            source="status-command",
            subject="interactive surface acceptance",
            thread_id=thread,
            adapter_command=[
                sys.executable,
                "-m",
                "codex_wake",
                "adapter-check",
                "status-command",
            ],
            adapter_config={
                "command": [sys.executable, "-c", "print('WAKE_SURFACE_VERIFIED')"]
            },
        )
        return f"status-command:{registration['id']}:terminal"

    def __exit__(self, *_):
        self.stack.close()


if __name__ == "__main__":
    with SurfaceFixture(sys.argv[1] if len(sys.argv) > 1 else None) as fixture:
        print(fixture.root, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
