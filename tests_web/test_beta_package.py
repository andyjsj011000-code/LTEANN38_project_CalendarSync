import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile


def test_release_is_clean_and_extracted_launcher_reopens_persistent_data(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('beta_build_release', repo / 'beta' / 'build_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = module.build()
    original_bytes = archive.read_bytes()
    assert module.build().read_bytes() == original_bytes
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
