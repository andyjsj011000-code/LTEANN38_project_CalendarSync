"""Opt-in web routes on real PostgreSQL; providers and delivery remain simulated."""
import os
import uuid
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from conftest import TENANT,CLIENT,ALICE
import test_server as contract
from web_service.config import Settings
from web_service.direct_source import DirectSource
from web_service.postgres_connection import PostgresConnectionFactory
from web_service.postgres_database import PostgresDatabase
from web_service.server import create_test_server


@pytest.fixture
def postgres_server(tmp_path):
    dsn=os.environ.get('CALENDARSYNC_DISPOSABLE_POSTGRES_DSN')
    if not dsn or os.environ.get('CALENDARSYNC_ALLOW_TEMP_SCHEMA')!='yes':
        pytest.skip('A dedicated disposable PostgreSQL database must be explicitly enabled')
    psycopg=pytest.importorskip('psycopg')
    schema='calendarsync_test_'+uuid.uuid4().hex
    created=False
    # Disable TLS only for the explicit loopback CI database; external targets
    # retain their explicit URI setting or the verified connection default.
    mode='disable' if urlsplit(dsn).hostname in {'127.0.0.1','localhost','::1'} else None
    connection=PostgresConnectionFactory(dsn,schema,sslmode=mode,statement_timeout_ms=5000,
                                         lock_timeout_ms=4000)
    clients=[]
    try:
        with psycopg.connect(dsn,autocommit=True,connect_timeout=5) as bootstrap:
            bootstrap.execute('CREATE SCHEMA '+schema)
            created=True
        ddl=(Path(__file__).parents[1]/'web_service'/'postgresql_schema_draft.sql').read_text(encoding='utf-8')
        with connection() as bootstrap:
            for statement in '\n'.join(line for line in ddl.splitlines()
                                       if not line.lstrip().startswith('--')).split(';'):
                if statement.strip(): bootstrap.execute(statement)
        now=[int(datetime(2026,10,22,8,0,tzinfo=timezone.utc).timestamp())]
        settings=Settings(database_path=tmp_path/'unused.sqlite3',data_key=Fernet.generate_key().decode(),
                          origin='https://test.example.school',tenant_id=TENANT,client_id=CLIENT,
                          client_secret='test-only-secret',session_seconds=28800,idle_seconds=1800)
        db=PostgresDatabase(connection,settings.data_key,TENANT,clock=lambda:now[0])
        identity=contract.Identity(db.clock); graph=contract.Graph()
        source=contract.Source(); sender=contract.Sender()
        app=create_test_server(settings,db,identity,graph,source,sender,worker=False)
        def client(oid=ALICE,background=True):
            c=TestClient(app,base_url=settings.origin); clients.append(c)
            if oid:
                identity.oid=oid
                c.get('/auth/login',params={'background':int(background)},follow_redirects=False)
                result=c.get('/auth/callback?state=s&code=ok',follow_redirects=False)
                assert result.headers['location']=='/'
                c.headers['x-csrf-token']=c.get('/api/me').json()['csrf_token']
            return c
        yield SimpleNamespace(db=db,settings=settings,now=now,identity=identity,graph=graph,
                              source=source,sender=sender,app=app,client=client,
                              integration=app.state.integration)
    finally:
        for client in clients: client.close()
        if created:
            with psycopg.connect(dsn,autocommit=True,connect_timeout=5) as bootstrap:
                bootstrap.execute("SET statement_timeout='5s'")
                bootstrap.execute('DROP SCHEMA '+schema+' CASCADE')


@pytest.mark.parametrize('behavior',[
    'test_all_device_logout_and_account_switch_invalidate_subscriptions',
    'test_changes_require_each_users_decision_and_current_access',
    'test_notification_dedup_complete_hidden_filters_and_pending_change',
    'test_worker_runs_after_browser_session_is_deleted',
])
def test_real_pg_web_contract(postgres_server,behavior):
    getattr(contract,behavior)(postgres_server)


def test_real_pg_refresh_can_commit_using_updated_device_version(postgres_server):
    s=postgres_server; c=s.client(); c.get('/api/today')
    session=s.db.session(c.cookies.get('__Host-calendarsync_session'))
    old=s.db.session_device(session)
    s.now[0]+=3550; s.source.time='19:00'
    s.integration.run_sync(old,ingest=True)
    assert s.identity.refreshes==1
    assert s.db.device(old['id_hash'])['version']==old['version']+1
    assert s.db.sync_status(session['user_id'])['state']=='connected'
    assert s.db.changes(session['user_id'],TENANT)[0]['after']['time']=='19:00'


def test_real_pg_direct_collection_cache_cursor_and_snapshot(postgres_server):
    s=postgres_server; c=s.client()
    session=s.db.session(c.cookies.get('__Host-calendarsync_session'))
    device=s.db.session_device(session)
    channels=[{'id':'class','team_id':CLIENT,'name':'Class'}]
    class Graph:
        def messages(self,*args):
            return [{'id':'root','createdDateTime':'2026-10-22T01:00:00Z',
                     'lastModifiedDateTime':'v1','body':{'content':'Test-only deadline'},'replies':[]}]
        def message(self,*args,**kwargs): return self.messages()[0]
    class Extractor:
        configured=True
        def extract(self,*args):
            return [{'key':'task','title':'Test-only event','notes':'','time':'18:00',
                     'start_date':'2026-10-22','end_date':'2026-10-22','kind':'assignment',
                     'cancelled':False}]
    source=DirectSource(Graph(),Extractor(),images=False)
    lease=s.db.begin_sync(session['user_id'],0)
    events=source.collect('test-token',channels,TENANT,s.db,session['user_id'])
    s.db.commit_snapshot(device,channels,events,60,lease)
    assert s.db.cached_messages('class')['root']['events'][0]['title']=='Test-only event'
    assert s.db.catalog_events(['class'])==events
    assert s.db.agenda(session['user_id'],TENANT,'2026-10-22')['teams'][0]['title']=='Test-only event'
