# Process HUD: implementation design

## One coherent system, not an orchestrator

The CLI creates durable task identities in a private SQLite database. Each managed
job has a detached Python supervisor; each attached backend has a detached
read-only observer. The Qt viewer is a separate process, reading this database.
All three core components are deterministic programs. No model invocation, HTTP listener,
execution queue, paid API, automatic restart or login service is introduced.

The optional completion bridge in CODEX_BRIDGE.md is an explicit opt-in integration
with the existing Codex client. It submits one tool-result event at job completion
and leaves inference idle during the wait. It adds no workload control or scheduler.

This deliberately avoids a dependency on Pueue's queue and systemd user-manager
availability. SQLite provides concurrent transactions, not JSON overwrites.
A managed supervisor becomes a Linux subreaper before spawning the command,
records the direct exit code and reaps orphaned descendants. A descendant still
running prevents premature completion. Processes deliberately delegated to other
services must be attached by their backend identity; subreaping is not containment.

## Identity, evidence and lifetime

Job ID: UUID hex. Optional idempotency key prevents accidental duplicate launches;
a repeated key with different launch parameters is an error. Boot ID + PID +
process creation time identifies Linux processes; Docker uses full container ID;
Kubernetes uses context + namespace + name + UID; remote managed tasks use host +
remote executable + remote UUID. Renamed/recreated objects are not silently adopted.

Execution state is separate from observation availability and terminal evidence.
Quiet output does not change a live process to hung. Missing PID => unknown outcome.
Broken remote connection => disconnected observation, not a failed task.
A report must expose evidence and capability limitations, not merely a green icon.

Managed jobs survive CLI exit, closed stdin/terminal and UI hide/exit. Logout/reboot
survival is not promised. A supervisor killed by the OS cannot reconstruct child
exit codes: recovery marks unknown; it never reruns the workload. Read-only
observers can be safely restarted. Stored results are retained after reboot.

## UI contract

Native Qt/PySide6: headerless 294-pixel strip, 30-pixel rows, alpha .24 background
with fully legible text. Natural height, clamped to available screen, scroll only
at overflow. Drag from a small top grip; resize width at the right edge.
Independent top-level detail window is never a child of the list's clipping area.
Both windows request always-on-top. No OS-specific blur is promised.
Closing hides. A small cross on each row removes that record without signalling its
workload. GNOME Ctrl+Alt+H invokes explicit show through a dedicated custom shortcut;
existing shortcuts remain intact and no login autostart is introduced. Ensure-running
respects hidden state; only explicit show reveals it.
No stop, pause or restart workload controls. Removal tombstones the monitor record;
it does not signal a process. Active removed managed jobs are still supervised
until final receipt; external removed observers detach. Purge is explicit and
restricted to finished or detached records.

## Progress contract

Explicit JSON progress via `@@HUD {json}` on stdout, CLI `progress`, or Python API.
An opt-in percent parser exists, never enabled on arbitrary output automatically.
Totals/counters or reported percentages must be finite and consistent. ETA is
estimated only from increasing measurements over time within the same phase and
total. Unknown/invalid/stale ETA is absent. A stale progress display is not a hang
warning. Percentage 100 is not proof of process completion. Logs are data, never
instructions to execute.

## Adapters

Local managed launch; local PID attach; Docker container inspection/logs; Kubernetes
Job inspection/logs; SSH managed launch and observation using this package's
headless CLI on the remote host. External producer registration/reporting is the
escape hatch for other job systems. SSH uses existing key/config credentials and
host verification; no credentials in the database, no root or cluster mutation.
Remote submit IDs are chosen before transmission; ambiguous acknowledgement never
causes automatic resubmission. Remote monitors reconnect by reading that same ID.

## Files and boundaries

`store.py`: transactional records/settings; `model.py`: progress/presentation;
`runtime.py`: detached supervision, attach observers, recovery;
`adapters.py`: bounded read-only backend operations, SSH submission;
`gui.py`: native view and independent details; `cli.py`: authoritative command API;
`notify.py`: best-effort session notifications; `api.py`: producer instrumentation;
`events.py`: durable completion/attention state and acknowledgement;
`codex_bridge.py`: authorized local relay, native delivery guard and recovery.

GUI removal supports standard Undo in the current GUI session. An explicit restore
can restart a detached observer even within the initial reconciliation grace; it
does not execute the workload. Technical GUI exit preserves saved visibility.

Private data default: $XDG_STATE_HOME/process-hud (usually ~/.local/state/process-hud).
Modes 0700/0600, per-task rotated combined stdout/stderr, capped tool responses.
The worker bootstrap import path is stripped before workload execution; preserve the
caller's original PYTHONPATH instead of injecting HUD site-packages. No secret environment dump. Command arguments and logs can still contain secrets:
never put credentials on the command line; the tool is not a secret sanitizer.

## Verification strategy

Real short Linux subprocess tests for detachment, descendants, errors, output,
progress, deletion and idempotence; fixture and executable-helper tests for external
backends; optional native Qt tests for layout/flyout/visibility. Target GNOME/X11,
actual remote SSH, Docker daemon, Kubernetes RBAC and sound need separate target
checks. See VALIDATION.md for what actually ran, not a proposed list.
