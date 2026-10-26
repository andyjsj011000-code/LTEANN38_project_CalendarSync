from types import SimpleNamespace
import sys
import pytest
from requests.exceptions import Timeout, ConnectionError, HTTPError
from conftest import ALICE, TENANT
from test_server import server
from web_service.connections import PersistentIdentity
from web_service.school_graph import GraphFailure


@pytest.mark.parametrize('failure', [Timeout('private endpoint'), ConnectionError('private host'), HTTPError(response=SimpleNamespace(status_code=503)), GraphFailure('refresh_unavailable')])
def test_transient_refresh_preserves_credentials_and_retries(server, monkeypatch, failure):
    client=server.client()
    session=server.db.session(client.cookies.get('__Host-calendarsync_session'))
    device=server.db.session_device(session)
    original=server.db.unseal(device['credential_blob'])
    refresh=server.identity.refresh
    def unavailable(*args): raise failure
    monkeypatch.setattr(server.identity,'refresh',unavailable)
    server.now[0]+=3550
    server.integration.run_sync(device)
    current=server.db.device(device['id_hash'])
    assert server.db.unseal(current['credential_blob'])==original
    assert current['state']=='connected'
    assert server.db.sync_status(session['user_id'])['state']=='refresh_unavailable'
    assert not server.db.channels(session['user_id'],TENANT)
    with pytest.raises(GraphFailure,match='refresh_busy'):
        server.integration.tokens.acquire(device)
    monkeypatch.setattr(server.identity,'refresh',refresh)
    server.now[0]+=61
    server.integration.run_sync(device)
    assert server.db.channels(session['user_id'],TENANT)
    assert server.db.unseal(server.db.device(device['id_hash'])['credential_blob'])['token']=='new-private-token'


@pytest.mark.parametrize('code,expected',[('temporarily_unavailable','refresh_unavailable'),('server_error','refresh_unavailable'),('invalid_grant','reauth_required'),('interaction_required','reauth_required')])
def test_refresh_classifies_msal_error_response(server,monkeypatch,code,expected):
    class Cache:
        def deserialize(self,value): pass
    class App:
        def __init__(self,*args,**kwargs): pass
        def get_accounts(self): return [{'local_account_id':ALICE,'realm':TENANT}]
        def acquire_token_silent_with_error(self,*args,**kwargs): return {'error':code,'error_description':'private details'}
    monkeypatch.setitem(sys.modules,'msal',SimpleNamespace(SerializableTokenCache=Cache,ConfidentialClientApplication=App))
    with pytest.raises(GraphFailure,match=expected): PersistentIdentity(server.settings).refresh('cache',ALICE,TENANT)
