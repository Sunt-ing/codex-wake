<h1 align="center">Codex Wake</h1>

<p align="center"><strong>Stop polling spam and wasted tokens: long-running tasks wait quietly in the background, waking the original session on completion.</strong></p>

<p align="center">
  <a href="https://github.com/Sunt-ing/codex-wake/actions/workflows/tests.yml"><img src="https://github.com/Sunt-ing/codex-wake/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="#get-started"><img src="assets/nav-get-started.svg" alt="Get started" height="28"></a>
  <a href="#reference"><img src="assets/nav-reference.svg" alt="Reference" height="28"></a>
  <a href="README.zh-CN.md"><img src="assets/nav-chinese.svg" alt="中文" height="28"></a>
</p>

![Codex Wake: comparison between traditional polling and event-driven wake-up workflows](assets/readme-hero-v3.png)

Codex Wake is an event-driven task watcher designed for Codex. It offloads polling for CI/CD pipelines, model training runs, cluster jobs, and other long-running tasks to an independent background daemon. When the task finishes, it delivers the results back to the original session using `codex queue`, waking Codex to continue processing.

> **Supported environments**: Codex CLI (TUI) and VS Code extension (**desktop App is not supported**).

---

## Why Codex Wake?

Having an LLM actively poll for status is an **anti-pattern**.

When you have Codex submit a cluster job or GitHub Action that takes hours to complete, you often end up stuck in this dilemma:

1. **Context pollution**: Codex checks the status every few minutes. Pages of "queued/running" flood the screen, burying your actual code reviews, architectural plans, and key conclusions under repetitive status logs.
2. **Wasted tokens**: Repeated status checks force the model to keep reading query outputs and deciding whether to keep waiting. These zero-information exchanges consume tokens and bloat subsequent turn contexts.
3. **"Fake work" breaks focus**: The interface constantly shows Codex is working, but clicking in reveals it just ran another status check. Worse, it sometimes drifts off to investigate "why the polling script is still waiting," straying from the real task.

**Codex's compute should be spent on thinking and coding, not stuck acting as a "human timer" in an infinite loop.**

---

## How Codex Wake works

The whole process takes three steps:

1. **Register the task**: After submitting the job with your usual tools, Codex registers a watch as guided by the bundled skill (binding the current session ID and specifying the data source or status command). Once registered, Codex can continue with other work, or end its turn if there is nothing else to do.
2. **Background status checks**: An independent background daemon takes over polling and saves its state in local SQLite. Background queries consume no model tokens and do not clutter your chat interface.
3. **Wake the original session**: Once the task finishes, the daemon delivers a completion message via `codex queue --thread <id> --message <result>`, waking the original session so Codex can inspect the result and proceed.

Codex Wake delivers messages back to the original session via Codex's native `queue` command, using the exact executable and environment configuration of that session. Results route directly into the existing conversation—no guessing the "latest chat", simulating keystrokes, or briefing a separate agent session. Codex Wake does not host an App Server.

Watches and pending messages survive a **daemon restart**. However, the originating Codex process must remain running; exiting it discards its watches, and reopening the conversation later will not restore them. Delivery retries after a crash may produce duplicates; the bundled skill guides the agent to recognize them by event ID.

---

## Get started

You'll need Python 3.11+ and a Codex build with `queue` and `plugin` support.
Use TUI or the Codex extension for VS Code; **the desktop App is not supported**.
Background services support macOS (launchd), Linux (systemd), and Windows 10+.

**Let your agent set it up.** Paste this into Codex:

```text
Install https://github.com/Sunt-ing/codex-wake following its README.
Use a persistent Python environment and my current Codex installation.
Run doctor, guide me through hook trust, and tell me when to open a new
session. Verify that session is registered before claiming it is ready.
```

Or install it yourself with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install git+https://github.com/Sunt-ing/codex-wake.git
codex-wake install
codex-wake doctor
```

If the command is not on PATH, run `uv tool update-shell` and reopen your terminal.
Prefer pip? Use `python -m pip install git+https://github.com/Sunt-ing/codex-wake.git`
in a virtualenv you intend to keep, then run the same `install` and `doctor` commands.

Then connect your session:

1. Start Codex with the same `CODEX_HOME`. In `/hooks`, review and trust Codex Wake's `SessionStart` and `SessionEnd` hooks. For VS Code, use the CLI with the extension's `CODEX_HOME` to do this review.
2. Start a **new TUI session or VS Code chat** to load the hooks and skill. Run `codex-wake status` and check that a session is registered.
3. Ask Codex to watch your task. For example:

> $codex-wake Watch GitHub Actions run 123456789 in OWNER/REPO. When it finishes, read the result and investigate any failures.

Or, for your own infrastructure:

> $codex-wake Watch training job 1234 using our cluster's status command. When it finishes, fetch the metrics and summarize the result.

The [bundled skill](src/codex_wake/bundled_plugins/codex-wake/skills/codex-wake/SKILL.md)
knows the registration workflow. For other sources, Codex can write a small
status checker in your project. You don't need to change Codex Wake to add one.
GitHub Actions uses your authenticated `gh`; GitLab uses `glab`.

---

## Reference

<details>
<summary><strong>Watch commands: GitHub, GitLab, and anything with a status command</strong></summary>

Run these inside the Codex session that should receive the result.
`CODEX_THREAD_ID` supplies the destination; use `--thread` for explicit integrations.

```sh
codex-wake register github-actions \
  --repository OWNER/REPO --run-id 123456789

codex-wake register gitlab-ci \
  --project GROUP/PROJECT --kind pipeline --id 12345

codex-wake register status-command --subject "training job 1234" -- \
  /absolute/path/to/check-training 1234
```

Use `--message "Inspect the logs and continue with the next experiment."` on any
registration to set follow-up instructions in advance. Custom instructions prompt Codex
to take proactive action (such as kicking off follow-up tasks or diagnosing errors)
rather than just replying "OK, got it." It replaces the default instruction, while
preserving the adapter's task status and result locations.
Place it before `--` for `status-command`. Without it, Wake appends
“If necessary, check the result and decide what to do next.”

For GitLab, `--kind job` watches one job, and `--hostname gitlab.example.com`
selects another host.

A status checker makes one bounded query, then exits:

| Exit code | Meaning |
| --- | --- |
| `75` | Still queued or running; check again later. |
| `0` | Finished; stdout becomes the completion message. |
| Anything else | The check failed; retry with backoff. |

A failed or canceled job is also a finished job: exit `0` and describe its
outcome. Finish the check within 10 seconds and use absolute paths. The daemon
runs with your permissions, outside your project's working directory.

Give different tasks distinct subjects. Repeating the same source and subject
in one session returns the existing watch if the command, configuration, and custom message match;
conflicting configurations are rejected.

</details>

<details>
<summary><strong>Custom adapter protocol</strong></summary>

For integrations that need structured input, an adapter reads one JSON object
from stdin:

```json
{
  "protocol_version": 1,
  "registration_id": "...",
  "source": "example",
  "subject": "task-42",
  "config": {"task_id": "42"}
}
```

Write exactly one JSON object to stdout:

```json
{"state": "pending"}
```

Or, when finished:

```json
{
  "state": "terminal",
  "event_id": "example:REGISTRATION_ID:completed",
  "message": "CODEX_WAKE example:REGISTRATION_ID:completed\ntask-42 completed successfully"
}
```

Replace `REGISTRATION_ID` with the ID from the request. Event IDs must be stable
and unique across the state database. A collision records an error and backs off
that watch; other deliveries continue. Include the event ID in the message so
the receiving agent can recognize duplicate deliveries.

```sh
codex-wake register custom \
  --source example --subject task-42 \
  --command-json '["python", "/absolute/path/to/adapter.py"]' \
  --config-json '{"task_id": "42"}'
```

The adapter queries once. Codex Wake handles scheduling, timeouts, persistence,
and delivery retries.

</details>

<details>
<summary><strong>Installation, troubleshooting, and removal</strong></summary>

`install` creates a user service and installs the lifecycle plugin in the active
`CODEX_HOME`. To use another Codex installation:

```sh
codex-wake install \
  --codex-bin /path/to/codex \
  --codex-home /path/to/codex-home \
  --sqlite-home /path/to/sqlite-home
```

Keep the installing Python environment available: both the service and hooks
record its executable path. Plugin installation does not grant hook trust;
review changed hooks after updates. See [Codex hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

On macOS, install as the logged-in desktop user. launchd uses `gui/<uid>` and
captures your PATH at installation. Reinstall after changing PATH so background
checks can find Homebrew and user-installed commands.

The default macOS state directory is `~/Library/Application Support/codex-wake`.
It contains `daemon.stderr.log` and `daemon.stdout.log`. Set `--state-dir` before
the subcommand to select another location.

```sh
codex-wake doctor
codex-wake --json status
codex-wake --json events --limit 20
# macOS service details
launchctl print gui/$(id -u)/com.sunting.codex-wake
# Remove the plugin and user service
codex-wake uninstall
```

A healthy daemon does not prove that your session's hooks ran. Check the
registered sessions too. An event marked `delivered` means queue accepted it;
it does not prove the model has finished handling it.

The Python runtime uses only the standard library. Source adapters reuse the
authentication in `gh` and `glab`. Workload submission and Codex updates remain
under your control.

</details>

<details>
<summary><strong>Development and test coverage</strong></summary>

```sh
python -m pip install '.[test]' ruff==0.16.6 build
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
python -m build
```

CI runs Python 3.11/3.14 on Linux, Windows, Apple Silicon macOS, and Intel macOS.
Both Mac architectures exercise real launchd startup, replacement, crash
recovery, rollback, delivery, and removal.

For live Codex tests, use a logged-in macOS session with a compatible Codex binary:

```sh
CODEX_WAKE_MACOS_E2E=1 CODEX_WAKE_CODEX_E2E=1 \
  python -m unittest discover -s tests -v
```

Tests use isolated services, temporary state and Codex homes, and a local model
response fixture. They verify plugin installation and updates, lifecycle hooks,
delivery to the intended one of two live threads (both idle and busy), and wake
messages in a real PTY. They do not use remote model inference or change existing Codex sessions.

Manual VS Code acceptance on 2026-09-09 used extension `26.903.61454` with Codex
`0.153.4` on macOS 26.5 (Apple Silicon): the original chat displayed the wake
message and completed the next turn. The fixture is available at
`tests/macos_surface_fixture.py`; use its wrapper in a temporary VS Code profile,
then close the test window and stop the fixture when finished.

Physical logout/relogin and sleep/wake have not been tested. Suspending the
daemon or restarting it is not equivalent to those checks. The desktop App is
outside the supported scope.

</details>

[MIT](LICENSE) © 2026 Sunt-ing
