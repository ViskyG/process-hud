"""Shell installer ownership checks, with venv/pip stubbed (no downloads or UI)."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def install_env(tmp_path):
    bootstrap = tmp_path/'bootstrap'
    bootstrap.mkdir()
    python = bootstrap/'python3'
    python.write_text(
        '#!'+sys.executable+'\n'
        'from pathlib import Path\n'
        'import sys\n'
        'assert sys.argv[1:3] == ["-m", "venv"]\n'
        'bin = Path(sys.argv[3])/"bin"\n'
        'bin.mkdir(parents=True, exist_ok=True)\n'
        'for name in ("python", "process-hud"):\n'
        '    p = bin/name\n'
        '    p.write_text("#!/bin/sh\\nexit 0\\n")\n'
        '    p.chmod(0o700)\n'
    )
    python.chmod(0o700)
    return dict(os.environ, PATH=str(bootstrap)+os.pathsep+os.environ['PATH'],
                PROCESS_HUD_INSTALL_ROOT=str(tmp_path/'installation'),
                PROCESS_HUD_BIN_DIR=str(tmp_path/'bin'),
                PROCESS_HUD_SKILLS_DIR=str(tmp_path/'skills'),
                XDG_STATE_HOME=str(tmp_path/'state'), DISPLAY='', WAYLAND_DISPLAY='',
                XDG_CURRENT_DESKTOP='', DESKTOP_SESSION='')


def run_script(name, env, *args):
    return subprocess.run(['bash', str(ROOT/name), *args], env=env,
                          capture_output=True, text=True, check=True)


def test_headless_uninstall_leaves_an_existing_skill_in_place(install_env):
    skill = Path(install_env['PROCESS_HUD_SKILLS_DIR'])/'process-hud/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('user-owned skill\n')
    run_script('install.sh', install_env, '--headless')
    result = run_script('uninstall.sh', install_env)
    assert skill.read_text() == 'user-owned skill\n'
    assert 'no matching installation receipt' in result.stdout
    assert not list(Path(install_env['XDG_STATE_HOME']).rglob('SKILL.md'))


@pytest.mark.parametrize('headless_upgrade', [False, True])
def test_ui_skill_is_archived_including_after_headless_upgrade(install_env, headless_upgrade):
    run_script('install.sh', install_env)
    if headless_upgrade:
        run_script('install.sh', install_env, '--headless')
    skill = Path(install_env['PROCESS_HUD_SKILLS_DIR'])/'process-hud/SKILL.md'
    skill.write_text(skill.read_text()+'\nUser annotation kept in archive.\n')
    run_script('uninstall.sh', install_env)
    assert not skill.exists()
    archived = list(Path(install_env['XDG_STATE_HOME']).rglob('SKILL.md'))
    assert len(archived) == 1
    assert 'User annotation kept in archive.' in archived[0].read_text()


def test_legacy_installation_without_receipt_keeps_its_skill(install_env):
    run_script('install.sh', install_env)
    (Path(install_env['PROCESS_HUD_INSTALL_ROOT'])/'.process-hud-skill-path').unlink()
    skill = Path(install_env['PROCESS_HUD_SKILLS_DIR'])/'process-hud/SKILL.md'
    before = skill.read_bytes()
    run_script('uninstall.sh', install_env)
    assert skill.read_bytes() == before
