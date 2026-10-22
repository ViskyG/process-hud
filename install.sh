#!/usr/bin/env bash
set -euo pipefail
# Installs only into the current user's directories. Never launches work or enables autostart.
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BASE=${PROCESS_HUD_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/process-hud}
BIN=${PROCESS_HUD_BIN_DIR:-$HOME/.local/bin}
SKILLS=${PROCESS_HUD_SKILLS_DIR:-$HOME/.agents/skills}
MODE=ui
if [[ ${1:-} == --headless ]]; then MODE=core; shift; fi
if (( $# )); then echo 'Usage: bash install.sh [--headless]' >&2; exit 2; fi
if [[ $BASE != /* || $BIN != /* || $SKILLS != /* || $BASE == / || $BASE == "$HOME" ]]; then
  echo 'Install directories must be safe absolute paths.' >&2; exit 2
fi
umask 077
MARKER="$BASE/.process-hud-install"
if [[ -e "$BASE" && ! -d "$BASE" ]]; then
  echo 'Installation destination is not a directory.' >&2; exit 2
fi
if [[ -d "$BASE" && ! -f "$MARKER" && -n $(find "$BASE" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
  echo 'Refusing to overwrite a nonempty unrecognized installation directory.' >&2; exit 2
fi
if [[ -f "$MARKER" && $(cat -- "$MARKER") != process-hud-owned-install-v1 ]]; then
  echo 'Refusing to overwrite an unrecognized installation marker.' >&2; exit 2
fi
if [[ -x "$BASE/venv/bin/python" ]]; then
  "$BASE/venv/bin/python" - <<'PY'
import importlib.util
if importlib.util.find_spec('process_hud') is None:
    raise SystemExit(0)  # Retrying this installer's incomplete initial installation.
from process_hud.store import Store
from process_hud.util import alive
s=Store()
if alive(s.setting('ui_identity')) is True or any(alive(j.get('worker')) is True for j in s.list(True)):
    raise SystemExit('Existing HUD/observers are still running. Finish work and use process-hud ui quit before upgrading.')
PY
fi
mkdir -p -- "$BASE" "$BIN"
if [[ $MODE == ui ]]; then mkdir -p -- "$SKILLS"; fi
if [[ -e "$BIN/process-hud" && ! -L "$BIN/process-hud" ]]; then
  echo "Refusing to replace an unrelated executable: $BIN/process-hud" >&2; exit 2
fi
if [[ -L "$BIN/process-hud" && $(readlink -- "$BIN/process-hud") != "$BASE/venv/bin/process-hud" ]]; then
  echo "Refusing to replace an unrelated symlink: $BIN/process-hud" >&2; exit 2
fi
printf '%s\n' 'process-hud-owned-install-v1' > "$MARKER"
python3 -m venv "$BASE/venv"
if [[ $MODE == ui ]]; then
  "$BASE/venv/bin/python" -m pip install "$HERE[ui]"
else
  "$BASE/venv/bin/python" -m pip install "$HERE"
fi
ln -sfn -- "$BASE/venv/bin/process-hud" "$BIN/process-hud"
mkdir -p "$BASE/examples" "$BASE/docs" "$BASE/scripts"
cp -a -- "$HERE/examples/." "$BASE/examples/"
cp -a -- "$HERE/docs/." "$BASE/docs/"
cp -- "$HERE/scripts/gnome_shortcut.py" "$BASE/scripts/gnome_shortcut.py"
cp -- "$HERE/scripts/install_codex_bridge.py" "$BASE/scripts/install_codex_bridge.py"
cp -- "$HERE/uninstall.sh" "$BASE/uninstall.sh"
printf '%s\n' 'process-hud-owned-install-v1' > "$BASE/.process-hud-install"
if [[ $MODE == ui ]]; then
  if [[ -e "$SKILLS/process-hud" || -L "$SKILLS/process-hud" ]]; then
    BACKUP="$BASE/skill-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mkdir -p "$BACKUP"
    mv -- "$SKILLS/process-hud" "$BACKUP/"
    printf 'Previous skill preserved at %s\n' "$BACKUP/process-hud"
  fi
  cp -a -- "$HERE/skills/process-hud" "$SKILLS/process-hud"
  # Headless installs do not own an existing desktop skill. Keep this receipt
  # across a later UI -> headless upgrade so uninstall still preserves our copy.
  printf '%s\n' "$SKILLS/process-hud" > "$BASE/.process-hud-skill-path"
  if command -v gsettings >/dev/null && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] &&
     [[ ${XDG_CURRENT_DESKTOP:-} == *GNOME* || ${DESKTOP_SESSION:-} == ubuntu ]]; then
    "$BASE/venv/bin/python" "$BASE/scripts/gnome_shortcut.py" install --bin "$BIN/process-hud" ||
      printf 'GNOME shortcut was not installed; existing bindings were preserved.\n' >&2
  fi
fi
printf '\nInstalled: %s\n' "$BIN/process-hud"
if [[ $MODE == ui ]]; then printf 'Skill directory: %s\n' "$SKILLS/process-hud"; fi
printf 'No autostart or background tasks enabled.\n'
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) printf 'This shell PATH does not include %s. Invoke its full path or add it to PATH.\n' "$BIN" ;;
esac
"$BIN/process-hud" doctor
