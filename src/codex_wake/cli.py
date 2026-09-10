from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from .adapters import check as check_adapter
from .core import QueueClient, Store, default_state_dir, process_token, work_once
from .doctor import doctor
from .installer import install_plugin, uninstall_plugins
from .lifecycle import hook_target
from .service import install_service, run_daemon, uninstall_service


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="codex-wake")
    result.add_argument("--state-dir", type=Path, default=default_state_dir())
    result.add_argument("--json", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)

    session = commands.add_parser("session", help="Manage Codex session targets")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    start = session_commands.add_parser("start")
    start.add_argument("--thread", required=True)
    start.add_argument("--surface", default="cli")
    start.add_argument("--codex-bin", type=Path, required=True)
    start.add_argument("--codex-home", type=Path, required=True)
    start.add_argument("--sqlite-home", type=Path, required=True)
    start.add_argument("--pid", type=int, default=os.getppid())
    start.add_argument("--process-token")
    end = session_commands.add_parser("end")
    end.add_argument("--thread", required=True)
    end.add_argument("--pid", type=int, default=os.getppid())
    end.add_argument("--process-token")
    for hook in (
        session_commands.add_parser("start-hook"),
        session_commands.add_parser("end-hook"),
    ):
        hook.add_argument("--codex-bin", type=Path)
        hook.add_argument("--codex-home", type=Path)
        hook.add_argument("--sqlite-home", type=Path)

    register = commands.add_parser("register", help="Watch an existing external task")
    adapters = register.add_subparsers(dest="adapter", required=True)
    github = adapters.add_parser("github-actions")
    github.add_argument("--repository", required=True, metavar="OWNER/REPO")
    github.add_argument("--run-id", required=True)
    gitlab = adapters.add_parser("gitlab-ci")
    gitlab.add_argument("--project", required=True, metavar="NAMESPACE/PROJECT")
    gitlab.add_argument("--id", required=True)
    gitlab.add_argument("--kind", choices=("pipeline", "job"), default="pipeline")
    gitlab.add_argument("--hostname")
    status_command = adapters.add_parser("status-command")
    status_command.add_argument("status_argv", nargs=argparse.REMAINDER)
    custom = adapters.add_parser("custom")
    custom.add_argument("--source", required=True)
    custom.add_argument("--command-json", required=True)
    custom.add_argument("--config-json", default="{}")
    for adapter in (github, gitlab, status_command, custom):
        adapter.add_argument("--subject")
        adapter.add_argument(
            "--message",
            help="Replace the default follow-up instruction for this wake; task results are preserved",
        )
        adapter.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))

    emit = commands.add_parser("emit")
    emit.add_argument("--registration", required=True)
    emit.add_argument("--event-id", required=True)
    emit.add_argument("--message", required=True)
    emit.add_argument("--ttl", type=float)

    commands.add_parser("work-once", help="Process due work once")
    commands.add_parser("daemon", help="Run the delivery daemon")
    install = commands.add_parser(
        "install", help="Install the current-user service and plugin"
    )
    install.add_argument("--codex-bin", type=Path)
    install.add_argument("--codex-home", type=Path)
    install.add_argument("--sqlite-home", type=Path)
    commands.add_parser("uninstall", help="Remove the current-user service and plugin")
    commands.add_parser("status")
    commands.add_parser("doctor")
    events = commands.add_parser("events")
    events.add_argument("--limit", type=int, default=20)
    adapter_check = commands.add_parser("adapter-check", help="Run one adapter check")
    adapter_check.add_argument(
        "adapter", choices=("github-actions", "gitlab-ci", "status-command")
    )
    return result


def output(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True))
        return
    print(" ".join(f"{key}={value[key]}" for key in value))


def output_doctor(value: dict, as_json: bool) -> None:
    if as_json:
        output(value, True)
        return
    for check in value["checks"]:
        print(f"{check['status']} {check['name']}: {check['detail']}")


def registration_spec(args: argparse.Namespace) -> tuple[str, str, list[str], dict]:
    adapter_command = [
        sys.executable,
        "-m",
        "codex_wake",
        "adapter-check",
        args.adapter,
    ]
    if args.adapter == "github-actions":
        config = {"repository": args.repository, "run_id": args.run_id}
        return (
            args.adapter,
            args.subject or f"{args.repository} Actions run {args.run_id}",
            adapter_command,
            config,
        )
    if args.adapter == "gitlab-ci":
        config = {"project": args.project, "id": args.id, "kind": args.kind}
        if args.hostname:
            config["hostname"] = args.hostname
        return (
            args.adapter,
            args.subject or f"{args.project} {args.kind} {args.id}",
            adapter_command,
            config,
        )
    if args.adapter == "status-command":
        command = args.status_argv
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            raise ValueError("status-command requires a command after --")
        return (
            args.adapter,
            args.subject or "status command",
            adapter_command,
            {"command": command},
        )
    command = json.loads(args.command_json)
    config = json.loads(args.config_json)
    if not isinstance(command, list) or not all(
        isinstance(part, str) and part for part in command
    ):
        raise ValueError("--command-json must be a non-empty JSON string array")
    if not command:
        raise ValueError("--command-json must be a non-empty JSON string array")
    if not isinstance(config, dict):
        raise ValueError("--config-json must be a JSON object")
    if not args.subject:
        raise ValueError("custom adapters require --subject")
    return args.source, args.subject, command, config


def lifecycle_target(
    store: Store,
    payload: dict,
    *,
    codex_bin: Path | None = None,
    codex_home: Path | None = None,
    sqlite_home: Path | None = None,
) -> dict:
    codex_home = codex_home or Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    )
    codex_home = codex_home.expanduser().absolute()
    installation = next(
        (
            item
            for item in store.installations()
            if Path(item["codex_home"]) == codex_home
        ),
        None,
    )
    return hook_target(
        payload,
        codex_bin=(
            codex_bin or (Path(installation["codex_bin"]) if installation else None)
        ),
        codex_home=codex_home,
        sqlite_home=sqlite_home,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "adapter-check":
            request = json.load(sys.stdin)
            print(json.dumps(check_adapter(args.adapter, request), sort_keys=True))
            return 0
        read_only = args.command in {"doctor", "events", "status"}
        with Store(args.state_dir, read_only=read_only) as store:
            if args.command == "session" and args.session_command == "start":
                token = args.process_token or process_token(args.pid)
                if token is None:
                    raise ValueError("target process is not active")
                target = store.register_target(
                    thread_id=args.thread,
                    surface=args.surface,
                    codex_bin=args.codex_bin.absolute(),
                    codex_home=args.codex_home.resolve(),
                    sqlite_home=args.sqlite_home.resolve(),
                    pid=args.pid,
                    token=token,
                )
                output(
                    {"event": "target_registered", "target_id": target["id"]}, args.json
                )
            elif args.command == "session" and args.session_command == "start-hook":
                target = lifecycle_target(
                    store,
                    json.load(sys.stdin),
                    codex_bin=args.codex_bin,
                    codex_home=args.codex_home,
                    sqlite_home=args.sqlite_home,
                )
                record = store.register_target(**target)
                output(
                    {"event": "target_registered", "target_id": record["id"]},
                    args.json,
                )
            elif args.command == "session" and args.session_command == "end":
                token = args.process_token or process_token(args.pid)
                closed = bool(token) and store.close_target(
                    thread_id=args.thread, pid=args.pid, token=token
                )
                output({"event": "target_closed", "closed": closed}, args.json)
            elif args.command == "session" and args.session_command == "end-hook":
                target = lifecycle_target(
                    store,
                    json.load(sys.stdin),
                    codex_bin=args.codex_bin,
                    codex_home=args.codex_home,
                    sqlite_home=args.sqlite_home,
                )
                closed = store.close_target(
                    thread_id=target["thread_id"],
                    pid=target["pid"],
                    token=target["token"],
                )
                output({"event": "target_closed", "closed": closed}, args.json)
            elif args.command == "register":
                if not args.thread:
                    raise ValueError("current Codex thread is unknown; pass --thread")
                source, subject, adapter_command, adapter_config = registration_spec(
                    args
                )
                registration, created = store.register(
                    source=source,
                    subject=subject,
                    thread_id=args.thread,
                    adapter_command=adapter_command,
                    adapter_config=adapter_config,
                    wake_message=args.message,
                )
                output(
                    {
                        "event": "registered" if created else "duplicate",
                        "registration_id": registration["id"],
                    },
                    args.json,
                )
            elif args.command == "emit":
                event, created = store.emit(
                    registration_id=args.registration,
                    event_id=args.event_id,
                    message=args.message,
                    ttl_seconds=args.ttl,
                )
                output(
                    {
                        "event": "queued" if created else "duplicate",
                        "event_id": event["event_id"],
                    },
                    args.json,
                )
            elif args.command == "work-once":
                output(work_once(store), args.json)
            elif args.command == "daemon":
                run_daemon(store)
            elif args.command == "install":
                executable = args.codex_bin or shutil.which("codex")
                if not executable:
                    raise ValueError("could not locate the Codex executable")
                codex_home = (
                    (
                        args.codex_home
                        or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
                    )
                    .expanduser()
                    .absolute()
                )
                sqlite_home = (
                    (
                        args.sqlite_home
                        or Path(os.environ.get("CODEX_SQLITE_HOME", codex_home))
                    )
                    .expanduser()
                    .absolute()
                )
                QueueClient().probe(
                    {
                        "codex_bin": str(Path(executable).absolute()),
                        "codex_home": str(codex_home),
                        "sqlite_home": str(sqlite_home),
                    }
                )
                unit = install_service(args.state_dir)
                plugin = install_plugin(
                    store,
                    codex_bin=Path(executable).absolute(),
                    codex_home=codex_home,
                    sqlite_home=sqlite_home,
                )
                output(
                    {"event": "installed", "service": str(unit), "plugin": plugin},
                    args.json,
                )
            elif args.command == "uninstall":
                output(
                    {
                        "event": "uninstalled",
                        "plugins": uninstall_plugins(store),
                        "service_removed": uninstall_service(args.state_dir),
                    },
                    args.json,
                )
            elif args.command == "status":
                output(store.status(), args.json)
            elif args.command == "doctor":
                result = doctor(store)
                output_doctor(result, args.json)
                return 0 if result["healthy"] else 1
            elif args.command == "events":
                output({"events": store.events(args.limit)}, args.json)
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"codex-wake: {error}", file=sys.stderr)
        return 2
    return 0
