#!/usr/bin/env bash
set -euo pipefail
BASE=${PROCESS_HUD_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/process-hud}
BIN=${PROCESS_HUD_BIN_DIR:-$HOME/.local/bin}
SKILLS=${PROCESS_HUD_SKILLS_DIR:-$HOME/.agents/skills}
if [[ ! -f "$BASE/.process-hud-install" ]] || [[ $(cat -- "$BASE/.process-hud-install") != process-hud-owned-install-v1 ]]; then
  echo 'Refusing to remove an unrecognized installation.' >&2; exit 2
fi
"$BASE/venv/bin/python" - <<'PY'
from process_hud.store import Store
from process_hud.util import alive
s=Store()
if alive(s.setting('ui_identity')) is True or any(alive(j.get('worker')) is True for j in s.list(True)):
    raise SystemExit('Observers or UI still running. Finish work and use process-hud ui quit. No processes were killed.')
PY
if [[ -L "$BIN/process-hud" && $(readlink -- "$BIN/process-hud") == "$BASE/venv/bin/process-hud" ]]; then
  if command -v gsettings >/dev/null && [[ -f "$BASE/scripts/gnome_shortcut.py" ]]; then
    "$BASE/venv/bin/python" "$BASE/scripts/gnome_shortcut.py" remove --bin "$BIN/process-hud" ||
      printf 'Could not remove the GNOME shortcut; inspect Keyboard Shortcuts.\n' >&2
  fi
  rm -- "$BIN/process-hud"
fi
# Preserve potentially user-edited skills rather than recursively deleting them.
if [[ -f "$BASE/.process-hud-skill-path" &&
      $(cat -- "$BASE/.process-hud-skill-path") == "$SKILLS/process-hud" &&
      ( -e "$SKILLS/process-hud" || -L "$SKILLS/process-hud" ) ]]; then
  ARCHIVE="${XDG_STATE_HOME:-$HOME/.local/state}/process-hud/skill-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "$ARCHIVE"
  mv -- "$SKILLS/process-hud" "$ARCHIVE/"
  printf 'Skill preserved: %s\n' "$ARCHIVE/process-hud"
elif [[ -e "$SKILLS/process-hud" || -L "$SKILLS/process-hud" ]]; then
  printf 'Existing skill left in place: no matching installation receipt for %s\n' "$SKILLS/process-hud"
fi
# Rename installation to a recovery directory instead of deleting code/previous backups.
ARCHIVE="${BASE}.uninstalled-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mv -- "$BASE" "$ARCHIVE"
printf 'Installation archived at %s\nTask records and logs were retained. No workload was stopped.\n' "$ARCHIVE"
