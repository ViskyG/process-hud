"""Own one GNOME shortcut without replacing any existing user bindings."""
from __future__ import annotations

import argparse
import ast
import re
import shlex
import subprocess
import sys

BASE = 'org.gnome.settings-daemon.plugins.media-keys'
PATH = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/process-hud/'
SCHEMA = BASE + '.custom-keybinding:' + PATH
BINDING = '<Primary><Alt>h'


def settings(*args):
    result = subprocess.run(['gsettings', *args], capture_output=True, text=True, timeout=4)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'gsettings failed')
    return result.stdout.strip()


def value(key):
    return ast.literal_eval(settings('get', SCHEMA, key))


def paths():
    raw = settings('get', BASE, 'custom-keybindings')
    return ast.literal_eval(raw.removeprefix('@as '))


def normalized(binding):
    mods = re.findall(r'<([^>]+)>', binding)
    aliases = {'primary': 'control', 'ctrl': 'control'}
    mods = {aliases.get(mod.lower(), mod.lower()) for mod in mods}
    key = re.sub(r'<[^>]+>', '', binding).lower()
    return frozenset(mods), key


def check_conflicts(existing_paths):
    wanted = normalized(BINDING)
    for path in existing_paths:
        if path != PATH:
            current = ast.literal_eval(settings('get', BASE + '.custom-keybinding:' + path, 'binding'))
            if normalized(current) == wanted:
                raise RuntimeError(f'Shortcut already belongs to {path}')
    for schema in (BASE, 'org.gnome.desktop.wm.keybindings', 'org.gnome.shell.keybindings'):
        try:
            output = settings('list-recursively', schema)
        except RuntimeError:
            continue
        for candidate in re.findall(r"'([^']+)'", output):
            if normalized(candidate) == wanted:
                raise RuntimeError(f'Shortcut already belongs to GNOME schema {schema}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'remove', 'status'))
    parser.add_argument('--bin', required=True, help='Absolute path to process-hud executable')
    args = parser.parse_args()
    command = shlex.quote(args.bin) + ' ui show'
    existing_paths = paths()
    current_command = value('command')
    if args.action == 'status':
        print(f"installed={PATH in existing_paths and current_command == command} binding={value('binding')!r}")
    elif args.action == 'install':
        if current_command and current_command != command:
            raise RuntimeError('Dedicated Process HUD shortcut path is already in use')
        if not current_command:
            check_conflicts(existing_paths)
            settings('set', SCHEMA, 'name', repr('Process HUD: показать'))
            settings('set', SCHEMA, 'command', repr(command))
            settings('set', SCHEMA, 'binding', repr(BINDING))
        if PATH not in existing_paths:
            settings('set', BASE, 'custom-keybindings', repr([*existing_paths, PATH]))
        print(f'Process HUD shortcut: Ctrl+Alt+H ({value("binding")})')
    elif PATH in existing_paths and current_command == command:
        settings('set', BASE, 'custom-keybindings', repr([p for p in existing_paths if p != PATH]))
        settings('reset-recursively', SCHEMA)
        print('Process HUD shortcut removed; other GNOME shortcuts preserved')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'Process HUD shortcut: {exc}', file=sys.stderr)
        raise SystemExit(2)
