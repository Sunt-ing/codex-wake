from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse


PENDING_GITHUB = {"queued", "in_progress", "pending", "requested", "waiting"}
# Manual jobs can still be started; approval is not task completion.
TERMINAL_GITLAB = {"success", "failed", "canceled", "skipped"}
PENDING_EXIT_CODE = 75


class AdapterInputError(ValueError):
    pass


def _config(request: dict) -> tuple[str, str, dict]:
    if not isinstance(request, dict) or request.get("protocol_version") != 1:
        raise AdapterInputError("adapter request protocol_version must be 1")
    registration_id = request.get("registration_id")
    subject = request.get("subject")
    config = request.get("config")
    if not isinstance(registration_id, str) or not registration_id:
        raise AdapterInputError("adapter request requires registration_id")
    if not isinstance(subject, str) or not subject:
        raise AdapterInputError("adapter request requires subject")
    if not isinstance(config, dict):
        raise AdapterInputError("adapter request config must be an object")
    return registration_id, subject, config


def _command(command: list[str]) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("source status command timed out after 10s") from error
    except OSError as error:
        raise RuntimeError(
            f"could not execute source status command: {error}"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"source status command exited {result.returncode}: {detail[:2000]}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"source status command returned invalid JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeError("source status command must return a JSON object")
    return value


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required source CLI is not installed: {name}")
    return path


def _event(registration_id: str, source: str, status: str, url: str) -> dict:
    event_id = f"{source}:{registration_id}:terminal"
    return {
        "state": "terminal",
        "event_id": event_id,
        "message": f"CODEX_WAKE {event_id}\nsource={source} status={status}\n{url}",
    }


def github_actions(request: dict) -> dict:
    registration_id, _, config = _config(request)
    repository = config.get("repository")
    run_id = config.get("run_id")
    if not isinstance(repository, str) or "/" not in repository:
        raise AdapterInputError("github-actions requires repository=OWNER/REPO")
    if not isinstance(run_id, (str, int)) or not str(run_id):
        raise AdapterInputError("github-actions requires run_id")
    run = _command(
        [
            _executable("gh"),
            "run",
            "view",
            str(run_id),
            "--repo",
            repository,
            "--json",
            "status,conclusion,url",
        ]
    )
    status = run.get("status")
    if status in PENDING_GITHUB:
        return {"state": "pending"}
    if status != "completed":
        raise RuntimeError(f"unknown GitHub Actions status: {status!r}")
    conclusion = run.get("conclusion") or "unknown"
    url = run.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub Actions response is missing url")
    return _event(registration_id, "github-actions", str(conclusion), url)


def gitlab_ci(request: dict) -> dict:
    registration_id, _, config = _config(request)
    project = config.get("project")
    object_id = config.get("id")
    kind = config.get("kind", "pipeline")
    if not isinstance(project, str) or "/" not in project:
        raise AdapterInputError("gitlab-ci requires project=NAMESPACE/PROJECT")
    if not isinstance(object_id, (str, int)) or not str(object_id):
        raise AdapterInputError("gitlab-ci requires id")
    if kind not in {"pipeline", "job"}:
        raise AdapterInputError("gitlab-ci kind must be pipeline or job")
    plural = "pipelines" if kind == "pipeline" else "jobs"
    endpoint = f"projects/{urllib.parse.quote(project, safe='')}/{plural}/{object_id}"
    command = [_executable("glab"), "api", endpoint]
    hostname = config.get("hostname")
    if hostname is not None:
        if not isinstance(hostname, str) or not hostname:
            raise AdapterInputError("gitlab-ci hostname must be a non-empty string")
        command.extend(["--hostname", hostname])
    value = _command(command)
    status = value.get("status")
    if status not in TERMINAL_GITLAB:
        if not isinstance(status, str) or not status:
            raise RuntimeError("GitLab CI response is missing status")
        return {"state": "pending"}
    url = value.get("web_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitLab CI response is missing web_url")
    return _event(registration_id, "gitlab-ci", status, url)


def status_command(request: dict) -> dict:
    registration_id, subject, config = _config(request)
    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise AdapterInputError("status-command requires a non-empty command array")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("status-command timed out after 10s") from error
    except OSError as error:
        raise RuntimeError(f"could not execute status-command: {error}") from error
    if result.returncode == PENDING_EXIT_CODE:
        return {"state": "pending"}
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"status-command exited {result.returncode}: {detail[:2000]}"
        )
    message = result.stdout.strip() or f"{subject} finished"
    event_id = f"status-command:{registration_id}:terminal"
    return {
        "state": "terminal",
        "event_id": event_id,
        "message": f"CODEX_WAKE {event_id}\nsource=status-command status=completed\n{message}",
    }


ADAPTERS = {
    "github-actions": github_actions,
    "gitlab-ci": gitlab_ci,
    "status-command": status_command,
}


def check(name: str, request: dict) -> dict:
    return ADAPTERS[name](request)
