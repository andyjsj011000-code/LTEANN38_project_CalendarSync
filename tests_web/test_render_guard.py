import pytest

from web_service.render_guard import ensure_hosted_storage_supported
from web_service.server import create_test_server


def test_local_storage_remains_available():
    ensure_hosted_storage_supported({})


@pytest.mark.parametrize('env', [
    {'RENDER': 'true'},
    {'DATABASE_URL': 'postgresql://example'},
    {'RENDER': 'true', 'DATABASE_URL': 'postgresql://example'},
])
def test_hosted_storage_is_rejected_until_implemented(env):
    with pytest.raises(RuntimeError, match='not implemented'):
        ensure_hosted_storage_supported(env)


def test_server_checks_before_reading_settings(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    with pytest.raises(RuntimeError, match='PostgreSQL storage is not implemented'):
        create_test_server()
