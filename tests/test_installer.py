import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_wake.core import Store
from codex_wake.installer import (
    MARKETPLACE_NAME,
    install_plugin,
    prepare_release,
    uninstall_plugins,
)


class InstallerTest(unittest.TestCase):
    def test_release_bundles_exact_python_hook_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release, version = prepare_release(
                root,
                codex_bin=root / "codex",
                codex_home=root / "codex-home",
                sqlite_home=root / "sqlite-home",
            )
            manifest = json.loads(
                (release / "plugins/codex-wake/.codex-plugin/plugin.json").read_text()
            )
            hooks = json.loads(
                (release / "plugins/codex-wake/hooks/hooks.json").read_text()
            )
            self.assertEqual(manifest["version"], version)
            self.assertTrue(
                (release / "plugins/codex-wake/skills/codex-wake/SKILL.md").is_file()
            )
            start = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertIn("-m codex_wake --state-dir", start)
            self.assertIn(str(Path(temporary).absolute()), start)
            self.assertIn("session start-hook", start)
            self.assertIn(f"--codex-bin {root / 'codex'}", start)
            self.assertIn(f"--codex-home {root / 'codex-home'}", start)
            self.assertIn(f"--sqlite-home {root / 'sqlite-home'}", start)
            self.assertEqual(
                json.loads((release / ".agents/plugins/marketplace.json").read_text())[
                    "name"
                ],
                MARKETPLACE_NAME,
            )

    def test_two_releases_in_the_same_second_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, _ = prepare_release(Path(temporary))
            second, _ = prepare_release(Path(temporary))
            self.assertNotEqual(first, second)

    def test_install_records_only_verified_plugin_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Store(root / "state") as store:

                def fake_run(command, environment, check=True):
                    if command[1:3] == ["plugin", "list"]:
                        release = sorted((root / "state/releases").iterdir())[-1]
                        version = json.loads(
                            (
                                release / "plugins/codex-wake/.codex-plugin/plugin.json"
                            ).read_text()
                        )["version"]
                        return json.dumps(
                            {
                                "installed": [
                                    {
                                        "name": "codex-wake",
                                        "marketplaceName": MARKETPLACE_NAME,
                                        "version": version,
                                        "installed": True,
                                    }
                                ]
                            }
                        )
                    return "{}"

                with patch("codex_wake.installer._run", side_effect=fake_run):
                    installed = install_plugin(
                        store,
                        codex_bin=Path("/bin/codex"),
                        codex_home=root / "codex-home",
                        sqlite_home=root / "sqlite-home",
                    )
                self.assertEqual(installed["codex_home"], str(root / "codex-home"))
                self.assertEqual(len(store.installations()), 1)

    def test_failed_update_restores_previous_release_and_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Store(root / "state") as store:
                previous_release, previous_version = prepare_release(store.state_dir)
                store.record_installation(
                    codex_home=root / "codex-home",
                    sqlite_home=root / "sqlite-home",
                    codex_bin=Path("/bin/codex"),
                    marketplace_name=MARKETPLACE_NAME,
                    plugin_version=previous_version,
                    release_dir=previous_release,
                )
                with (
                    patch("codex_wake.installer._remove_plugin") as remove,
                    patch(
                        "codex_wake.installer._activate_plugin",
                        side_effect=[RuntimeError("new release failed"), None],
                    ) as activate,
                ):
                    with self.assertRaisesRegex(RuntimeError, "new release failed"):
                        install_plugin(
                            store,
                            codex_bin=Path("/bin/codex"),
                            codex_home=root / "codex-home",
                            sqlite_home=root / "sqlite-home",
                        )
                self.assertEqual(remove.call_count, 2)
                self.assertEqual(activate.call_count, 2)
                self.assertEqual(
                    store.installations()[0]["plugin_version"], previous_version
                )

    def test_uninstall_preserves_record_on_failure_and_can_retry(self):
        for failure in ("plugin", "marketplace", "listing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with Store(root / "state") as store:
                    store.record_installation(
                        codex_home=root / "home",
                        sqlite_home=root / "home",
                        codex_bin=root / "codex",
                        marketplace_name=MARKETPLACE_NAME,
                        plugin_version="test",
                        release_dir=root / "release",
                    )
                    installed = {"plugin": True, "marketplace": True}

                    def run(command, **kwargs):
                        operation = (
                            "plugin"
                            if command[1:3] == ["plugin", "remove"]
                            else "listing"
                            if command[3] == "list"
                            else "marketplace"
                        )
                        if operation == failure:
                            return subprocess.CompletedProcess(
                                command, 1, "", "permission denied"
                            )
                        if operation == "listing":
                            items = (
                                [{"name": MARKETPLACE_NAME}]
                                if installed["marketplace"]
                                else []
                            )
                            return subprocess.CompletedProcess(
                                command, 0, json.dumps({"marketplaces": items}), ""
                            )
                        installed[operation] = False
                        return subprocess.CompletedProcess(command, 0, "{}", "")

                    with patch("codex_wake.installer.subprocess.run", side_effect=run):
                        with self.assertRaisesRegex(RuntimeError, "permission denied"):
                            uninstall_plugins(store)
                        self.assertEqual(len(store.installations()), 1)
                        self.assertTrue(installed["marketplace"])
                        failure = None
                        uninstall_plugins(store)
                        self.assertFalse(installed["plugin"])
                        self.assertFalse(installed["marketplace"])
                        self.assertEqual(store.installations(), [])

    def test_uninstall_handles_absent_marketplace_but_rejects_invalid_listing(self):
        for listing in ({"marketplaces": []}, {}, {"marketplaces": [None]}):
            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with Store(root / "state") as store:
                    store.record_installation(
                        codex_home=root / "home",
                        sqlite_home=root / "home",
                        codex_bin=root / "codex",
                        marketplace_name=MARKETPLACE_NAME,
                        plugin_version="test",
                        release_dir=root / "release",
                    )
                    with patch(
                        "codex_wake.installer.subprocess.run",
                        side_effect=[
                            subprocess.CompletedProcess([], 0, "{}", ""),
                            subprocess.CompletedProcess([], 0, json.dumps(listing), ""),
                        ],
                    ):
                        if listing == {"marketplaces": []}:
                            uninstall_plugins(store)
                            self.assertEqual(store.installations(), [])
                        else:
                            with self.assertRaisesRegex(
                                RuntimeError, "invalid marketplace list"
                            ):
                                uninstall_plugins(store)
                            self.assertEqual(len(store.installations()), 1)


if __name__ == "__main__":
    unittest.main()
