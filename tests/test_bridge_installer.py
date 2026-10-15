"""Test idempotent first-use bridge setup without a real VS Code installation."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "install_codex_bridge.py"


def load_installer(path):
    spec = importlib.util.spec_from_file_location("testable_hud_bridge_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_installation(tmp_path, monkeypatch):
    home = tmp_path / "user"
    base = home / ".local/share/process-hud"
    script = base / "scripts/install_codex_bridge.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SOURCE, script)

    (base / ".process-hud-install").write_text("process-hud-owned-install-v1")
    py = base / "venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("#!test python\n")

    installed_skill = home / ".agents/skills/process-hud/SKILL.md"
    installed_skill.parent.mkdir(parents=True)
    installed_skill.write_text("existing HUD skill\n")

    extensions = home / ".vscode/extensions"
    client = extensions / "openai.chatgpt-1/bin/linux-x86_64/codex"
    client.parent.mkdir(parents=True)
    client.write_text("fake Codex\n")
    (extensions / "extensions.json").write_text(
        json.dumps([{"identifier": {"id": "openai.chatgpt"},
                     "relativeLocation": "openai.chatgpt-1"}])
    )

    settings = home / ".config/Code/User/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{\n  // preserve my comment\n  "editor.tabSize": 4\n}\n')

    site = home / "fake-venv-site/process_hud"
    site.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    module = load_installer(script)
    monkeypatch.setattr(module.subprocess, "check_output",
                        lambda *args, **kwargs: str(site) + "\n")
    monkeypatch.setattr(sys, "argv", [str(script), "--ensure"])
    return module, base, settings, installed_skill


def test_installed_copy_bootstraps_once_without_source_checkout(tmp_path, monkeypatch, capsys):
    installer, base, settings, skill = fixture_installation(tmp_path, monkeypatch)
    assert not (base / "src").exists()

    installer.main()
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "configured"
    assert first["reload_required"] is True
    assert (base / "venv/bin/process-hud-codex").is_file()
    assert (base / "codex-bridge.json").is_file()
    assert skill.read_text() == "existing HUD skill\n"
    assert "// preserve my comment" in settings.read_text()
    assert installer.current_cli_executable(settings.read_text()) == str(
        base / "venv/bin/process-hud-codex"
    )
    backups = list((base / "bridge-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").is_file()

    settings_before = settings.read_text()
    installer.main()
    again = json.loads(capsys.readouterr().out)
    assert again["status"] == "already_configured"
    assert again["reload_required"] is False
    assert settings.read_text() == settings_before
    assert list((base / "bridge-backups").iterdir()) == backups


def test_first_use_refuses_to_override_other_codex_executable(tmp_path, monkeypatch):
    installer, base, settings, _ = fixture_installation(tmp_path, monkeypatch)
    settings.write_text('{"chatgpt.cliExecutable": "/custom/codex"}\n')

    with pytest.raises(ValueError, match="another executable"):
        installer.main()
    assert not (base / "bridge-backups").exists()
    assert installer.current_cli_executable(settings.read_text()) == "/custom/codex"


def test_jsonc_setting_parser_ignores_commented_out_values():
    installer = load_installer(SOURCE)
    sample = ('{\n // "chatgpt.cliExecutable": "/disabled",\n'
              ' "chatgpt.cliExecutable": "/the/actual/binary"\n}')
    assert installer.current_cli_executable(sample) == "/the/actual/binary"
    assert installer.current_cli_executable('{}') is None


def test_jsonc_nested_setting_is_preserved_and_root_setting_is_added():
    installer = load_installer(SOURCE)
    sample = '{"extension.data": {"chatgpt.cliExecutable": "/nested"}}'
    assert installer.current_cli_executable(sample) is None
    updated = json.loads(installer.setting(sample, '/wrapper'))
    assert updated['extension.data']['chatgpt.cliExecutable'] == '/nested'
    assert updated['chatgpt.cliExecutable'] == '/wrapper'


@pytest.mark.parametrize('value', ['false', '17', '[]', '{}'])
def test_jsonc_wrong_setting_type_is_refused(value):
    installer = load_installer(SOURCE)
    sample = '{"chatgpt.cliExecutable": '+value+'}'
    with pytest.raises(ValueError, match='string or null'):
        installer.setting(sample, '/wrapper')


@pytest.mark.parametrize('sample', ['{"editor.fontSize": 14, broken }', '[]',
                                   '{"chatgpt.cliExecutable": null,"chatgpt.cliExecutable": "/second"}'])
def test_invalid_or_duplicate_jsonc_is_refused(sample):
    installer = load_installer(SOURCE)
    with pytest.raises(ValueError):
        installer.setting(sample, '/wrapper')


def test_jsonc_comments_trailing_commas_and_escaped_keys():
    installer = load_installer(SOURCE)
    sample = ('{ /* keep */ "extension.data": {"items": [1, 2,],},\n'
              ' "chatgpt.cli\\u0045xecutable": null, // keep too\n}')
    updated = installer.setting(sample, '/wrapper')
    assert '/* keep */' in updated and '// keep too' in updated
    assert installer.current_cli_executable(updated) == '/wrapper'
    assert updated.count('cli\\u0045xecutable') == 1


def test_first_use_creates_missing_settings_directory(tmp_path, monkeypatch, capsys):
    installer, base, settings, _ = fixture_installation(tmp_path, monkeypatch)
    shutil.rmtree(settings.parent)
    installer.main()
    assert json.loads(capsys.readouterr().out)['status'] == 'configured'
    assert settings.is_file()


@pytest.mark.parametrize('damage', ['config-json', 'binary', 'wrapper', 'permissions', 'marker'])
def test_ensure_does_not_report_damaged_installation_as_ready(tmp_path, monkeypatch, capsys, damage):
    installer, base, settings, _ = fixture_installation(tmp_path, monkeypatch)
    installer.main()
    capsys.readouterr()
    if damage == 'config-json':
        (base/'codex-bridge.json').write_text('{broken}')
    elif damage == 'binary':
        (base/'codex-bridge.json').write_text('{"real_binary": "/missing/codex"}')
    elif damage == 'wrapper':
        (base/'venv/bin/process-hud-codex').write_text('#!/bin/sh\nexit 0\n')
    elif damage == 'permissions':
        (base/'venv/bin/process-hud-codex').chmod(0o600)
    else:
        (base/'.process-hud-install').write_text('unrelated installation')
    before = settings.read_bytes()
    with pytest.raises(ValueError):
        installer.main()
    assert settings.read_bytes() == before
    assert len(list((base/'bridge-backups').iterdir())) == 1


def test_backup_manifest_can_restore_the_previous_files(tmp_path, monkeypatch, capsys):
    installer, base, settings, skill = fixture_installation(tmp_path, monkeypatch)
    settings_before, skill_before = settings.read_bytes(), skill.read_bytes()
    installer.main()
    backup = Path(json.loads(capsys.readouterr().out)['backup'])
    for entry in json.loads((backup/'manifest.json').read_text()):
        target = Path(entry['target'])
        if entry['backup'] is None:
            target.unlink()
        else:
            shutil.copy2(backup/entry['backup'], target)
    assert settings.read_bytes() == settings_before
    assert skill.read_bytes() == skill_before
    assert not (base/'codex-bridge.json').exists()
    assert not (base/'venv/bin/process-hud-codex').exists()


def test_ensure_follows_active_extension_after_update(tmp_path, monkeypatch, capsys):
    installer, base, settings, _ = fixture_installation(tmp_path, monkeypatch)
    installer.main()
    capsys.readouterr()
    config_file = base/'codex-bridge.json'
    config = json.loads(config_file.read_text())
    config['real_binary'] = '/removed/previous-extension/codex'
    config_file.write_text(json.dumps(config))
    installer.main()
    assert json.loads(capsys.readouterr().out)['status'] == 'already_configured'
