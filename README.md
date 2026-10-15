# Process HUD

**English** · [Русский](README.ru.md)

**Stop wasting Codex quota on waiting for other programs.**

Tests, builds, imports, and downloads can run for minutes or hours. The command does the actual work, but Codex may still spend model turns repeatedly checking whether it has finished. Each check uses context and tokens without moving the task forward.

**Process HUD takes over that waiting.** Codex starts or registers a long-running job, gets a task ID, and ends its turn. A small local program monitors the job without asking the model anything. When it finishes — whether successfully or with an error — or needs intervention, the connected bridge sends an event back to the conversation responsible for the job. Codex can then pick up where it left off.

The translucent desktop panel makes the handoff visible to you, too. But the main benefit is **event-driven continuation instead of model-driven polling**.

![Process HUD desktop panel showing background jobs](docs/images/process-hud.png)

*Actual Qt interface with demonstration jobs.*

## One event instead of repeated checks

Without Process HUD:

```text
Codex starts a test suite
  → asks whether it finished
  → waits
  → asks again
  → waits
  → checks again
  → finally reads the result
```

With Process HUD:

```text
Codex starts the suite through HUD
  → HUD takes responsibility for monitoring it
  → Codex ends its turn; no model calls are spent waiting
  → HUD watches the process locally
  → completion, failure, or attention needed? HUD emits an event
  → Codex resumes in the right conversation and handles the result
```

A failure is a result, too. The bridge can bring Codex back for a non-zero exit code, a signal interruption, or an observation problem that needs attention. Lost contact isn't silently treated as a failed job, and HUD never restarts the workload on its own.

### Why it matters for quota usage

In [JetBrains' Benjamin-Plus analysis](https://github.com/JetBrains/benjamin-plus-skill/blob/main/EXPECTED-RESULTS.md), **45.8% of agent steps in one studied Java SWE-bench configuration were polling steps**, including 3,982 checks made less than five seconds apart. That is a share of *steps*, not a universal percentage of tokens or quota spent polling. It nevertheless shows how much agent activity can go into checking status rather than solving the task.

We also tested Process HUD itself. Across two live 120-second waiting scenarios — a normal Codex conversation and a subagent returning to its root agent — the waiting conversations made **0 new model turns and recorded 0 token-counter updates**. Completion reached the intended conversations. Success, errors, and interruptions were tested separately.

In other words, the **waiting and monitoring phase can run with no model calls**. Codex still uses tokens to launch the job, inspect its result, and continue working; this is not a claim of a fixed percentage saved from everyone's overall quota.

[Verification results](docs/VALIDATION.md) · [Research and measurement limits](docs/RESEARCH.md)

## Let Codex install it

Process HUD is **installed once per Linux user**, not separately for every agent or project. Any Codex with access to that environment can use the shared command, skill, job store, and HUD window.

Give any Codex agent this instruction:

> Read the installation guide at
> https://github.com/ViskyG/process-hud/blob/main/docs/INSTALL_FOR_CODEX.en.md
> Install or reuse Process HUD on this machine, install the skill, and set up automatic continuation where supported. Verify the real callback on both a successful job and a failed job. Do not poll processes with model turns while waiting.

If the repository is private and Codex cannot open the URL, provide access to a local checkout and point it at `docs/INSTALL_FOR_CODEX.en.md`.

[Installation guide for Codex →](docs/INSTALL_FOR_CODEX.en.md)

## First use: the skill connects the bridge

Process HUD includes a [Codex skill](skills/process-hud/SKILL.md) that tells the agent which long-running, noninteractive commands to hand off and, crucially, **not to wait by repeatedly calling tools or checking logs**.

On the first job that requires automatic continuation, the skill tells Codex to check whether the current conversation is already connected to the local completion bridge. If it is, nothing needs configuring. If not, **Codex runs the bundled setup script itself**, safely reusing the existing installation and preserving backups.

For the supported **Codex extension in VS Code on Linux x86_64**, initial bridge setup needs **one VS Code window reload**. This is because the bridge wraps the Codex App Server at startup; it cannot attach retroactively to the already-running server. After the reload, subsequent conversations are handled automatically. **There is no manual binding of projects, agents, or conversation IDs.** HUD uses verified Codex session metadata to route the result to the originating conversation; subagent results go to the root agent.

When the bridge is live, Codex can run, for example:

```bash
process-hud run --title "Project tests" --resume-codex --cwd /absolute/project/path -- python3 -m pytest
```

The launch returns a task ID immediately. Codex ends its waiting turn; HUD handles the rest until it delivers the result or an attention event.

**Terminal Codex CLI:** the shared CLI and skill can start and monitor jobs, but the published automatic-resume bridge currently targets the VS Code extension. A separately verified terminal auto-resume integration is not yet included. Manual monitoring must not be presented as an automatic callback.

## Install manually

For Linux with Python 3.10+ and `venv` (Ubuntu: `python3-venv`):

```bash
git clone https://github.com/ViskyG/process-hud.git
cd process-hud
bash install.sh
~/.local/bin/process-hud doctor
```

This installs HUD and the Codex skill at `~/.agents/skills/process-hud`. The bridge will be configured by the skill when it is first needed. If you prefer to configure the VS Code bridge yourself:

```bash
~/.local/share/process-hud/venv/bin/python ~/.local/share/process-hud/scripts/install_codex_bridge.py --ensure
```

If newly configured, reload VS Code once. The script leaves an existing working configuration alone and does not silently replace an unrelated `chatgpt.cliExecutable` setting. The bridge will not be ready for the *current* conversation until it is running and the connection is verified.

To see the UI and try a normal background job:

```bash
~/.local/bin/process-hud ui show
~/.local/bin/process-hud run --title "Demo" -- ~/.local/share/process-hud/venv/bin/python ~/.local/share/process-hud/examples/demo.py
```

This demo checks task monitoring, **not** an automatic Codex callback. The [Codex installation guide](docs/INSTALL_FOR_CODEX.en.md) includes success and failure callback checks.

## What you can see while Codex waits

While Codex is idle, Process HUD shows the jobs it handed off:

- A compact, translucent, always-on-top panel that grows with the task list.
- Each job's state, log output, exit code, and measured progress and estimated time remaining *when the job supplies enough information*.
- Desktop notifications and optional sound when work finishes.
- Local subprocesses and attachment to existing PIDs, Docker containers, Kubernetes Jobs, and SSH tasks.

On GNOME, **Ctrl+Alt+H** can show the panel if the shortcut is available. Clicking the cross beside a task **removes its HUD entry without killing the process**. `Ctrl+Z` in the panel restores a recently removed entry in the current UI session.

[Commands and examples →](docs/USAGE.md)

## Current scope

Process HUD currently targets Linux. The UI and local monitoring have been exercised on Ubuntu 24.04 with GNOME/X11, along with Docker inspection and Codex event delivery via the VS Code bridge (including failure and interruption paths). Live Kubernetes Job and remote SSH launch scenarios are not among the published end-to-end validations. Windows, macOS, and reliable always-on-top behavior under Wayland have not been verified.

HUD does not stop or restart workloads and cannot promise that they survive logout or reboot. A quiet process is not automatically considered hung; a lost connection does not imply failure.

[What was actually tested →](docs/VALIDATION.md)

## Documentation

[Codex installation (English)](docs/INSTALL_FOR_CODEX.en.md) · [Установка для Codex (RU)](docs/INSTALL_FOR_CODEX.md) · [Usage](docs/USAGE.md) · [Codex bridge](docs/CODEX_BRIDGE.md) · [Validation](docs/VALIDATION.md) · [Research](docs/RESEARCH.md) · [Architecture](docs/ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md)
