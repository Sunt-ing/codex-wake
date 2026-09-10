"""Interactive TUI acceptance with a real PTY and local model fixture."""

import os
import re
import sys
import time
import unittest

from macos_surface_fixture import SurfaceFixture


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("CODEX_WAKE_CODEX_E2E") == "1",
    "requires macOS, real Codex and CODEX_WAKE_CODEX_E2E=1",
)
class MacOSCLITest(unittest.TestCase):
    def test_interactive_cli_displays_wake_and_releases_session(self):
        import pexpect

        with SurfaceFixture() as fixture:
            child = pexpect.spawn(
                fixture.codex,
                ["--no-alt-screen", "-C", str(fixture.workspace)],
                env=fixture.env,
                encoding="utf-8",
                dimensions=(40, 160),
            )
            output = ""

            def pump_until(predicate, timeout=20):
                nonlocal output
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    try:
                        data = child.read_nonblocking(30000, timeout=0.1)
                    except pexpect.TIMEOUT:
                        continue
                    output += data
                    if "\x1b[6n" in data:
                        child.send("\x1b[1;1R")
                self.fail(
                    "TUI did not reach expected state: "
                    + re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)[-4000:]
                )

            try:
                pump_until(lambda: "gpt-6-astra default" in output)
                child.send("Initialize interactive acceptance.")
                pump_until(lambda: "Initialize interactive acceptance." in output)
                time.sleep(1)
                child.send("\r")
                pump_until(
                    lambda: (
                        "LOCAL_TEST_ACK_1" in output
                        and bool(fixture.store.active_targets())
                    )
                )
                thread = fixture.store.active_targets()[0]["thread_id"]
                event_id = fixture.watch(thread)
                pump_until(
                    lambda: (
                        "WAKE_SURFACE_VERIFIED" in output
                        and re.search(r"LOCAL_TEST_ACK_(?:[2-9]|[1-9][0-9]+)\b", output)
                    )
                )
                self.assertEqual(fixture.store.event(event_id)["state"], "delivered")
                child.send("/quit")
                time.sleep(0.3)
                child.send("\r")
                child.expect(pexpect.EOF, timeout=10)
                deadline = time.monotonic() + 5
                while fixture.store.active_targets() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertFalse(fixture.store.active_targets())
            finally:
                child.close(force=True)
