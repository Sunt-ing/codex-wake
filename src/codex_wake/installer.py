from __future__ import annotations

import importlib.resources
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import Store


MARKETPLACE_NAME = "codex-wake-local"
PLUGIN_NAME = "codex-wake"


def _command(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def prepare_release(
    state_dir: Path,
    *,
    codex_bin: Path | None = None,
    codex_home: Path | None = None,
    sqlite_home: Path | None = None,
) -> tuple[Path, str]:
    cachebuster = (
        time.strftime("local-%Y%m%d-%H%M%S", time.gmtime())
        + f"-{time.time_ns() % 1_000_000_000:09d}"
    )
    release = state_dir / "releases" / cachebuster
    source = importlib.resources.files("codex_wake").joinpath(
        "bundled_plugins", PLUGIN_NAME
    )
    release.mkdir(parents=True)
    plugin = release / "plugins" / PLUGIN_NAME
    shutil.copytree(source, plugin)

    manifest_path = plugin / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    version = f"{manifest['version'].split('+', 1)[0]}+codex.{cachebuster}"
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    hooks_path = plugin / "hooks" / "hooks.json"
    hooks = json.loads(hooks_path.read_text())
    prefix = [
        str(Path(sys.executable).absolute()),
        "-m",
        "codex_wake",
        "--state-dir",
        str(state_dir.absolute()),
        "session",
    ]
    target = []
    if codex_bin is not None:
        target.extend(["--codex-bin", str(codex_bin.absolute())])
    if codex_home is not None:
        target.extend(["--codex-home", str(codex_home.absolute())])
    if sqlite_home is not None:
        target.extend(["--sqlite-home", str(sqlite_home.absolute())])
    hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _command(
        [*prefix, "start-hook", *target]
    )
    hooks["hooks"]["SessionEnd"][0]["hooks"][0]["command"] = _command(
        [*prefix, "end-hook", *target]
    )
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n")

    marketplace = {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "Codex Wake Local"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    marketplace_path = release / ".agents/plugins/marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(json.dumps(marketplace, indent=2) + "\n")
    return release, version


def _environment(codex_home: Path, sqlite_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["CODEX_SQLITE_HOME"] = str(sqlite_home)
    return environment


def _run(command: list[str], environment: dict[str, str], *, check: bool = True) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, env=environment
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"{' '.join(command[:3])} exited {result.returncode}: {detail[:2000]}"
        )
    return result.stdout


def _remove_plugin(codex_bin: Path, environment: dict[str, str]) -> None:
    _run(
        [
            str(codex_bin),
            "plugin",
            "remove",
            f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
            "--json",
        ],
        environment,
        check=False,
    )
    _run(
        [
            str(codex_bin),
            "plugin",
            "marketplace",
            "remove",
            MARKETPLACE_NAME,
            "--json",
        ],
        environment,
        check=False,
    )


def _activate_plugin(
    codex_bin: Path,
    environment: dict[str, str],
    release: Path,
    version: str,
) -> None:
    _run(
        [str(codex_bin), "plugin", "marketplace", "add", str(release), "--json"],
        environment,
    )
    _run(
        [
            str(codex_bin),
            "plugin",
            "add",
            f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
            "--json",
        ],
        environment,
    )
    listing = json.loads(
        _run(
            [
                str(codex_bin),
                "plugin",
                "list",
                "--marketplace",
                MARKETPLACE_NAME,
                "--json",
            ],
            environment,
        )
    )
    matches = [
        item
        for item in listing.get("installed", [])
        if item.get("name") == PLUGIN_NAME
        and item.get("marketplaceName") == MARKETPLACE_NAME
        and item.get("version") == version
        and item.get("installed") is True
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Codex did not activate the expected Codex Wake plugin version"
        )


def install_plugin(
    store: Store,
    *,
    codex_bin: Path,
    codex_home: Path,
    sqlite_home: Path,
) -> dict:
    for directory in (codex_home, sqlite_home):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    release, version = prepare_release(
        store.state_dir,
        codex_bin=codex_bin,
        codex_home=codex_home,
        sqlite_home=sqlite_home,
    )
    environment = _environment(codex_home, sqlite_home)
    previous = next(
        (
            item
            for item in store.installations()
            if item["codex_home"] == str(codex_home)
        ),
        None,
    )
    if previous is not None:
        _remove_plugin(codex_bin, environment)
    try:
        _activate_plugin(codex_bin, environment, release, version)
    except Exception as error:
        _remove_plugin(codex_bin, environment)
        if previous is not None:
            try:
                _activate_plugin(
                    codex_bin,
                    environment,
                    Path(previous["release_dir"]),
                    previous["plugin_version"],
                )
            except Exception as rollback_error:
                raise RuntimeError(
                    f"plugin update failed ({error}); rollback failed ({rollback_error})"
                ) from error
        raise
    store.record_installation(
        codex_home=codex_home,
        sqlite_home=sqlite_home,
        codex_bin=codex_bin,
        marketplace_name=MARKETPLACE_NAME,
        plugin_version=version,
        release_dir=release,
    )
    return {
        "codex_home": str(codex_home),
        "plugin_version": version,
        "release": str(release),
    }


def uninstall_plugins(store: Store) -> list[dict]:
    removed = []
    for installation in store.installations():
        environment = _environment(
            Path(installation["codex_home"]), Path(installation["sqlite_home"])
        )
        codex_bin = installation["codex_bin"]
        marketplace = installation["marketplace_name"]
        _run(
            [
                codex_bin,
                "plugin",
                "remove",
                f"{PLUGIN_NAME}@{marketplace}",
                "--json",
            ],
            environment,
        )
        # A previous attempt or manual cleanup may already have removed the
        # marketplace. Verify absence instead of ignoring removal failures.
        listing = json.loads(
            _run([codex_bin, "plugin", "marketplace", "list", "--json"], environment)
        )
        if (
            not isinstance(listing, dict)
            or not isinstance(listing.get("marketplaces"), list)
            or not all(
                isinstance(item, dict) and isinstance(item.get("name"), str)
                for item in listing["marketplaces"]
            )
        ):
            raise RuntimeError("Codex returned an invalid marketplace list")
        if any(item["name"] == marketplace for item in listing["marketplaces"]):
            _run(
                [codex_bin, "plugin", "marketplace", "remove", marketplace, "--json"],
                environment,
            )
        store.remove_installation(installation["codex_home"])
        removed.append({"codex_home": installation["codex_home"]})
    return removed
