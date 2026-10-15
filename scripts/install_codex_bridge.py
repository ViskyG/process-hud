#!/usr/bin/env python3
"""Install the optional relay into the existing HUD venv, preserving dependencies.

Backups stay outside the source tree: VS Code settings may contain private data.
No editor reload, workload launch, login service or model request happens here.
"""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def mask_comments(text):
    # Preserve offsets and all quoted strings while masking JSONC comments.
    pattern = r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/'
    return re.sub(pattern, lambda m: m[0] if m[0].startswith('"') else
                  ''.join('\n' if c == '\n' else ' ' for c in m[0]), text)


def setting(text, value):
    masked = mask_comments(text)
    pattern = r'"chatgpt\.cliExecutable"\s*:\s*("(?:\\.|[^"\\])*"|null)'
    matches = list(re.finditer(pattern, masked))
    if len(matches) > 1:
        raise ValueError('Duplicate cliExecutable settings; resolve before installing')
    if matches:
        match = matches[0]
        return text[:match.start(1)]+json.dumps(value)+text[match.end(1):]
    opening = masked.find('{')
    if opening < 0:
        raise ValueError('No settings JSON object')
    rest = masked[opening+1:].strip()
    comma = ',' if not rest.startswith('}') else ''
    return text[:opening+1]+'\n  "chatgpt.cliExecutable": '+json.dumps(value)+comma+text[opening+1:]


def main():
    os.umask(0o077)
    source = Path(__file__).resolve().parents[1]
    base = Path.home()/'.local/share/process-hud'
    venv = base/'venv'
    python = venv/'bin/python'
    if not (base/'.process-hud-install').is_file() or not python.is_file():
        raise ValueError('Install Process HUD normally first')
    site = Path(subprocess.check_output([str(python), '-c',
                'import process_hud;print(process_hud.__path__[0])'], text=True).strip())
    extensions = Path.home()/'.vscode/extensions'
    matches = [x for x in json.loads((extensions/'extensions.json').read_text())
               if x.get('identifier', {}).get('id') == 'openai.chatgpt']
    if len(matches) != 1:
        raise ValueError('Expected one active Codex extension')
    real = extensions/matches[0]['relativeLocation']/'bin/linux-x86_64/codex'
    if not real.is_file():
        raise ValueError('Bundled Codex binary is missing')
    settings = Path.home()/'.config/Code/User/settings.json'
    original = settings.read_text() if settings.exists() else '{}\n'
    wrapper = venv/'bin/process-hud-codex'
    updated = setting(original, str(wrapper))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = base/'bridge-backups'/stamp
    backup.mkdir(parents=True, mode=0o700)
    modules = sorted((source/'src/process_hud').glob('*.py'))
    targets = [site/module.name for module in modules]
    skill = Path.home()/'.agents/skills/process-hud/SKILL.md'
    targets += [settings, wrapper, base/'codex-bridge.json', skill]
    manifest = []
    for i, target in enumerate(targets):
        exists = target.exists()
        if exists:
            shutil.copy2(target, backup/str(i))
        manifest.append({'target': str(target), 'backup': str(i) if exists else None})
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    for target in targets[:len(modules)]:
        tmp = target.with_name(target.name+'.bridge-new')
        shutil.copy2(source/'src/process_hud'/target.name, tmp)
        os.replace(tmp, target)
    skill.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source/'skills/process-hud/SKILL.md', skill)
    wrapper.write_text('#!'+str(python)+'\nfrom process_hud.codex_bridge import main\nraise SystemExit(main())\n')
    wrapper.chmod(0o755)
    (base/'codex-bridge.json').write_text(json.dumps({'real_binary': str(real),
                                                  'extension_root': str(extensions)}, indent=2)+'\n')
    (base/'codex-bridge.json').chmod(0o600)
    tmp = settings.with_name('settings.json.hud-new')
    tmp.write_text(updated)
    tmp.chmod(settings.stat().st_mode & 0o777 if settings.exists() else 0o600)
    os.replace(tmp, settings)
    print(json.dumps({'installed': str(wrapper), 'backup': str(backup),
                      'real_binary': str(real), 'reload_required': True}, indent=2))


if __name__ == '__main__':
    main()
