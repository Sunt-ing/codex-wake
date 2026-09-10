from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


DEFAULT_WAKE_MESSAGE = "If necessary, check the result and decide what to do next."


SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    codex_bin TEXT NOT NULL,
    codex_home TEXT NOT NULL,
    sqlite_home TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_token TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'closed')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_target_per_thread
    ON targets(thread_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS registrations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES targets(id),
    adapter_command_json TEXT,
    adapter_config_json TEXT NOT NULL DEFAULT '{}',
    wake_message TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'triggered', 'resolved', 'discarded')),
    next_check_at_ms INTEGER NOT NULL DEFAULT 0,
    check_attempts INTEGER NOT NULL DEFAULT 0,
    last_check_error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS registrations_due
    ON registrations(state, next_check_at_ms);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    registration_id TEXT NOT NULL REFERENCES registrations(id),
    message TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'discarded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    delivered_at_ms INTEGER,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS events_due
    ON events(state, next_attempt_at_ms);

CREATE TABLE IF NOT EXISTS runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    pid INTEGER NOT NULL,
    process_token TEXT NOT NULL,
    version TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    heartbeat_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS installations (
    codex_home TEXT PRIMARY KEY,
    sqlite_home TEXT NOT NULL,
    codex_bin TEXT NOT NULL,
    marketplace_name TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    release_dir TEXT NOT NULL,
    installed_at_ms INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def default_state_dir() -> Path:
    override = os.environ.get("CODEX_WAKE_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        return Path(root) / "codex-wake" if root else Path.home() / "codex-wake"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-wake"
    root = os.environ.get("XDG_STATE_HOME")
    return (
        Path(root) / "codex-wake" if root else Path.home() / ".local/state/codex-wake"
    )


def process_token(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split()
        except OSError:
            return None
        return fields[21] if len(fields) > 21 else None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        token = result.stdout.strip()
        return token if result.returncode == 0 and token else None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            if exit_time.dwHighDateTime or exit_time.dwLowDateTime:
                return None
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    # ponytail: unsupported Unix variants only get liveness; add their native
    # process creation token if they become supported targets.
    return "alive"


def process_is_alive(pid: int, token: str) -> bool:
    return process_token(pid) == token


class Store:
    def __init__(self, state_dir: Path | None = None, *, read_only: bool = False):
        self.state_dir = (state_dir or default_state_dir()).resolve()
        self.path = self.state_dir / "state.sqlite3"
        if read_only:
            if not self.path.is_file():
                raise ValueError(f"Codex Wake is not installed: {self.path} is missing")
            self.connection = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=30
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 30000")
            return
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.state_dir, 0o700)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript(SCHEMA)
        with self.transaction():
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(registrations)")
            }
            if "wake_message" not in columns:
                self.connection.execute(
                    "ALTER TABLE registrations ADD COLUMN wake_message TEXT"
                )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def register_target(
        self,
        *,
        thread_id: str,
        surface: str,
        codex_bin: Path,
        codex_home: Path,
        sqlite_home: Path,
        pid: int,
        token: str,
    ) -> dict:
        values = (thread_id, surface, str(codex_bin), str(codex_home), str(sqlite_home))
        if any(not value.strip() for value in values):
            raise ValueError("target fields must be non-empty")
        if not process_is_alive(pid, token):
            raise ValueError("Codex target process is not active")
        target_id = str(uuid.uuid4())
        timestamp = now_ms()
        with self.transaction():
            current = self.connection.execute(
                "SELECT * FROM targets WHERE thread_id = ? AND state = 'active'",
                (thread_id,),
            ).fetchone()
            if (
                current is not None
                and current["pid"] == pid
                and current["process_token"] == token
            ):
                target_id = current["id"]
                self.connection.execute(
                    """
                    UPDATE targets SET surface = ?, codex_bin = ?, codex_home = ?,
                        sqlite_home = ?, updated_at_ms = ? WHERE id = ?
                    """,
                    (
                        surface,
                        str(codex_bin),
                        str(codex_home),
                        str(sqlite_home),
                        timestamp,
                        target_id,
                    ),
                )
            else:
                if current is not None:
                    self._close_target(current["id"], timestamp)
                self.connection.execute(
                    """
                    INSERT INTO targets (
                        id, thread_id, surface, codex_bin, codex_home, sqlite_home,
                        pid, process_token, state, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (target_id, *values, pid, token, timestamp, timestamp),
                )
        return self.target(target_id)

    def _close_target(self, target_id: str, timestamp: int) -> None:
        self.connection.execute(
            "UPDATE targets SET state = 'closed', updated_at_ms = ? WHERE id = ?",
            (timestamp, target_id),
        )
        self.connection.execute(
            """
            UPDATE registrations SET state = 'discarded', updated_at_ms = ?
            WHERE target_id = ? AND state = 'active'
            """,
            (timestamp, target_id),
        )
        self.connection.execute(
            """
            UPDATE events SET state = 'discarded', last_error = 'target closed'
            WHERE registration_id IN (
                SELECT id FROM registrations WHERE target_id = ?
            ) AND state = 'pending'
            """,
            (target_id,),
        )

    def close_target(self, *, thread_id: str, pid: int, token: str) -> bool:
        row = self.connection.execute(
            """
            SELECT id FROM targets
            WHERE thread_id = ? AND pid = ? AND process_token = ? AND state = 'active'
            """,
            (thread_id, pid, token),
        ).fetchone()
        if row is None:
            return False
        with self.transaction():
            self._close_target(row["id"], now_ms())
        return True

    def target(self, target_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM targets WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown target: {target_id}")
        return dict(row)

    def active_target(self, thread_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM targets WHERE thread_id = ? AND state = 'active'",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no active target for thread: {thread_id}")
        target = dict(row)
        if not process_is_alive(target["pid"], target["process_token"]):
            with self.transaction():
                self._close_target(target["id"], now_ms())
            raise ValueError("Codex target process is no longer active")
        return target

    def register(
        self,
        *,
        source: str,
        subject: str,
        thread_id: str,
        adapter_command: list[str] | None = None,
        adapter_config: dict | None = None,
        wake_message: str | None = None,
    ) -> tuple[dict, bool]:
        if not source.strip() or not subject.strip():
            raise ValueError("source and subject must be non-empty")
        if adapter_command is not None and (
            not adapter_command
            or not all(isinstance(part, str) and part for part in adapter_command)
        ):
            raise ValueError("adapter command must be a non-empty string array")
        if adapter_config is not None and not isinstance(adapter_config, dict):
            raise ValueError("adapter config must be an object")
        if wake_message is not None and (
            not isinstance(wake_message, str) or not wake_message.strip()
        ):
            raise ValueError("wake message must be a non-empty string")
        target = self.active_target(thread_id)
        key = "\x1f".join((target["id"], source, subject))
        timestamp = now_ms()
        with self.transaction():
            existing = self.connection.execute(
                "SELECT * FROM registrations WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                if (
                    json.loads(existing["adapter_command_json"] or "null")
                    != adapter_command
                    or json.loads(existing["adapter_config_json"])
                    != (adapter_config or {})
                    or existing["wake_message"] != wake_message
                ):
                    raise ValueError(
                        "registration already exists with a different adapter command "
                        "or config/message; use a different --subject for a separate task"
                    )
                return dict(existing), False
            registration_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO registrations (
                    id, idempotency_key, source, subject, target_id,
                    adapter_command_json, adapter_config_json, wake_message, state,
                    next_check_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    registration_id,
                    key,
                    source,
                    subject,
                    target["id"],
                    json.dumps(adapter_command) if adapter_command else None,
                    json.dumps(adapter_config or {}, sort_keys=True),
                    wake_message,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM registrations WHERE id = ?", (registration_id,)
            ).fetchone()
        return dict(row), True

    def emit(
        self,
        *,
        registration_id: str,
        event_id: str,
        message: str,
        ttl_seconds: float | None = None,
    ) -> tuple[dict, bool]:
        if not event_id.strip() or not message.strip():
            raise ValueError("event ID and message must be non-empty")
        timestamp = now_ms()
        expires_at = (
            timestamp + int(ttl_seconds * 1000) if ttl_seconds is not None else None
        )
        with self.transaction():
            registration = self.connection.execute(
                "SELECT * FROM registrations WHERE id = ?", (registration_id,)
            ).fetchone()
            if registration is None:
                raise ValueError(f"unknown registration: {registration_id}")
            existing = self.connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["registration_id"] != registration_id
                    or existing["message"] != message
                ):
                    raise ValueError(f"event ID collision: {event_id}")
                return dict(existing), False
            if registration["state"] != "active":
                raise ValueError(f"registration is not active: {registration['state']}")
            self.connection.execute(
                """
                INSERT INTO events (
                    event_id, registration_id, message, state, next_attempt_at_ms,
                    expires_at_ms, created_at_ms
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (event_id, registration_id, message, timestamp, expires_at, timestamp),
            )
            self.connection.execute(
                """
                UPDATE registrations SET state = 'triggered', updated_at_ms = ?
                WHERE id = ?
                """,
                (timestamp, registration_id),
            )
            row = self.connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return dict(row), True

    def due_registrations(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT r.*, t.thread_id, t.pid, t.process_token,
                   t.state AS target_state
            FROM registrations AS r
            JOIN targets AS t ON t.id = r.target_id
            WHERE r.state = 'active'
              AND r.adapter_command_json IS NOT NULL
              AND r.next_check_at_ms <= ?
            ORDER BY r.next_check_at_ms, r.created_at_ms
            LIMIT ?
            """,
            (now_ms(), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_checked(self, registration_id: str, next_check_at_ms: int) -> None:
        with self.transaction():
            self.connection.execute(
                """
                UPDATE registrations SET next_check_at_ms = ?, check_attempts = 0,
                    last_check_error = NULL, updated_at_ms = ?
                WHERE id = ? AND state = 'active'
                """,
                (next_check_at_ms, now_ms(), registration_id),
            )

    def mark_check_error(self, registration_id: str, error: str, attempts: int) -> None:
        delay_ms = min(60_000, 1000 * 2 ** min(attempts, 6))
        with self.transaction():
            self.connection.execute(
                """
                UPDATE registrations SET check_attempts = check_attempts + 1,
                    next_check_at_ms = ?, last_check_error = ?, updated_at_ms = ?
                WHERE id = ? AND state = 'active'
                """,
                (now_ms() + delay_ms, error[:2000], now_ms(), registration_id),
            )

    def discard_registration(self, registration_id: str, reason: str) -> None:
        timestamp = now_ms()
        with self.transaction():
            self.connection.execute(
                """
                UPDATE registrations SET state = 'discarded', last_check_error = ?,
                    updated_at_ms = ? WHERE id = ? AND state IN ('active', 'triggered')
                """,
                (reason, timestamp, registration_id),
            )
            self.connection.execute(
                """
                UPDATE events SET state = 'discarded', last_error = ?
                WHERE registration_id = ? AND state = 'pending'
                """,
                (reason, registration_id),
            )

    def due_events(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT e.*, r.target_id, r.wake_message, t.thread_id, t.codex_bin, t.codex_home,
                   t.sqlite_home, t.pid, t.process_token, t.state AS target_state
            FROM events AS e
            JOIN registrations AS r ON r.id = e.registration_id
            JOIN targets AS t ON t.id = r.target_id
            WHERE e.state = 'pending' AND e.next_attempt_at_ms <= ?
            ORDER BY e.next_attempt_at_ms, e.created_at_ms
            LIMIT ?
            """,
            (now_ms(), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_delivered(self, event_id: str) -> None:
        timestamp = now_ms()
        with self.transaction():
            self.connection.execute(
                """
                UPDATE events SET state = 'delivered', delivered_at_ms = ?,
                    attempts = attempts + 1, last_error = NULL
                WHERE event_id = ? AND state = 'pending'
                """,
                (timestamp, event_id),
            )
            self.connection.execute(
                """
                UPDATE registrations SET state = 'resolved', updated_at_ms = ?
                WHERE id = (SELECT registration_id FROM events WHERE event_id = ?)
                """,
                (timestamp, event_id),
            )

    def mark_retry(self, event_id: str, error: str, attempts: int) -> None:
        delay_ms = min(60_000, 1000 * 2 ** min(attempts, 6))
        with self.transaction():
            self.connection.execute(
                """
                UPDATE events SET attempts = attempts + 1, next_attempt_at_ms = ?,
                    last_error = ? WHERE event_id = ? AND state = 'pending'
                """,
                (now_ms() + delay_ms, error[:2000], event_id),
            )

    def discard(self, event_id: str, reason: str) -> None:
        with self.transaction():
            self.connection.execute(
                """
                UPDATE events SET state = 'discarded', last_error = ?
                WHERE event_id = ? AND state = 'pending'
                """,
                (reason, event_id),
            )
            self.connection.execute(
                """
                UPDATE registrations SET state = 'discarded', updated_at_ms = ?
                WHERE id = (SELECT registration_id FROM events WHERE event_id = ?)
                """,
                (now_ms(), event_id),
            )

    def event(self, event_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown event: {event_id}")
        return dict(row)

    def status(self) -> dict:
        counts = {
            row["state"]: row["count"]
            for row in self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM events GROUP BY state"
            )
        }
        return {
            "database": str(self.path),
            "runtime": self.runtime_status(),
            "targets_active": self.connection.execute(
                "SELECT COUNT(*) FROM targets WHERE state = 'active'"
            ).fetchone()[0],
            "registrations_active": self.connection.execute(
                "SELECT COUNT(*) FROM registrations WHERE state = 'active'"
            ).fetchone()[0],
            "events": counts,
        }

    def runtime_start(self, pid: int, token: str, version: str) -> None:
        timestamp = now_ms()
        with self.transaction():
            current = self.connection.execute(
                "SELECT * FROM runtime WHERE singleton = 1"
            ).fetchone()
            if current is not None and process_is_alive(
                current["pid"], current["process_token"]
            ):
                raise RuntimeError(
                    f"Codex Wake daemon is already running as PID {current['pid']}"
                )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO runtime (
                    singleton, pid, process_token, version, started_at_ms, heartbeat_at_ms
                ) VALUES (1, ?, ?, ?, ?, ?)
                """,
                (pid, token, version, timestamp, timestamp),
            )

    def heartbeat(self, pid: int, token: str) -> None:
        with self.transaction():
            changed = self.connection.execute(
                """
                UPDATE runtime SET heartbeat_at_ms = ?
                WHERE singleton = 1 AND pid = ? AND process_token = ?
                """,
                (now_ms(), pid, token),
            ).rowcount
            if not changed:
                raise RuntimeError("Codex Wake daemon lost its runtime lease")

    def runtime_stop(self, pid: int, token: str) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM runtime WHERE singleton = 1 AND pid = ? AND process_token = ?",
                (pid, token),
            )

    def runtime_status(self) -> dict:
        row = self.connection.execute(
            "SELECT * FROM runtime WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return {"running": False, "healthy": False}
        value = dict(row)
        running = process_is_alive(value["pid"], value["process_token"])
        age_ms = max(0, now_ms() - value["heartbeat_at_ms"])
        return {
            "running": running,
            "healthy": running and age_ms <= 30_000,
            "identity": f"{value['pid']}:{value['process_token']}",
            "pid": value["pid"],
            "version": value["version"],
            "heartbeat_age_ms": age_ms,
        }

    def events(self, limit: int = 20) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT event_id, registration_id, state, attempts, created_at_ms,
                   delivered_at_ms, last_error
            FROM events ORDER BY created_at_ms DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_targets(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM targets WHERE state = 'active' ORDER BY created_at_ms"
            )
        ]

    def record_installation(
        self,
        *,
        codex_home: Path,
        sqlite_home: Path,
        codex_bin: Path,
        marketplace_name: str,
        plugin_version: str,
        release_dir: Path,
    ) -> None:
        with self.transaction():
            self.connection.execute(
                """
                INSERT OR REPLACE INTO installations (
                    codex_home, sqlite_home, codex_bin, marketplace_name,
                    plugin_version, release_dir, installed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(codex_home),
                    str(sqlite_home),
                    str(codex_bin),
                    marketplace_name,
                    plugin_version,
                    str(release_dir),
                    now_ms(),
                ),
            )

    def installations(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM installations ORDER BY installed_at_ms"
            )
        ]

    def remove_installation(self, codex_home: str) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM installations WHERE codex_home = ?", (codex_home,)
            )


class QueueError(RuntimeError):
    pass


class QueueClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    @staticmethod
    def environment(target: dict) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CODEX_THREAD_ID", "CODEX_WAKE_TARGET_ID"}
        }
        environment["CODEX_HOME"] = target["codex_home"]
        environment["CODEX_SQLITE_HOME"] = target["sqlite_home"]
        return environment

    def probe(self, target: dict) -> None:
        result = self._run([target["codex_bin"], "queue", "--help"], target)
        help_text = result.stdout + result.stderr
        if not all(
            marker in help_text
            for marker in ("Usage: codex queue", "--thread", "--message")
        ):
            raise QueueError("codex queue does not support --thread and --message")

    def deliver(self, event: dict) -> None:
        self._run(
            [
                event["codex_bin"],
                "queue",
                "--thread",
                event["thread_id"],
                "--message",
                event["message"]
                + "\n\n"
                + (event.get("wake_message") or DEFAULT_WAKE_MESSAGE),
            ],
            event,
        )

    def _run(
        self, command: list[str], target: dict
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self.environment(target),
            )
        except subprocess.TimeoutExpired as error:
            raise QueueError(
                f"codex queue timed out after {self.timeout:g}s"
            ) from error
        except (OSError, ValueError) as error:
            raise QueueError(f"could not execute codex queue: {error}") from error
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise QueueError(f"codex queue exited {result.returncode}: {detail[:2000]}")
        return result


class AdapterError(RuntimeError):
    pass


class AdapterClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def check(self, registration: dict) -> dict:
        command = json.loads(registration["adapter_command_json"])
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise AdapterError("adapter command must be a non-empty JSON string array")
        request = {
            "protocol_version": 1,
            "registration_id": registration["id"],
            "source": registration["source"],
            "subject": registration["subject"],
            "config": json.loads(registration["adapter_config_json"]),
        }
        try:
            result = subprocess.run(
                command,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise AdapterError(f"adapter timed out after {self.timeout:g}s") from error
        except (OSError, ValueError) as error:
            raise AdapterError(f"could not execute adapter: {error}") from error
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise AdapterError(f"adapter exited {result.returncode}: {detail[:2000]}")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AdapterError(f"adapter returned invalid JSON: {error}") from error
        if not isinstance(response, dict) or response.get("state") not in (
            "pending",
            "terminal",
        ):
            raise AdapterError("adapter state must be pending or terminal")
        if response["state"] == "terminal" and (
            not isinstance(response.get("event_id"), str)
            or not response["event_id"].strip()
            or not isinstance(response.get("message"), str)
            or not response["message"].strip()
        ):
            raise AdapterError("terminal adapter result requires event_id and message")
        return response


def poll_due(
    store: Store,
    client: AdapterClient | None = None,
    *,
    interval_seconds: float = 15.0,
    workers: int = 4,
) -> dict[str, int]:
    adapter = client or AdapterClient()
    result = {"pending": 0, "terminal": 0, "discarded": 0, "failed": 0}
    # One worker-sized batch keeps queued results from waiting behind hundreds
    # of slow status checks. Remaining watches stay due for the next cycle.
    due = store.due_registrations(limit=max(1, workers))
    active = []
    for registration in due:
        if registration["target_state"] != "active" or not process_is_alive(
            registration["pid"], registration["process_token"]
        ):
            store.discard_registration(registration["id"], "target closed")
            result["discarded"] += 1
        else:
            active.append(registration)

    def checked(registration: dict) -> tuple[dict, dict | Exception]:
        try:
            return registration, adapter.check(registration)
        except AdapterError as error:
            return registration, error

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for registration, outcome in executor.map(checked, active):
            if isinstance(outcome, Exception):
                store.mark_check_error(
                    registration["id"], str(outcome), registration["check_attempts"]
                )
                result["failed"] += 1
            elif outcome["state"] == "pending":
                store.mark_checked(
                    registration["id"], now_ms() + int(interval_seconds * 1000)
                )
                result["pending"] += 1
            else:
                try:
                    store.emit(
                        registration_id=registration["id"],
                        event_id=outcome["event_id"],
                        message=outcome["message"],
                    )
                except ValueError as error:
                    # A conflicting adapter event must not prevent unrelated
                    # registrations or already queued events from progressing.
                    store.mark_check_error(
                        registration["id"], str(error), registration["check_attempts"]
                    )
                    result["failed"] += 1
                else:
                    result["terminal"] += 1
    return result


def deliver_due(
    store: Store,
    client: QueueClient | None = None,
    *,
    progress: Callable[[], None] | None = None,
) -> dict[str, int]:
    queue = client or QueueClient()
    result = {"delivered": 0, "discarded": 0, "retried": 0}
    timestamp = now_ms()
    for event in store.due_events(limit=4):
        if event["expires_at_ms"] is not None and event["expires_at_ms"] <= timestamp:
            store.discard(event["event_id"], "event expired")
            result["discarded"] += 1
            continue
        if event["target_state"] != "active" or not process_is_alive(
            event["pid"], event["process_token"]
        ):
            store.discard(event["event_id"], "target closed")
            result["discarded"] += 1
            continue
        try:
            if progress is not None:
                progress()
            queue.probe(event)
            if progress is not None:
                progress()
            queue.deliver(event)
        except QueueError as error:
            store.mark_retry(event["event_id"], str(error), event["attempts"])
            result["retried"] += 1
        else:
            store.mark_delivered(event["event_id"])
            result["delivered"] += 1
    return result


def work_once(
    store: Store, *, progress: Callable[[], None] | None = None
) -> dict[str, dict[str, int]]:
    for target in store.active_targets():
        if not process_is_alive(target["pid"], target["process_token"]):
            store.close_target(
                thread_id=target["thread_id"],
                pid=target["pid"],
                token=target["process_token"],
            )
    if progress is not None:
        progress()
    polled = poll_due(store)
    if progress is not None:
        progress()
    return {"poll": polled, "delivery": deliver_due(store, progress=progress)}
