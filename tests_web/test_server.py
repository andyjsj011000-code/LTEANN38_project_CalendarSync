from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import base64
import json
import sys
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import httpx
import pytest
from conftest import TENANT,CLIENT,ALICE,BOB
from web_service.config import Settings
from web_service.server import create_test_server
from web_service.test_storage import TestDatabase
from web_service.push import Subscription,Notifications,PushSender,b64
from web_service.connections import PersistentIdentity,ConnectionTokens
from web_service.direct_source import TeamsGraph,AIExtractor,DirectSource
from web_service.storage import digest,Missing,Conflict
from web_service.school_graph import GraphFailure

class Identity:
    def __init__(self,clock): self.clock=clock; self.oid=ALICE; self.refreshes=0; self.refresh_error=False
    def begin(self,background=False): return {'state':'s','auth_uri':'https://login.microsoftonline.com/authorize','background_allowed':background}
    def finish(self,flow,query):
        if query!={'state':'s','code':'ok'}: raise ValueError()
        return {'tid':TENANT,'oid':self.oid,'iss':f'https://login.microsoftonline.com/{TENANT}/v2.0','aud':CLIENT,'sub':'subject','exp':self.clock()+3600,'name':'Student','_graph':{'token':'private-access-token','expires_in':3600},'_connection':{'cache':'private-cache-refresh-token' if flow['background_allowed'] else '', 'background':flow['background_allowed']}}
    def refresh(self,*args):
        self.refreshes+=1
        if self.refresh_error: raise ValueError('secret failure')
        return 'new-private-token',3600,'new-private-cache'
class Graph:
    def __init__(self): self.allowed=True; self.calls=0
    def assert_member(self,*args): pass
    def channels(self,token,oid,tenant):
        self.calls+=1
        return [{'id':'class','team_id':CLIENT,'name':'Class'}] if self.allowed and oid==ALICE else []
class Source:
    configured=True
    def __init__(self): self.time='18:00'; self.cancelled=False; self.failure=False
    def events(self,channels,tenant):
        if self.failure: raise ValueError('secret source')
        return [{'id':'deadline','channel_id':'class','title':'School deadline','notes':'Private post','start_date':'2026-10-22','end_date':'2026-10-22','time':self.time,'kind':'assignment','cancelled':self.cancelled}] if channels else []
class Sender:
    configured=True; public='public-vapid-key'
    def __init__(self): self.sent=[]; self.outcome='sent'
    def send(self,subscription,payload): self.sent.append((subscription,payload)); return self.outcome

def subscription(endpoint='https://fcm.googleapis.com/fcm/send/opaque'):
    public=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    return {'endpoint':endpoint,'keys':{'p256dh':b64(public),'auth':b64(b'0123456789abcdef')}}

@pytest.fixture
def server(tmp_path):
    now=[int(datetime(2026,10,22,8,0,tzinfo=timezone.utc).timestamp())] # 17:00 KST
    settings=Settings(database_path=tmp_path/'test.sqlite3',data_key=Fernet.generate_key().decode(),origin='https://test.example.school',tenant_id=TENANT,client_id=CLIENT,client_secret='secret',session_seconds=28800,idle_seconds=1800)
    db=TestDatabase(settings.database_path,settings.data_key,clock=lambda:now[0]); identity=Identity(db.clock); graph=Graph(); source=Source(); sender=Sender()
    app=create_test_server(settings,db,identity,graph,source,sender,worker=False); clients=[]
    def client(oid=ALICE,background=True):
        c=TestClient(app,base_url=settings.origin); clients.append(c)
        if oid:
            identity.oid=oid; c.get('/auth/login',params={'background':int(background)},follow_redirects=False)
            response=c.get('/auth/callback?state=s&code=ok',follow_redirects=False)
            assert response.headers['location']=='/'
            c.headers['x-csrf-token']=c.get('/api/me').json()['csrf_token']
        return c
    yield SimpleNamespace(now=now,settings=settings,db=db,identity=identity,graph=graph,source=source,sender=sender,app=app,client=client,integration=app.state.integration)
    for c in clients: c.close()


def test_independent_connection_survives_view_expiry_but_explicit_logout_revokes(server):
    c=server.client(); session=server.db.session(c.cookies.get('__Host-calendarsync_session')); device=server.db.session_device(session)
    c.post('/api/push',json=subscription())
    server.now[0]+=1801
    assert c.get('/api/me').status_code==401
    assert server.db.device(device['id_hash']) and server.db.subscriptions(device['id_hash'])
    assert server.db.session(c.cookies.get('__Host-calendarsync_session')) is None
    d=server.client(); current=server.db.session_device(server.db.session(d.cookies.get('__Host-calendarsync_session')))
    d.post('/api/push',json=subscription('https://fcm.googleapis.com/fcm/send/device-two'))
    assert d.post('/auth/logout').status_code==204
    assert server.db.device(current['id_hash']) is None
    assert not server.db.subscriptions(current['id_hash'])


def test_temporary_login_cannot_enable_background_push(server):
    c=server.client(background=False)
    assert c.get('/api/push').json()['background_enabled'] is False
    assert c.post('/api/push',json=subscription()).status_code==409


def test_refresh_cache_encryption_rotation_and_revocation_race(server):
    c=server.client(); session=server.db.session(c.cookies.get('__Host-calendarsync_session')); device=server.db.session_device(session)
    assert b'private-cache-refresh-token' not in server.db.path.read_bytes()
    server.now[0]+=3550
    assert server.integration.tokens.acquire(device)=='new-private-token' and server.identity.refreshes==1
    assert b'new-private-cache' not in server.db.path.read_bytes()
    lease=server.db.lock_refresh(device['id_hash']); server.db.revoke_device(device['id_hash'])
    assert not server.db.save_refresh(device['id_hash'],lease,'revived',3600,'bad-cache')
    with pytest.raises(GraphFailure): server.integration.tokens.acquire(device)


def test_refresh_failure_stops_school_access_and_background_notifications(server):
    c=server.client(); assert c.get('/api/today').json()['teams']
    session=server.db.session(c.cookies.get('__Host-calendarsync_session')); device=server.db.session_device(session)
    server.now[0]+=3550; server.identity.refresh_error=True
    with pytest.raises(GraphFailure): server.integration.tokens.acquire(device)
    assert not server.db.channels(session['user_id'],TENANT)
    assert server.db.device(device['id_hash'])['state']=='reauth_required'


def test_all_device_logout_and_account_switch_invalidate_subscriptions(server):
    a=server.client(); a.post('/api/push',json=subscription()); b=server.client()
    b.post('/api/push',json=subscription('https://fcm.googleapis.com/fcm/send/two'))
    assert a.post('/auth/logout-all').status_code==204
    assert b.get('/api/me').status_code==401
    with server.db.connect() as db: assert db.execute('SELECT count(*) FROM web_push_subscriptions').fetchone()[0]==0
    c=server.client(); c.post('/api/push',json=subscription())
    server.identity.oid=BOB; c.get('/auth/login?background=1',follow_redirects=False)
    c.get('/auth/callback?state=s&code=ok',follow_redirects=False)
    with server.db.connect() as db: assert db.execute('SELECT count(*) FROM web_push_subscriptions').fetchone()[0]==0


def test_changes_require_each_users_decision_and_current_access(server):
    a=server.client(); initial=a.get('/api/today').json()['teams'][0]
    server.source.time='19:00'; server.now[0]+=61
    assert a.post('/api/teams/sync').status_code==200
    assert a.get('/api/today').json()['teams'][0]['time']=='18:00'
    change=a.get('/api/changes').json()['items'][0]
    b=server.client(BOB)
    assert b.post('/api/changes/'+change['id'],json={'accept':True}).status_code==404
    assert a.post('/api/changes/'+change['id'],json={'accept':True}).status_code==200
    assert a.get('/api/today').json()['teams'][0]['time']=='19:00'
    assert a.get('/api/changes').json()['items']==[]
    server.source.time='20:00'; server.now[0]+=61; a.post('/api/teams/sync')
    pending=a.get('/api/changes').json()['items'][0]
    server.graph.allowed=False; server.now[0]+=61
    assert a.post('/api/changes/'+pending['id'],json={'accept':True}).status_code==404


def test_personal_override_feedback_and_rule_ownership(server):
    a=server.client(); event=a.get('/api/today').json()['teams'][0]
    b=server.client(BOB)
    body={'title':'My title','start_date':'2026-10-22','notes':'my notes'}
    assert b.put('/api/events/'+event['id']+'/override',json=body).status_code==404
    assert a.put('/api/events/'+event['id']+'/override',json=body).status_code==200
    assert a.get('/api/today').json()['teams'][0]['title']=='My title'
    assert b.get('/api/reminders/teams/'+event['id']).status_code==404
    assert a.put('/api/reminders/teams/'+event['id'],json={'offsets':[-60],'custom_times':[]}).status_code==200
    assert a.post('/api/feedback',json={'message':'feedback-private'}).status_code==201
    assert b'feedback-private' not in server.db.path.read_bytes()


@pytest.mark.parametrize('endpoint',['http://fcm.googleapis.com/fcm/send/x','https://127.0.0.1/x','https://fcm.googleapis.com.evil.test/x','https://fcm.googleapis.com:8443/x','https://user@fcm.googleapis.com/x'])
def test_push_endpoint_ssrf_and_cross_account_registration(server,endpoint):
    c=server.client(); assert c.post('/api/push',json=subscription(endpoint)).status_code==422


def test_push_endpoint_cannot_be_stolen_between_devices(server):
    a=server.client(); b=server.client(BOB); payload=subscription()
    assert a.post('/api/push',json=payload).status_code==201
    assert b.post('/api/push',json=payload).status_code==409
    assert payload['endpoint'].encode() not in server.db.path.read_bytes()
    assert a.post('/api/push/test').json()['accepted']==1
    assert a.post('/api/push/test').status_code==429


def test_notification_dedup_complete_hidden_filters_and_pending_change(server):
    c=server.client(); event=c.get('/api/today').json()['teams'][0]
    c.post('/api/push',json=subscription())
    session=server.db.session(c.cookies.get('__Host-calendarsync_session')); device=server.db.session_device(session)
    notes=Notifications(server.db,server.sender)
    assert notes.dispatch(device)==1
    assert notes.dispatch(device)==0
    assert 'School deadline' not in json.dumps(server.sender.sent)
    c.patch('/api/events/'+event['id']+'/state',json={'completed':True})
    assert notes.due(session['user_id'],TENANT)==[]
    c.patch('/api/events/'+event['id']+'/state',json={'completed':False,'hidden':True})
    assert notes.due(session['user_id'],TENANT)==[]
    c.patch('/api/events/'+event['id']+'/state',json={'hidden':False})
    server.source.time='19:00'; server.now[0]+=61; c.post('/api/teams/sync')
    assert notes.due(session['user_id'],TENANT)==[]


def test_worker_runs_after_browser_session_is_deleted(server):
    c=server.client(); c.post('/api/push',json=subscription()); session=server.db.session(c.cookies.get('__Host-calendarsync_session'))
    server.db.revoke_session(c.cookies.get('__Host-calendarsync_session'))
    server.integration.tick()
    assert server.sender.sent and server.db.channels(session['user_id'],TENANT)


def test_static_worker_manifest_and_csp_do_not_cache_private_pages(server):
    c=server.client(None)
    worker=c.get('/service-worker.js'); assert worker.status_code==200
    assert 'caches.open' not in worker.text and "addEventListener('fetch'" not in worker.text
    assert c.get('/manifest.webmanifest').json()['display']=='standalone'
    assert "worker-src 'self'" in c.get('/').headers['content-security-policy']
    assert c.get('/api/service').json()['mode']=='test-server'
    assert c.post('/auth/beta',json={'code':'fake'}).status_code==404


def test_msal_persistent_cache_selects_only_same_tenant_and_oid(server,monkeypatch):
    seen={}
    class Cache:
        def deserialize(self,value): seen['read']=value
        def serialize(self): return 'rotated-cache'
    class App:
        def __init__(self,*args,**kwargs): seen['options']=kwargs
        def get_accounts(self): return [{'local_account_id':ALICE,'realm':TENANT},{'local_account_id':BOB,'realm':TENANT}]
        def acquire_token_silent_with_error(self,scopes,account): seen['account']=account; return {'access_token':'new','expires_in':3600,'scope':' '.join(scopes)}
    monkeypatch.setitem(sys.modules,'msal',SimpleNamespace(SerializableTokenCache=Cache,ConfidentialClientApplication=App))
    identity=PersistentIdentity(server.settings)
    assert identity.refresh('encrypted-cache',ALICE,TENANT)==('new',3600,'rotated-cache')
    assert seen['account']['local_account_id']==ALICE and seen['options']['exclude_scopes']==[]


def test_direct_ingestion_preserves_period_and_proposes_replies_and_deletion(server):
    class DirectGraph:
        def __init__(self): self.version=1; self.deleted=False
        def messages(self,token,channel):
            return [{'id':'m','createdDateTime':'2026-10-22T01:00:00Z','body':{'content':'period task'},'replies':[{'body':{'content':'changed'}}] if self.version==2 else [],'lastModifiedDateTime':str(self.version),'deletedDateTime':'deleted' if self.deleted else None}]
        def message(self,*args): return None
    class Extractor:
        configured=True
        def __init__(self): self.calls=0
        def extract(self,text,images,previous):
            self.calls+=1
            return [{'key':'task','title':'Full period','notes':'n','start_date':'2026-10-20','end_date':'2026-11-10','time':None,'kind':'assignment','cancelled':False}]
    graph=DirectGraph(); extractor=Extractor(); source=DirectSource(graph,extractor)
    channel=[{'id':'class','team_id':CLIENT,'name':'Class'}]
    one=source.collect('t',channel,TENANT,server.db,'u')
    assert one[0]['end_date']=='2026-11-10'
    source.collect('t',channel,TENANT,server.db,'u'); assert extractor.calls==1
    graph.version=2; two=source.collect('t',channel,TENANT,server.db,'u'); assert extractor.calls==2
    assert one[0]['id']==two[0]['id'] and one[0]['source_version']!=two[0]['source_version']
    graph.deleted=True; three=source.collect('t',channel,TENANT,server.db,'u'); assert three[0]['cancelled']


def test_ai_schema_rejects_instructions_and_arbitrary_output():
    from web_service.direct_source import Extraction
    with pytest.raises(Exception): Extraction.model_validate({'events':[{'key':'x','title':'x','notes':'n','start_date':'2026-10-22','end_date':'2026-10-21','time':None,'kind':'event','cancelled':False,'execute':'evil'}]})


def test_pending_changes_keep_decision_and_newer_revision(server):
    c=server.client(); c.get('/api/today')
    server.source.time='19:00'; server.now[0]+=61; c.post('/api/teams/sync')
    item=c.get('/api/changes').json()['items'][0]
    assert c.post('/api/changes/'+item['id'],json={'accept':False}).status_code==200
    server.now[0]+=61; c.post('/api/teams/sync')
    assert c.get('/api/changes').json()['items']==[]
    server.source.time='20:00'; server.now[0]+=61; c.post('/api/teams/sync')
    assert c.get('/api/changes').json()['items'][0]['after']['time']=='20:00'


def test_custom_reminder_time_and_expired_subscription_are_handled(server):
    c=server.client(); task=c.post('/api/tasks',json={'title':'Private task','start_date':'2026-11-20','end_date':'2026-11-21'}).json()
    # A custom reminder may precede a future task period.
    when=datetime.fromtimestamp(server.db.clock()+60,timezone.utc).isoformat()
    assert c.put('/api/reminders/personal/'+task['id'],json={'offsets':[],'custom_times':[when]}).status_code==200
    c.post('/api/push',json=subscription()); device=server.db.session_device(server.db.session(c.cookies.get('__Host-calendarsync_session')))
    server.now[0]+=60
    assert any(key.startswith('item:personal:') for key,payload in server.integration.notifications.due(device['user_id'],TENANT))
    server.sender.outcome='expired'; server.integration.notifications.dispatch(device)
    assert server.db.subscriptions(device['id_hash'])==[]


def test_web_push_transport_disallows_redirects_and_does_not_log_secrets(server,monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    private=b64(key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    sender=PushSender(private,'mailto:test@example.invalid'); observed={}
    class Response: status_code=201
    class PushError(Exception): pass
    def webpush(**kwargs):
        observed.update(kwargs)
        assert kwargs['requests_session'].trust_env is False
        return Response()
    monkeypatch.setitem(sys.modules,'pywebpush',SimpleNamespace(webpush=webpush,WebPushException=PushError))
    assert sender.send(subscription(),{'title':'CalendarSync'})=='sent'
    assert observed['timeout']==10 and observed['ttl']==600 and len(sender.public)>80


def test_hosted_images_reject_arbitrary_network_destinations():
    graph=TeamsGraph(lambda: (_ for _ in ()).throw(AssertionError('Must not open a network client')))
    channel={'id':'class','team_id':CLIENT}
    assert graph.hosted_image('secret',channel,'message','https://evil.test/image.png') is None
    assert graph.hosted_image('secret',channel,'message','https://graph.microsoft.com/v1.0/users/other/photo/$value') is None


def test_expired_connections_are_scrubbed_not_just_disabled(server):
    c=server.client(); session=server.db.session(c.cookies.get('__Host-calendarsync_session')); device=server.db.session_device(session)
    c.post('/api/push',json=subscription())
    server.now[0]+=31*86400; server.db.maintenance()
    with server.db.connect() as conn:
        row=conn.execute('SELECT active,credential_blob FROM web_connections WHERE id_hash=?',(device['id_hash'],)).fetchone()
        assert row[0]==0 and server.db.unseal(row[1])=={}
        assert conn.execute('SELECT count(*) FROM web_push_subscriptions').fetchone()[0]==0


@pytest.mark.parametrize('status',[403,404])
def test_single_message_permission_denial_is_not_a_deletion(status):
    import httpx
    from web_service.school_graph import GraphFailure
    graph=TeamsGraph(lambda:httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(status,json={}))))
    channel={'id':'class','team_id':CLIENT}
    if status==403:
        with pytest.raises(GraphFailure): graph.message('token',channel,'message')
    else:
        assert graph.message('token',channel,'message') is None


def test_ancient_personal_task_cannot_break_reminder_worker(server):
    c=server.client(); c.get('/api/today')
    response=c.post('/api/tasks',json={'title':'Ancient','start_date':'0001-01-01','end_date':'0001-01-01','time':'00:00'})
    assert response.status_code==201
    session=server.db.session(c.cookies.get('__Host-calendarsync_session'))
    due=server.integration.notifications.due(session['user_id'],TENANT)
    assert any(key.startswith('item:teams:') for key,payload in due)



def test_single_message_uses_supported_separate_reply_endpoint():
    import httpx
    seen=[]
    def reply(request):
        seen.append(str(request.url))
        if request.url.path.endswith('/replies'):
            return httpx.Response(200,json={'value':[{'id':'reply','body':{'content':'Updated deadline'}}]})
        assert not request.url.query
        return httpx.Response(200,json={'id':'message','body':{'content':'Deadline'}})
    graph=TeamsGraph(lambda:httpx.Client(transport=httpx.MockTransport(reply)))
    result=graph.message('token',{'id':'class','team_id':CLIENT},'message')
    assert result['replies'][0]['id']=='reply' and seen[1].endswith('/replies?$top=30')
