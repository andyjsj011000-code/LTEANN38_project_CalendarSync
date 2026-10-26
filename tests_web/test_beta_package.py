import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


def test_release_is_clean_and_extracted_launcher_reopens_persistent_data(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('beta_build_release', repo / 'beta' / 'build_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = module.build(tmp_path / "releases")
    original_bytes = archive.read_bytes()
    assert module.build(tmp_path / "releases").read_bytes() == original_bytes
    extracted = tmp_path / 'extracted'
    with zipfile.ZipFile(archive) as package:
        assert len(package.namelist()) == 17
        assert not any('var/' in p or p.endswith(('.sqlite3', 'config.json', 'invitations.json')) for p in package.namelist())
        assert b'\r\n' in package.read('start_beta.cmd')
        package.extractall(extracted)
    command = [sys.executable, str(extracted / 'start_beta.py'), '--current-python', '--no-browser', '--prepare-only']
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, cwd=extracted)
    assert result.returncode == 0, result.stderr
    assert 'http://127.0.0.1:8080' in result.stdout
    assert all('Tester ' + str(i) in result.stdout for i in range(1, 4))
    config = extracted / 'var' / 'data' / 'config.json'
    invitations = config.with_name('invitations.json')
    before = config.read_bytes(), invitations.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, cwd=extracted)
    assert result.returncode == 0, result.stderr
    assert before == (config.read_bytes(), invitations.read_bytes())
    assert len(json.loads(invitations.read_text(encoding='utf-8'))) == 3


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows launcher regression')
@pytest.mark.parametrize('missing_files', [True, False])
def test_windows_launcher_explains_missing_companions_before_python(tmp_path, missing_files):
    repo = Path(__file__).resolve().parents[1]
    launcher = tmp_path / 'start_beta.cmd'
    # Simulate opening only the CMD file from inside the ZIP. No Python script
    # should be attempted, even on a computer with no Python on PATH.
    content = (repo / 'beta/start_beta.cmd').read_text(encoding='utf-8')
    launcher.write_bytes(content.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8'))
    if not missing_files:
        (tmp_path / 'start_beta.py').write_text('raise RuntimeError("must not run")')
        (tmp_path / 'requirements-beta.txt').write_text('')
        (tmp_path / 'web_service').mkdir()
        (tmp_path / 'web_service/beta.py').write_text('')
    system32 = Path(os.environ['SystemRoot']) / 'System32'
    env = {**os.environ, 'PATH': str(system32)}
    result = subprocess.run([str(system32 / 'cmd.exe'), '/d', '/c', str(launcher)], cwd=tmp_path, env=env, input=b'Q', capture_output=True, timeout=15)
    output = result.stdout.decode('utf-8', errors='replace')
    assert result.returncode != 0
    assert 'R:' in output and 'Q:' in output
    if missing_files:
        assert '모두 압축 풀기' in output
        assert 'Python 3.11' not in output
    else:
        assert 'Python 3.11' in output
        assert 'Add Python to PATH' in output
    assert "can't open file" not in output and 'must not run' not in output
