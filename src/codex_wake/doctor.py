from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from . import __version__
from .core import QueueClient, QueueError, Store


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _plugin_check(installation: dict) -> dict:
    release = Path(installation["release_dir"])
    manifest_path = release / "plugins/codex-wake/.codex-plugin/plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return _check("plugin", "FAIL", f"invalid release manifest: {error}")
    if manifest.get("version") != installation["plugin_version"]:
        return _check("plugin", "FAIL", "recorded and release plugin versions differ")
    environment = os.environ.copy()
    environment["CODEX_HOME"] = installation["codex_home"]
    environment["CODEX_SQLITE_HOME"] = installation["sqlite_home"]
    try:
        result = subprocess.run(
            [
                installation["codex_bin"],
                "plugin",
                "list",
                "--marketplace",
                installation["marketplace_name"],
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        listing = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return _check("plugin", "FAIL", f"could not inspect live plugin: {error}")
    matches = [
        item
        for item in listing.get("installed", [])
        if item.get("name") == "codex-wake"
        and item.get("marketplaceName") == installation["marketplace_name"]
        and item.get("version") == installation["plugin_version"]
        and item.get("installed") is True
    ]
    if len(matches) != 1:
        return _check(
            "plugin", "FAIL", "live Codex plugin does not match recorded release"
        )
    return _check("plugin", "PASS", installation["plugin_version"])


def doctor(store: Store) -> dict:
    checks = []
    try:
        quick_check = store.connection.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as error:
        return {
            "healthy": False,
            "checks": [_check("database", "FAIL", str(error))],
        }
    checks.append(
        _check(
            "database",
            "PASS" if quick_check == "ok" else "FAIL",
            str(quick_check),
        )
    )
    if quick_check != "ok":
        return {"healthy": False, "checks": checks}
    runtime = store.runtime_status()
    checks.append(
        _check(
            "daemon",
            "PASS"
            if runtime["healthy"] and runtime.get("version") == __version__
            else "FAIL",
            (
                f"healthy version {__version__}"
                if runtime["healthy"] and runtime.get("version") == __version__
                else "not running, heartbeat is stale, or version differs"
            ),
        )
    )
    installations = store.installations()
    if not installations:
        checks.append(
            _check("plugin", "FAIL", "no Codex plugin installation is recorded")
        )
    else:
        checks.extend(_plugin_check(installation) for installation in installations)

    targets = store.active_targets()
    if not targets:
        checks.append(
            _check(
                "queue",
                "WARN",
                "no live Codex session is registered; review Codex Wake in /hooks and start a new session",
            )
        )
    for target in targets:
        try:
            QueueClient().probe(target)
        except QueueError as error:
            checks.append(_check("queue", "FAIL", f"{target['thread_id']}: {error}"))
        else:
            checks.append(_check("queue", "PASS", target["thread_id"]))

    failed_watches = store.connection.execute(
        "SELECT id, source, last_check_error FROM registrations "
        "WHERE state = 'active' AND last_check_error IS NOT NULL "
        "ORDER BY created_at_ms, id"
    ).fetchall()
    if failed_watches:
        checks.extend(
            _check(
                "polling",
                "WARN",
                f"{watch['id']} ({watch['source']}): {watch['last_check_error']}",
            )
            for watch in failed_watches
        )
    else:
        checks.append(_check("polling", "PASS", "no active watches have query errors"))

    failed_events = store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE state = 'pending' AND last_error IS NOT NULL"
    ).fetchone()[0]
    checks.append(
        _check(
            "delivery",
            "WARN" if failed_events else "PASS",
            f"{failed_events} pending event(s) have errors",
        )
    )
    return {
        "healthy": not any(check["status"] == "FAIL" for check in checks),
        "checks": checks,
    }
