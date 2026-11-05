"""Beta configuration/preflight boundary checks; no live providers or database."""
import builtins
import json
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from conftest import TENANT,CLIENT
from web_service import hosted


@pytest.fixture
def config_env():
    return {'WEB_STORAGE_BACKEND':'postgresql','WEB_DATA_KEY':Fernet.generate_key().decode(),
            'WEB_ORIGIN':'https://beta.example.test','WEB_ENTRA_TENANT_ID':TENANT,
            'WEB_ENTRA_CLIENT_ID':CLIENT,'WEB_ENTRA_CLIENT_SECRET':'private-entra',
            'DATABASE_URL':'postgresql://private_user:private_password@db.example.test/private_db',
            'WEB_OPENAI_API_KEY':'private-ai'}


def test_config_preflight_does_not_import_driver_or_reveal_secrets(config_env,monkeypatch):
    original=builtins.__import__
    def no_driver(name,*args,**kwargs):
        if name=='psycopg' or name.startswith('psycopg.'):
            pytest.fail('Configuration checks must not import or connect a database driver')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',no_driver)
    result=hosted.configuration(config_env)
    assert result.describe()['callback_url']=='https://beta.example.test/auth/callback'
    assert result.describe()['automatic_schema_changes'] is False
    text=repr(result)+json.dumps(result.describe())
    for key in ('WEB_DATA_KEY','WEB_ENTRA_CLIENT_SECRET','WEB_OPENAI_API_KEY','DATABASE_URL'):
        assert config_env[key] not in text
    for value in ('private_user','private_password','private_db','db.example.test'):
        assert value not in text


@pytest.mark.parametrize('change',[
    {'WEB_STORAGE_BACKEND':'sqlite'}, {'WEB_INGESTION_MODE':'oracle'},
    {'WEB_ORIGIN':'http://localhost:8080'}, {'WEB_ENTRA_CLIENT_SECRET':''},
    {'WEB_OPENAI_API_KEY':''}, {'WEB_DATABASE_SCHEMA':'public'},
    {'WEB_DATABASE_SCHEMA':'information_schema'}, {'WEB_DATABASE_SCHEMA':'other;DROP'},
    {'WEB_AI_IMAGES_ENABLED':'maybe'}, {'WEB_DATABASE_SSLMODE':'disable'},
])
def test_unready_or_unsafe_hosted_configuration_is_rejected(config_env,change):
    with pytest.raises(ValueError): hosted.configuration({**config_env,**change})


def test_internal_tls_requirement_is_explicit_and_not_an_automatic_fallback(config_env):
    assert hosted.configuration(config_env).connection.sslmode=='verify-full'
    assert hosted.configuration({**config_env,'WEB_DATABASE_SSLMODE':'require'}).connection.sslmode=='require'


def test_explicit_hosted_factory_passes_separate_pg_adapter_without_notification_mode(config_env,monkeypatch):
    import web_service.server as server
    calls=[]
    def create(settings,database,identity,graph,source,sender,**kwargs):
        calls.append((settings,database,identity,graph,source,sender,kwargs))
        return 'app'
    monkeypatch.setattr(server,'create_test_server',create)
    graph,source,identity,sender=object(),object(),object(),object()
    assert hosted.create_hosted_app(config_env,identity=identity,graph=graph,source=source,
                                    sender=sender,worker=False)=='app'
    settings,db,provider,actual_graph,actual_source,actual_sender,kwargs=calls[0]
    assert db.tenant_id==TENANT and db.storage_backend=='postgresql-experimental'
    assert db.connect.describe()['schema']=='calendarsync_web'
    assert (provider,actual_graph,actual_source,actual_sender)==(identity,graph,source,sender)
    assert kwargs=={'worker':False}
