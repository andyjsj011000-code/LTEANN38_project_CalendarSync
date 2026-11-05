"""Whole-app candidate contract through a SQLite SQL shim, never a live PG DB."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from conftest import TENANT,CLIENT,ALICE,BOB
import test_server as server_contract
from web_service.config import Settings
from web_service.postgres_database import PostgresDatabase
from web_service.server import create_test_server
from web_service.storage import digest


@pytest.fixture
def pg_server(tmp_path):
    path=tmp_path/'pg-shim.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript((Path(__file__).parents[1]/'web_service'/'postgresql_schema_draft.sql').read_text(encoding='utf-8'))
    statements=[]
    class Shim:
        def __init__(self,c): self.c=c
        def execute(self,sql,params=()):
            statements.append(sql)
            sql=re.sub(r' FOR UPDATE(?: OF \w+)?','',sql).replace('%s','?')
            return self.c.execute(sql,params)
    @contextmanager
    def connect():
        c=sqlite3.connect(path)
        c.execute('PRAGMA foreign_keys=ON')
        try:
            with c: yield Shim(c)
        finally: c.close()
    now=[int(datetime(2026,10,22,8,0,tzinfo=timezone.utc).timestamp())]
    settings=Settings(database_path=tmp_path/'unused.sqlite3',data_key=Fernet.generate_key().decode(),
                      origin='https://test.example.school',tenant_id=TENANT,client_id=CLIENT,
                      client_secret='test-only-secret',session_seconds=28800,idle_seconds=1800)
    db=PostgresDatabase(connect,settings.data_key,TENANT,clock=lambda:now[0])
    # Test-only access to the SQL shim file for the existing encryption checks.
    db.path=path
    identity=server_contract.Identity(db.clock); graph=server_contract.Graph()
    source=server_contract.Source(); sender=server_contract.Sender()
    app=create_test_server(settings,db,identity,graph,source,sender,worker=False)
    clients=[]
    def client(oid=ALICE,background=True):
        c=TestClient(app,base_url=settings.origin); clients.append(c)
        if oid:
            identity.oid=oid
            c.get('/auth/login',params={'background':int(background)},follow_redirects=False)
            result=c.get('/auth/callback?state=s&code=ok',follow_redirects=False)
            assert result.headers['location']=='/'
            c.headers['x-csrf-token']=c.get('/api/me').json()['csrf_token']
        return c
    yield SimpleNamespace(db=db,path=path,settings=settings,now=now,identity=identity,graph=graph,
                          source=source,sender=sender,app=app,client=client,
                          integration=app.state.integration,statements=statements)
    for c in clients: c.close()


@pytest.mark.parametrize('contract',[
    'test_independent_connection_survives_view_expiry_but_explicit_logout_revokes',
    'test_temporary_login_cannot_enable_background_push',
    'test_refresh_cache_encryption_rotation_and_revocation_race',
    'test_refresh_failure_stops_school_access_and_background_notifications',
    'test_all_device_logout_and_account_switch_invalidate_subscriptions',
    'test_changes_require_each_users_decision_and_current_access',
    'test_personal_override_feedback_and_rule_ownership',
    'test_push_endpoint_cannot_be_stolen_between_devices',
    'test_notification_dedup_complete_hidden_filters_and_pending_change',
    'test_worker_runs_after_browser_session_is_deleted',
    'test_pending_changes_keep_decision_and_newer_revision',
    'test_custom_reminder_time_and_expired_subscription_are_handled',
    'test_ancient_personal_task_cannot_break_reminder_worker',
])
def test_injected_pg_adapter_satisfies_existing_server_behavior(pg_server,contract):
    getattr(server_contract,contract)(pg_server)
    assert not any('BEGIN IMMEDIATE' in sql or 'INSERT OR IGNORE' in sql or '?' in sql
                   for sql in pg_server.statements)


def test_personal_tasks_and_devices_use_their_own_sql_columns(pg_server):
    s=pg_server; a=s.client(); b=s.client(BOB)
    response=a.post('/api/tasks',json={'title':'Personal','start_date':'2026-10-22'})
    assert response.status_code==201
    task=response.json(); assert task['source']=='personal' and task['version']==1
    assert b.get('/api/tasks/'+task['id']).status_code==404
    updated=a.patch('/api/tasks/'+task['id']+'/state',json={'version':1,'completed':True})
    assert updated.status_code==200 and updated.json()['version']==2
    assert a.patch('/api/tasks/'+task['id']+'/state',json={'version':1,'hidden':True}).status_code==409
    assert a.get('/api/push').status_code==200


def test_refreshed_device_version_is_used_for_sync_commit(pg_server):
    s=pg_server; c=s.client(); c.get('/api/today')
    session=s.db.session(c.cookies.get('__Host-calendarsync_session'))
    old=s.db.session_device(session)
    s.now[0]+=3550
    s.source.time='19:00'
    s.integration.run_sync(old,ingest=True)
    assert s.identity.refreshes==1
    current=s.db.device(old['id_hash'])
    assert current['version']==old['version']+1
    assert s.db.sync_status(session['user_id'])['state']=='connected'
    assert s.db.changes(session['user_id'],TENANT)[0]['after']['time']=='19:00'


def test_revocation_during_fetch_rolls_back_the_snapshot(pg_server):
    s=pg_server; c=s.client()
    session=s.db.session(c.cookies.get('__Host-calendarsync_session'))
    device=s.db.session_device(session)
    events=s.source.events
    def revoke_while_fetching(channels,tenant):
        s.db.revoke_device(device['id_hash'])
        return events(channels,tenant)
    s.source.events=revoke_while_fetching
    s.integration.run_sync(device,ingest=True)
    with s.db.connect() as db:
        assert db.execute('SELECT count(*) FROM web_events').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM web_channel_access').fetchone()[0]==0


def test_candidate_initialize_only_reads_and_rejects_other_school_data(pg_server):
    s=pg_server; s.statements.clear(); s.db.initialize()
    assert all(sql.lstrip().startswith('SELECT ') for sql in s.statements)
    with pytest.raises(ValueError,match='tenant'):
        s.db.save_user('foreign',BOB,{'name':'Foreign'})
    with s.db.connect() as db:
        db.execute('INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(%s,%s,%s,%s)',
                   ('foreign-user','foreign',BOB,s.db.seal({'name':'Foreign'})))
    with pytest.raises(RuntimeError,match='schema validation'):
        s.db.initialize()
    with s.db.connect() as db:
        assert db.execute("SELECT count(*) FROM web_users WHERE id='foreign-user'").fetchone()[0]==1


def test_missing_candidate_columns_are_not_automatically_migrated(pg_server):
    s=pg_server
    with s.db.connect() as db: db.execute('ALTER TABLE web_deliveries DROP COLUMN lease_token')
    s.statements.clear()
    with pytest.raises(RuntimeError,match='explicit reviewed migration'):
        s.db.initialize()
    assert not any(sql.lstrip().startswith(('CREATE','ALTER','UPDATE','DELETE','INSERT'))
                   for sql in s.statements)


def test_server_rejects_wrong_tenant_and_unfinished_pg_notification_runtime(pg_server):
    s=pg_server
    with pytest.raises(ValueError,match='notification runtime'):
        create_test_server(s.settings,s.db,s.identity,s.graph,s.source,s.sender,
                           worker=False,notification_runtime=object())
    s.db.tenant_id='foreign'
    with pytest.raises(ValueError,match='school tenant'):
        create_test_server(s.settings,s.db,s.identity,s.graph,s.source,s.sender,worker=False)
