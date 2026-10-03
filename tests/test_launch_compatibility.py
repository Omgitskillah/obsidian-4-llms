import os
from pathlib import Path
import shutil
import subprocess
import sys
from obsidian_mcp import semantic


def test_nuru_layout_launch(tmp_path):
    root = tmp_path / 'repo'
    package = root / 'servers/obsidian/src/obsidian_mcp'
    shutil.copytree(Path(semantic.__file__).parent, package)
    vault = root / 'vault'
    vault.mkdir()
    (root / '.env.obsidian').write_text(f'VAULT_PATH={vault}\n')
    env = os.environ.copy()
    for key in ('VAULT_PATH', 'OBSIDIAN_ENV_FILE', 'OBSIDIAN_READ_ONLY'):
        env.pop(key, None)
    env.update(PYTHONPATH=str(package.parent), OBSIDIAN_PROTECT_USER_AUTHORED='1')
    result = subprocess.run([sys.executable, '-c', 'from obsidian_mcp import server; print(server.VAULT_PATH)'],
        cwd=root / 'servers/obsidian', env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert str(vault) in result.stdout


def test_config_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = tmp_path / '.env.obsidian'
    local.write_text('VAULT_PATH=local\n')
    monkeypatch.delenv('OBSIDIAN_ENV_FILE', raising=False)
    assert semantic._env_file() == local
    explicit = tmp_path / 'other.env'
    monkeypatch.setenv('OBSIDIAN_ENV_FILE', str(explicit))
    assert semantic._env_file() == explicit
