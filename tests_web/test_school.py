from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import TENANT, CLIENT, ALICE, BOB
from web_service.config import Settings
from web_service.school import create_school_app, SchoolIdentity
from web_service.school_storage import SchoolDatabase
from web_service.school_graph import DelegatedGraph, GraphFailure, SCOPES
from web_service.oracle_source import OracleSource, OracleConfig, NoSource
from web_service.storage import Missing, digest

class Identity:
    def __init__(self,clock):
        self.clock=clock; self.oid=ALICE; self.overrides={}
    def begin(self):
        return {"state":"state","auth_uri":"https://login.microsoftonline.com/authorize?state=state","nonce":"private-nonce"}
    def finish(self,flow,query):
        if query!={"code":"valid","state":"state"} or flow["state"]!="state":
            raise ValueError()
        return {"tid":TENANT,"oid":self.oid,"iss":f"https://login.microsoftonline.com/{TENANT}/v2.0","aud":CLIENT,"sub":"subject","exp":self.clock()+3600,"name":"Student","preferred_username":"student@school.test","_graph":{"token":"secret-graph-token","expires_in":3600},**self.overrides}

class Graph:
    def __init__(self):
        self.available=True; self.denied=False; self.member=True; self.calls=0
    def assert_member(self,token,oid):
        if not self.member: raise GraphFailure("account_not_allowed")
    def channels(self,token,oid,tenant):
        self.calls+=1
        if not self.available: raise GraphFailure()
        return [] if self.denied or oid==BOB else [{"id":"channel-a","name":"Authorized class","team_id":CLIENT}]

class Source:
    configured=True
    def __init__(self): self.available=True; self.empty=False
    def events(self,channels,tenant):
        if not self.available: raise RuntimeError("must never leak DB secret")
        if not channels or self.empty: return []
        return [{"id":"event-a","channel_id":"channel-a","title":"Private school event","notes":"secret notes","start_date":"2026-10-20","end_date":"2026-10-26","time":"18:00","kind":"assignment","source_url":"https://teams.microsoft.com/l/message/channel-a/1"}]

@pytest.fixture
def school(tmp_path):
    now=[int(datetime(2026,10,22,tzinfo=timezone.utc).timestamp())]
    settings=Settings(database_path=tmp_path/'school.sqlite3',data_key=Fernet.generate_key().decode(),origin='https://calendar.example.test',tenant_id=TENANT,client_id=CLIENT,client_secret='secret',session_seconds=3600)
    db=SchoolDatabase(settings.database_path,settings.data_key,clock=lambda:now[0]); graph=Graph(); source=Source(); identity=Identity(db.clock)
    app=create_school_app(settings,db,identity,graph,source)
    clients=[]
    def client(oid=ALICE):
        c=TestClient(app,base_url=settings.origin); clients.append(c)
        if oid:
            identity.oid=oid
            assert c.get('/auth/login',follow_redirects=False).status_code==303
            assert c.get('/auth/callback?code=valid&state=state',follow_redirects=False).headers['location']=='/'
            c.headers['x-csrf-token']=c.get('/api/me').json()['csrf_token']
        return c
    yield SimpleNamespace(now=now,settings=settings,db=db,graph=graph,source=source,identity=identity,app=app,client=client)
    for c in clients: c.close()


def test_real_ui_is_separate_from_invitation_beta(school):
    c=school.client(None)
    assert 'data-mode="school"' in c.get('/').text
    assert c.get('/api/service').json()['school_login']
    assert c.get('/api/beta/info').status_code==404
    assert c.post('/auth/beta',json={'code':'anything'}).status_code==404
    for path in ('/docs','/openapi.json','/assets/../school.py','/assets/config.json'):
        assert c.get(path).status_code==404


def test_school_host_bound_cookies_token_encryption_and_identity(school):
    c=school.client(); cookie=c.cookies.get('__Host-calendarsync_session')
    assert cookie and 'secret-graph-token' not in cookie
    session=school.db.session(cookie)
    assert session['object_id']==ALICE
    assert b'secret-graph-token' not in school.db.path.read_bytes()
    r=c.get('/auth/login',follow_redirects=False)
    assert all(part in r.headers['set-cookie'] for part in ('__Host-calendarsync_login','HttpOnly','Secure','SameSite=lax','Path=/'))
    assert 'private-nonce' not in r.headers['set-cookie']


@pytest.mark.parametrize('change',[{'tid':BOB},{'aud':BOB},{'iss':'https://evil.test'},{'oid':'forged'},{'exp':0},{'_graph':{} }])
def test_forged_identity_cannot_create_school_session(school,change):
    c=school.client(None); school.identity.overrides=change
    c.get('/auth/login',follow_redirects=False)
    assert c.get('/auth/callback?code=valid&state=state',follow_redirects=False).headers['location']=='/?login=failed'
    assert c.get('/api/me').status_code==401


def test_guest_membership_is_required_before_session(school):
    c=school.client(None); school.graph.member=False
    c.get('/auth/login',follow_redirects=False)
    assert c.get('/auth/callback?code=valid&state=state',follow_redirects=False).headers['location']=='/?login=failed'
    with school.db.connect() as db:
        assert db.execute('SELECT count(*) FROM web_sessions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM web_users').fetchone()[0]==0


def test_cookie_less_callback_and_replay_fail(school):
    c=school.client(None)
    assert c.get('/auth/callback?code=valid&state=state',follow_redirects=False).headers['location']=='/?login=failed'
    c=school.client()
    assert c.get('/auth/callback?code=valid&state=state',follow_redirects=False).headers['location']=='/?login=failed'


def test_channel_visibility_is_verified_for_each_user(school):
    a=school.client(ALICE); b=school.client(BOB)
    assert a.get('/api/today').json()['teams'][0]['title']=='Private school event'
    assert b.get('/api/today').json()['teams']==[]
    assert b.patch('/api/events/oracle:event-a/state',json={'hidden':True}).status_code==404
    task=a.post('/api/tasks',json={'title':'private task','start_date':'2026-10-22'}).json()
    assert b.get('/api/tasks/'+task['id']).status_code==404


def test_revoked_channel_access_disappears_after_bounded_proof(school):
    c=school.client(); assert c.get('/api/today').json()['teams']
    school.graph.denied=True; school.now[0]+=61
    assert c.get('/api/today').json()['teams']==[]
    assert c.patch('/api/events/oracle:event-a/state',json={'completed':True}).status_code==404


@pytest.mark.parametrize('failure',['graph','source'])
def test_fail_closed_does_not_reuse_stale_access_but_preserves_personal_tasks(school,failure):
    c=school.client(); c.post('/api/tasks',json={'title':'my task','start_date':'2026-10-22'})
    assert c.get('/api/today').json()['teams']
    getattr(school,failure).available=False; school.now[0]+=61
    response=c.get('/api/today'); assert response.status_code==200
    assert response.json()['teams']==[] and response.json()['personal'][0]['title']=='my task'
    assert 'must never leak' not in c.get('/api/settings').text


def test_no_source_never_claims_live_ingestion(school):
    school.app.state.integration.source=NoSource(); c=school.client()
    assert c.get('/api/today').json()['teams']==[]
    settings=c.get('/api/settings').json()
    assert not settings['capabilities']['live_teams_ingestion']
    assert settings['connection']['state']=='source_not_configured'


def test_idle_and_token_expiry_revoke_sessions_and_tokens(school):
    c=school.client(); raw=c.cookies.get('__Host-calendarsync_session')
    school.now[0]+=901
    assert c.get('/api/me').status_code==401
    with school.db.connect() as db:
        assert db.execute('SELECT 1 FROM web_school_tokens WHERE session_hash=?',(digest(raw),)).fetchone() is None
    d=school.client(); school.now[0]+=3601
    assert d.get('/api/me').status_code==401


def test_logout_all_revokes_other_devices_and_inflight_snapshot(school):
    a=school.client(); b=school.client(); session=school.db.session(a.cookies.get('__Host-calendarsync_session'))
    a.get('/api/today')
    assert a.post('/auth/logout-all',headers={'x-csrf-token':'bad'}).status_code==403
    assert a.post('/auth/logout-all').status_code==204
    assert b.get('/api/me').status_code==401
    with pytest.raises(Missing): school.db.apply_snapshot(session,[],[],60)
    with school.db.connect() as db:
        assert db.execute('SELECT count(*) FROM web_school_tokens').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM web_channel_access').fetchone()[0]==0


def test_security_headers_host_origin_and_request_size(school):
    c=school.client()
    r=c.get('/')
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert "script-src 'self'" in r.headers['content-security-policy']
    assert r.headers['x-frame-options']=='DENY' and r.headers['strict-transport-security']=='max-age=31536000'
    assert c.get('/api/me',headers={'host':'evil.test'}).status_code==400
    assert c.get('/api/me',headers={'sec-fetch-site':'cross-site'}).status_code==403
    assert c.post('/api/tasks',headers={'origin':'https://evil.test'},json={}).status_code==403
    assert c.post('/api/tasks',content=b'x'*65537).status_code==413
    def chunks(): yield b'x'*40000; yield b'x'*40000
    assert c.post('/api/tasks',content=chunks()).status_code==413
    r=c.post('/api/tasks',json={'title':'SECRET_INPUT','start_date':'invalid'})
    assert r.status_code==422 and 'SECRET_INPUT' not in r.text


def test_shared_login_rate_limit_and_no_client_forwarded_ip_trust(school):
    c=school.client(None)
    for _ in range(20): assert c.get('/auth/login',follow_redirects=False).status_code==303
    assert c.get('/auth/login',headers={'x-forwarded-for':'new-client'},follow_redirects=False).status_code==429


def test_school_app_requires_configuration_and_explicit_local_development(school):
    with pytest.raises(ValueError): create_school_app(replace(school.settings,tenant_id='',client_id='',client_secret=''))
    with pytest.raises(ValueError): create_school_app(replace(school.settings,origin='http://localhost:8080'))
    assert 'secret' not in repr(school.settings) and school.settings.data_key not in repr(school.settings)


def test_graph_denied_private_channel_and_foreign_page_are_not_permission_proofs():
    requests=[]
    def handler(request):
        requests.append(str(request.url))
        if request.url.path=='/v1.0/me': return httpx.Response(200,json={'id':ALICE,'userType':'Member'})
        if request.url.path.endswith('/joinedTeams'): return httpx.Response(200,json={'value':[{'id':CLIENT}]})
        if request.url.path.endswith('/allChannels'): return httpx.Response(200,json={'value':[{'id':'allowed','displayName':'Class'},{'id':'private'},{'id':'foreign','tenantId':BOB}]})
        if '/private/' in request.url.path: return httpx.Response(403)
        return httpx.Response(200,json={'value':[]})
    graph=DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(handler)))
    assert [x['id'] for x in graph.channels('token',ALICE,TENANT)]==['allowed']
    assert not any('/foreign/' in x for x in requests)
    def malicious(request):
        if request.url.path=='/v1.0/me': return httpx.Response(200,json={'id':ALICE,'userType':'Member'})
        return httpx.Response(200,json={'value':[],'@odata.nextLink':'https://evil.test/v1.0/me/joinedTeams'})
    graph=DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(malicious)))
    with pytest.raises(GraphFailure): graph.channels('secret',ALICE,TENANT)


@pytest.mark.parametrize('profile',[{'id':BOB,'userType':'Member'},{'id':ALICE,'userType':'Guest'},{'id':ALICE}])
def test_graph_rejects_guest_missing_membership_and_wrong_user(profile):
    graph=DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=profile))))
    with pytest.raises(GraphFailure): graph.assert_member('token',ALICE)


def test_msal_school_flow_requires_delegated_scopes_and_no_offline_access(monkeypatch,school):
    import sys
    calls={}
    class ClientMock:
        def __init__(self,*args,**kwargs): calls['construction']=kwargs
        def initiate_auth_code_flow(self,**kwargs): calls['begin']=kwargs; return {'auth_uri':'https://login.microsoftonline.com','state':'s'}
        def acquire_token_by_auth_code_flow(self,*args): return {'id_token_claims':{'oid':ALICE},'access_token':'token','scope':' '.join(SCOPES),'expires_in':3600}
    monkeypatch.setitem(sys.modules,'msal',SimpleNamespace(ConfidentialClientApplication=ClientMock))
    provider=SchoolIdentity(school.settings); provider.begin()
    assert set(calls['begin']['scopes'])==set(SCOPES)
    assert calls['construction']['exclude_scopes']==['offline_access']
    assert provider.finish({}, {})['_graph']['token']=='token'


def test_oracle_bridge_uses_bound_read_only_query_and_strict_decryption():
    key=Fernet.generate_key(); cipher=Fernet(key); calls=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,**params): calls.append((sql,params))
        def fetchmany(self,n):
            return [('id','channel-a',cipher.encrypt('Task'.encode()).decode(),cipher.encrypt(b'<b>hello</b>').decode(),datetime(2026,10,20),datetime(2026,10,26,18),'msg')]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def cursor(self): return Cursor()
        def rollback(self): calls.append(('ROLLBACK',{}))
    source=OracleSource(OracleConfig('readonly','password','dsn',key.decode()),connect=lambda **kwargs:Connection())
    events=source.events([{'id':'channel-a','team_id':CLIENT}],TENANT)
    assert events[0]['notes']=='hello' and events[0]['end_date']=='2026-10-26'
    assert calls[0][0]=='SET TRANSACTION READ ONLY'
    assert calls[1][1]['channel_id']=='channel-a' and ':channel_id' in calls[1][0]
    source.cipher=Fernet(Fernet.generate_key())
    with pytest.raises(Exception): source.events([{'id':'channel-a','team_id':CLIENT}],TENANT)


def test_expired_sync_worker_cannot_override_new_permission_check(school):
    c=school.client(); session=school.db.session(c.cookies.get('__Host-calendarsync_session'))
    old=school.db.begin_sync(session['user_id'],60)
    school.now[0]+=181
    new=school.db.begin_sync(session['user_id'],60)
    assert old != new
    school.db.fail_sync(session['user_id'],'verification_failed',old)
    assert school.db.sync_status(session['user_id'])['lease_until']==new
    with pytest.raises(Missing): school.db.apply_snapshot(session,[],[],60,old)
    school.db.apply_snapshot(session,[],[],60,new)


def test_foundation_cookie_and_bare_identity_headers_never_authenticate_school_app(school):
    c=school.client(None)
    user=school.db.save_user(TENANT,ALICE,{'name':'A'})
    raw=school.db.issue_session(user,3600)
    c.cookies.set('calendarsync_session',raw)
    assert c.get('/api/me',headers={'x-user-id':user}).status_code==401
    c.cookies.set('__Host-calendarsync_session',raw)
    assert c.get('/api/me').status_code==401
