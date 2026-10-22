# Reliability audit — 23 October 2026

The audit covered process supervision, progress, local storage, the Codex relay,
backend observation, installation, notifications and the Qt interface. It started
from commit `907606d`. Fixes extend the existing implementation and retain the
observation-only contract.

## Confirmed defects and fixes

| Area | Reproduced problem | Fix |
| --- | --- | --- |
| Managed processes | A log write error could abandon supervision and close a running workload's stdout pipe. | Continue draining output and collecting wait status; record unavailable logs. Transient metadata-write failures do not close the workload pipe. |
| Attached processes | The final PID log snapshot could be missed between periodic log reads. A log-write error could discard terminal backend evidence. | Take a final bounded snapshot and preserve independently observed terminal evidence when log storage fails. |
| Restore | Undo could race an observer already leaving after removal and leave the restored row unobserved. | Recheck removal transactionally and hand observation to a replacement only after the previous observer releases its lifetime lock. The workload is never relaunched. |
| Command launch | Relative PATH entries were resolved against the caller's directory instead of the selected workload directory. | Resolve the executable using the workload's `--cwd`, preserving virtual-environment symlink paths. |
| Progress | Merging several stdout events could retain obsolete counters across percent/phase/indeterminate transitions; a malformed event could discard valid progress. | Apply bounded batches in order in one transaction; reject malformed individual events. |
| ETA | Changing only the total retained an obsolete ETA. Message updates could mislabel a reported ETA, and remote observation could refresh its measurement age. | Reset invalid estimates and preserve measurement time and provenance. Reject contradictory counters and percentages. |
| Identity and helpers | An explicit task ID and idempotency key could identify different records. Oversized helper input was rejected after spawning; one timeout path leaked a different exception type. | Reject conflicting identities and oversized input before execution; normalize helper timeouts. |
| Remote evidence | Inconsistent SSH terminal reports and a boolean Docker exit code could be accepted. | Validate terminal state, completion flag, exit code and protocol version without inventing an outcome. |
| Codex relay | Bidirectional RPC IDs and numeric/string IDs could collide. Optional audit/metadata I/O failures could interrupt IDE traffic. | Distinguish responses from server requests and retain ID types; make diagnostic file writes best effort. |
| Delivery | A saved accepted receipt appeared pending after its owner unloaded. Stale metadata could approve a launch for an unloaded conversation. | Read durable receipts before testing current availability; verify owner/source and instance against the live socket. |
| Relay resources | SQLite connections relied on garbage collection; failed App Server startup left a socket. | Close database connections and clean up failed-start sockets explicitly. |
| GUI | A multiline progress message could expand the details window beyond the screen and hide its actions. | Scroll metadata inside the existing compact details window. |
| Notifications | Titles with quotes or option-like prefixes were interpreted by `gdbus`. | Encode string arguments and separate options from values. |
| Installation | Headless uninstall could move a skill installed elsewhere. A non-executable Codex binary could be reported as configured. | Track ownership of the installed skill and check binary executability before changing settings. Legacy installs without a skill receipt leave the skill in place. |
| Bridge upgrade | A normal package upgrade regenerated the console entry point, which the bridge setup then rejected as a modified wrapper. | Accept the exact known pip launcher templates for the same installed interpreter; continue rejecting custom code and unrelated launchers. |

## Evidence and limits

The complete regression suite passed: **224 tests, Python 3.10, 53.96 seconds**.
Shell syntax and `git diff --check` passed. The final wheel was built with
setuptools 84.2.1 in an isolated build environment; its name/version, both console
entry points and all 13 runtime modules were checked against the source tree.
An additional scratch installation reproduced the bridge upgrade with real pip:
configure the bridge, reinstall the wheel, then run `--ensure`. The original
setup rejected the regenerated launcher; the fixed setup accepted it without
rewriting settings or adding a backup. An appended custom instruction was still
rejected. The wrapper's version request reached a fixture Codex binary.

Regression tests cover real bounded local subprocesses, deterministic failure and
race injection, a fixture App Server, offscreen Qt, and isolated installation
fixtures. Socket tests require permission to create local Unix sockets; sandbox
denial is an environment limitation, not an App Server protocol failure.

The changed details window was also exercised in the real X11 session on two
2560×1440 displays. It remained 405×370, its long text scrolled, and its action
buttons stayed inside the window. Row removal/restoration and hiding/showing were
checked in a separate temporary store. A captured window image was inspected.
These were demonstration records, not live user workloads.

Notification argument handling was exercised with native `gdbus` on a private
D-Bus session for quotes, Unicode, line breaks and an option-like title. This
does not constitute a new audible-sound or desktop-banner test.

The relay checks use a deterministic App Server fixture and do not invoke a model.
Earlier live continuation, subagent and idle-token evidence remains documented in
[VALIDATION.md](VALIDATION.md); it is not presented as a fresh live test of this
revision. No new Docker/Kubernetes/SSH infrastructure was created, and real remote
backend execution, macOS and Windows were not tested in this audit.

Persistent failure of the state database can still prevent a final receipt from
being saved. Log-write resilience is not a guarantee of recovery from every
storage or operating-system failure; missing durable evidence stays unknown.

Updating a running installation remains blocked by the installer while its GUI or
observers are active. Repository test results do not mean that an already-running
GUI, observer or Codex relay has loaded the new code.
