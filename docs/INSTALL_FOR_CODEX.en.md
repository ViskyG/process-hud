# Install Process HUD for Codex

This guide is written for a Codex agent. A user can give you this URL and ask you to install Process HUD. The installation belongs to the **Linux user**, not to a particular Codex agent, conversation, or project.

Your goal is to let Codex delegate long-running noninteractive commands to Process HUD, **stop spending model turns on polling**, and resume in the correct conversation when a job succeeds, fails, is interrupted, or needs attention.

## 1. Check the existing installation first

Use the current user's permitted environment. Check whether `~/.local/bin/process-hud` exists, inspect its `doctor` report, and check whether `~/.agents/skills/process-hud/SKILL.md` is installed. If the required components are available, **do not reinstall** just because this is a different agent or project.

`doctor` prints diagnostic information; exit code zero does not certify that the UI or callback works. For a graphical installation, check `pyside6: true` and actually open the window in a desktop session. Missing optional tools such as `kubectl` do not prevent local jobs. If `process-hud` is not on `PATH`, use `~/.local/bin/process-hud` for every command below.

Never terminate someone else's workload to install or upgrade HUD. The installer intentionally refuses to replace active observers. Do not bypass sandbox, tool-approval, or filesystem permission rules.

If HUD is missing, use an existing, clean checkout of `https://github.com/ViskyG/process-hud`, or clone it into an allowed directory:

```bash
git clone https://github.com/ViskyG/process-hud.git
cd process-hud
bash install.sh
~/.local/bin/process-hud doctor
```

The supported host for the graphical UI is Linux (tested on Ubuntu 24.04 GNOME/X11), Python 3.10+ and `venv`. If cloning a private repository fails, ask for access or a local checkout; never invent an alternative source. The installer creates a user-local venv and installs the skill, without enabling background workload autostart.

The paths below assume the default installation. The VS Code bridge installer currently requires `~/.local/share/process-hud` and standard VS Code directories; custom install roots and a different `XDG_DATA_HOME` are not supported by that installer.

## 2. Set up event-driven continuation only when needed

For **Codex inside VS Code on Linux x86_64**, before the **first** long-running operation that needs an automatic callback, check that the current conversation has a live relay:

```bash
~/.local/share/process-hud/venv/bin/python -c 'from process_hud.codex_bridge import current_origin; print(current_origin()["thread_id"])'
```

Success means the bridge already recognizes this exact conversation. No per-agent or per-project binding is necessary: the bridge derives the source thread and, for subagents, its root owner from Codex metadata.

If the check fails because the relay is not configured, use the **installed** setup script:

```bash
~/.local/share/process-hud/venv/bin/python ~/.local/share/process-hud/scripts/install_codex_bridge.py --ensure
```

This is idempotent. It preserves backups under `~/.local/share/process-hud/bridge-backups/` and refuses to replace a conflicting `chatgpt.cliExecutable` setting. If the installed script is absent in an older installation, use the checkout after inspecting the existing installation and following its safe upgrade rules. Do not modify unrelated VS Code settings.

**First-time configuration requires one VS Code window reload.** The relay is a wrapper around the Codex App Server and cannot be injected into an App Server that is already running. After the reload, run the live conversation check again. A `configured` or `already_configured` installer message is not by itself proof of a live callback. Never launch `--resume-codex` before verifying the current conversation.

**Terminal Codex CLI:** the shared HUD CLI and skill can monitor your tasks, but the currently published bridge installer integrates with the VS Code extension, not terminal auto-resume. Do not claim the terminal conversation will wake automatically without a separate, tested integration.

## 3. Follow the skill while working

Read `~/.agents/skills/process-hud/SKILL.md`. Use HUD for substantial, noninteractive commands that need a later result. Do not insert your own repeated `sleep`, `tail -f`, or `report` calls while waiting.

With a verified live bridge, a representative command is:

```bash
process-hud run --title "Project tests" --resume-codex --cwd /absolute/project/path -- python3 -m pytest
```

Do not use an example path literally. Launching returns a task ID, not a final result. End the waiting turn. The local HUD watches the task, and the connected bridge delivers the completion or attention event to the owning Codex conversation. When resumed, read the report once, validate the result, and remove the handled HUD entry. A failed job is not permission to rerun it automatically.

## 4. Verify the end-to-end callback

First confirm the regular HUD UI and a simple job (use `ui show` only in a desktop session). Then, with a verified live bridge and permission to run test jobs, test a success:

```bash
process-hud run --title "HUD success check" --resume-codex -- ~/.local/share/process-hud/venv/bin/python ~/.local/share/process-hud/examples/demo.py --seconds 5
```

In a separate resumed turn, test an intentional failure:

```bash
process-hud run --title "HUD failure check" --resume-codex -- ~/.local/share/process-hud/venv/bin/python ~/.local/share/process-hud/examples/demo.py --seconds 5 --fail
```

Finish the waiting turn after each launch; do not poll. Verify that the correct Codex conversation resumes and reads the actual exit status. The second command is expected to fail; this is a test of **delivery**, not a request to repair or rerun the job.

Report which pieces were already installed, whether the bridge was configured or reused, whether VS Code was reloaded, which live checks passed, and any remaining limitation. Avoid including secret or private job log content in the report.

Further reading: [CLI reference](USAGE.md), [bridge design and rollback](CODEX_BRIDGE.md), [verification evidence](VALIDATION.md).

[Русская инструкция](INSTALL_FOR_CODEX.md)
