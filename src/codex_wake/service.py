from __future__ import annotations

import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .core import Store, process_is_alive, process_token, work_once


SERVICE_NAME = "codex-wake.service"
LAUNCH_AGENT_LABEL = "com.sunting.codex-wake"
WINDOWS_STARTUP_NAME = "codex-wake.cmd"


def run_daemon(
    store: Store,
    *,
    interval_seconds: float = 1.0,
    max_cycles: int | None = None,
) -> None:
    pid = os.getpid()
    token = process_token(pid)
    if token is None:
        raise RuntimeError("could not identify the daemon process")
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    previous = {}
    for name in (signal.SIGINT, signal.SIGTERM):
        previous[name] = signal.signal(name, stop)
    store.runtime_start(pid, token, __version__)
    cycles = 0
    try:
        while not stopping:
            work_once(store, progress=lambda: store.heartbeat(pid, token))
            store.heartbeat(pid, token)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            time.sleep(interval_seconds)
    finally:
        store.runtime_stop(pid, token)
        for name, handler in previous.items():
            signal.signal(name, handler)


def systemd_unit(state_dir: Path, python: Path | None = None) -> str:
    executable = (
        str((python or Path(sys.executable)).absolute())
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )
    state = str(state_dir.absolute()).replace("\\", "\\\\").replace('"', '\\"')
    return f"""[Unit]
Description=Codex Wake delivery daemon

[Service]
Type=simple
ExecStart=\"{executable}\" -m codex_wake --state-dir \"{state}\" daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def systemd_user_dir() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "systemd/user"
    )


def _runtime(state_dir: Path) -> dict:
    try:
        with Store(state_dir, read_only=True) as store:
            return store.runtime_status()
    except (OSError, ValueError, sqlite3.Error):
        return {}


def _wait_for_runtime(
    state_dir: Path, previous_identity: str | None, startup_timeout: float
) -> None:
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        runtime = _runtime(state_dir)
        if (
            runtime.get("healthy")
            and runtime.get("version") == __version__
            and runtime.get("identity") != previous_identity
        ):
            return
        time.sleep(0.1)
    raise RuntimeError(
        "the restarted Codex Wake daemon did not report a new process at the expected version"
    )


def install_linux_service(state_dir: Path, *, startup_timeout: float = 10.0) -> Path:
    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / SERVICE_NAME
    previous_unit = unit.read_text(encoding="utf-8") if unit.is_file() else None
    try:
        previous_identity = _runtime(state_dir).get("identity")
        unit.write_text(systemd_unit(state_dir), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True)
        subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True)
        _wait_for_runtime(state_dir, previous_identity, startup_timeout)
        return unit
    except Exception:
        if previous_unit is None:
            unit.unlink(missing_ok=True)
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
                check=False,
            )
        else:
            unit.write_text(previous_unit, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if previous_unit is not None:
            subprocess.run(
                ["systemctl", "--user", "restart", SERVICE_NAME], check=False
            )
        raise


def uninstall_linux_service() -> bool:
    unit = systemd_user_dir() / SERVICE_NAME
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SERVICE_NAME], check=False
    )
    removed = unit.exists()
    unit.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return removed


def launch_agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def launch_agent(state_dir: Path, python: Path | None = None) -> bytes:
    state_dir = state_dir.absolute()
    # launchd does not read shell profiles. Preserve the installer's search path
    # so adapters can find Homebrew/user-installed CLIs after login as well.
    search_path = os.environ.get("PATH", os.defpath)
    return plistlib.dumps(
        {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": [
                str((python or Path(sys.executable)).absolute()),
                "-m",
                "codex_wake",
                "--state-dir",
                str(state_dir.absolute()),
                "daemon",
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": search_path, "PYTHONUNBUFFERED": "1"},
            "StandardOutPath": str(state_dir / "daemon.stdout.log"),
            "StandardErrorPath": str(state_dir / "daemon.stderr.log"),
        }
    )


def _bootstrap_macos_service(domain: str, agent: Path, timeout: float) -> None:
    # bootout can return before launchd has finished tearing down the old job.
    # During that window bootstrap returns EIO (5), even for a valid plist.
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(agent)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        if result.returncode != 5 or time.monotonic() >= deadline:
            raise RuntimeError(
                f"could not load {agent}: {result.stderr.strip()} "
                f"(launchctl exit {result.returncode})"
            )
        time.sleep(0.1)


def install_macos_service(state_dir: Path, *, startup_timeout: float = 10.0) -> Path:
    state_dir = state_dir.absolute()
    state_dir.mkdir(parents=True, exist_ok=True)
    agent = launch_agent_path()
    agent.parent.mkdir(parents=True, exist_ok=True)
    previous_agent = agent.read_bytes() if agent.is_file() else None
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LAUNCH_AGENT_LABEL}"
    previous_identity = _runtime(state_dir).get("identity")
    try:
        agent.write_bytes(launch_agent(state_dir))
        subprocess.run(
            ["launchctl", "bootout", service], check=False, capture_output=True
        )
        subprocess.run(["launchctl", "enable", service], check=True)
        _bootstrap_macos_service(domain, agent, startup_timeout)
        # RunAtLoad starts the process; kickstart -k here would kill that new
        # process while it is opening the database and registering its runtime.
        _wait_for_runtime(state_dir, previous_identity, startup_timeout)
        return agent
    except Exception as error:
        subprocess.run(
            ["launchctl", "bootout", service], check=False, capture_output=True
        )
        if previous_agent is None:
            agent.unlink(missing_ok=True)
        else:
            agent.write_bytes(previous_agent)
            try:
                _bootstrap_macos_service(domain, agent, startup_timeout)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"service update failed ({error}); rollback failed ({rollback_error})"
                ) from error
        raise RuntimeError(
            f"{error}; daemon log: {state_dir / 'daemon.stderr.log'}"
        ) from error


def uninstall_macos_service() -> bool:
    agent = launch_agent_path()
    service = f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
    result = subprocess.run(
        ["launchctl", "bootout", service], capture_output=True, text=True
    )
    if result.returncode:
        # An already absent service is fine, but do not silently report success
        # (and remove its definition) when a loaded daemon could not be stopped.
        loaded = subprocess.run(
            ["launchctl", "print", service], capture_output=True, text=True
        )
        if loaded.returncode == 0:
            raise RuntimeError(f"could not stop {service}: {result.stderr.strip()}")
    removed = agent.exists()
    agent.unlink(missing_ok=True)
    return removed


def windows_startup_path() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    return root / "Microsoft/Windows/Start Menu/Programs/Startup" / WINDOWS_STARTUP_NAME


def windows_startup_script(state_dir: Path, python: Path | None = None) -> str:
    command = subprocess.list2cmdline(
        [
            str((python or Path(sys.executable)).absolute()),
            "-m",
            "codex_wake",
            "--state-dir",
            str(state_dir.absolute()),
            "daemon",
        ]
    )
    return f'@echo off\r\nstart "" /b {command}\r\n'


def _stop_windows_runtime(state_dir: Path) -> None:
    runtime = _runtime(state_dir)
    if not runtime.get("running"):
        return
    pid = runtime["pid"]
    token = runtime["identity"].split(":", 1)[1]
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process_is_alive(pid, token):
        time.sleep(0.05)
    if process_is_alive(pid, token):
        raise RuntimeError(f"could not stop the previous Codex Wake daemon PID {pid}")
    with Store(state_dir) as store:
        store.runtime_stop(pid, token)


def install_windows_service(state_dir: Path, *, startup_timeout: float = 10.0) -> Path:
    startup = windows_startup_path()
    startup.parent.mkdir(parents=True, exist_ok=True)
    previous_script = startup.read_text(encoding="utf-8") if startup.is_file() else None
    previous_identity = _runtime(state_dir).get("identity")
    process = None
    try:
        startup.write_text(windows_startup_script(state_dir), encoding="utf-8")
        _stop_windows_runtime(state_dir)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_wake",
                "--state-dir",
                str(state_dir.absolute()),
                "daemon",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            ),
        )
        _wait_for_runtime(state_dir, previous_identity, startup_timeout)
        return startup
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
        if previous_script is None:
            startup.unlink(missing_ok=True)
        else:
            startup.write_text(previous_script, encoding="utf-8")
            subprocess.Popen(
                ["cmd.exe", "/c", str(startup)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
        raise


def uninstall_windows_service(state_dir: Path) -> bool:
    startup = windows_startup_path()
    _stop_windows_runtime(state_dir)
    removed = startup.exists()
    startup.unlink(missing_ok=True)
    return removed


def install_service(state_dir: Path) -> Path:
    if sys.platform.startswith("linux"):
        return install_linux_service(state_dir)
    if sys.platform == "darwin":
        return install_macos_service(state_dir)
    if sys.platform == "win32":
        return install_windows_service(state_dir)
    raise ValueError(f"service installation is not supported on {sys.platform}")


def uninstall_service(state_dir: Path) -> bool:
    if sys.platform.startswith("linux"):
        return uninstall_linux_service()
    if sys.platform == "darwin":
        return uninstall_macos_service()
    if sys.platform == "win32":
        return uninstall_windows_service(state_dir)
    raise ValueError(f"service removal is not supported on {sys.platform}")
