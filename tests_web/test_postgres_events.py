"""Behavior via a SQLite SQL shim; PostgreSQL locking is not modeled here."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_events import PostgresEvents
from web_service.storage import Conflict, Missing


@pytest.fixture
def events(tmp_path):
    path, now = tmp_path/'events.sqlite3', [1000]
    cipher = Fernet(Fernet.generate_key())
    def seal(value): return cipher.encrypt(json.dumps(value).encode()).decode()
    def unseal(value): return json.loads(cipher.decrypt(value.encode()))
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parents[1]/'web_service'/'postgresql_schema_draft.sql').read_text(encoding='utf-8'))
        db.executemany('INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?)',
                       [('alice','tenant','a','x'),('bob','tenant','b','x'),('outsider','other','o','x')])
        db.executemany('INSERT INTO web_channels VALUES(?,?,?)',
                       [('class','tenant',seal({'name':'Class'})),('foreign','other',seal({'name':'Foreign'}))])
        db.executemany('INSERT INTO web_channel_access VALUES(?,?,?,?)',
                       [('alice','class',900,1100),('bob','class',900,1100),('alice','foreign',900,1100)])
        for ident, channel in [('direct:event','class'),('direct:foreign','foreign')]:
            fields = source()
            db.execute('''INSERT INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob)
                VALUES(?,?,?,?,?,?)''', (ident,channel,fields['start_date'],fields['end_date'],fields['kind'],seal(fields)))
        db.executemany('INSERT INTO web_user_source_versions VALUES(?,?,?)',
                       [('alice','direct:event','baseline'),('bob','direct:event','baseline')])
    class Shim:
        def __init__(self, db): self.db=db
        def execute(self, sql, values=()):
            return self.db.execute(re.sub(r' FOR UPDATE(?: OF a)?','',sql).replace('%s','?'),values)
    @contextmanager
    def connect():
        connection=sqlite3.connect(path)
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection: yield Shim(connection)
        finally:
            connection.close()
    def execute(sql, values=()):
        with connect() as db: return db.execute(sql,values).fetchall()
    repo=PostgresEvents(connect,seal,unseal,clock=lambda:now[0])
    def review(user='alice', **values):
        execute('INSERT INTO web_changes(id,user_id,event_id,source_hash,payload_blob,created_at) VALUES(%s,%s,%s,%s,%s,%s)',
                ('change-'+user,user,'direct:event','new-hash',seal(source(title='Updated',source_version='v2',**values)),1000))
    return SimpleNamespace(repo=repo,now=now,execute=execute,seal=seal,unseal=unseal,review=review)


def source(**values):
    return {'title':'Original','notes':'private notes','time':'09:00','source_url':'https://teams.microsoft.com/',
            'start_date':'2026-11-05','end_date':'2026-11-06','kind':'assignment',
            'cancelled':False,'source_version':'v1',**values}


def personal(**values):
    return {'title':'Personal','notes':'','time':'08:00','start_date':'2026-11-05','end_date':'2026-11-06',
            'display_mode':'daily','selected_dates':[],**values}


def test_reads_require_same_active_tenant_user_and_current_acl(events):
    e=events
    assert [item['id'] for item in e.repo._shared('alice','tenant')]==['direct:event']
    assert e.repo._shared('alice','other')==[]
    assert e.repo._shared('outsider','tenant')==[]
    e.repo.add_task('alice',personal())
    assert e.repo.calendar('alice','other','2026-11-05','2026-11-06')==[]
    e.now[0]=1100
    assert e.repo._shared('alice','tenant')==[]
    e.now[0]=1000
    e.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert e.repo._shared('alice','tenant')==[]
    assert e.repo.calendar('alice','tenant','2026-11-05','2026-11-06')==[]


def test_state_and_override_are_user_scoped_and_identity_fields_are_protected(events):
    e=events
    state=e.repo.event_state('alice','tenant','direct:event',{'hidden':True,'completed':True})
    assert state['hidden'] and state['completed']
    e.repo.override_event('alice','tenant','direct:event',{'title':'My title','end_date':'2026-11-07'})
    alice,=e.repo._shared('alice','tenant'); bob,=e.repo._shared('bob','tenant')
    assert alice['title']=='My title' and alice['end_date']=='2026-11-07'
    assert alice['id']=='direct:event' and alice['channel_id']=='class'
    assert bob['title']=='Original' and not bob['hidden'] and not bob['completed']
    for field in ('id','channel_id','user_id','source_version','hidden'):
        with pytest.raises(ValueError):
            e.repo.override_event('alice','tenant','direct:event',{field:'forged'})
    with pytest.raises(ValueError): e.repo.event_state('alice','tenant','direct:event',{})
    with pytest.raises(ValueError): e.repo.event_state('alice','tenant','direct:event',{'hidden':42})
    raw=e.execute("SELECT override_blob FROM web_event_states WHERE user_id='alice'")[0][0]
    assert 'My title' not in raw


@pytest.mark.parametrize('mutation',[
    "UPDATE web_users SET active=0 WHERE id='alice'",
    "UPDATE web_channel_access SET valid_until=1000 WHERE user_id='alice'",
    "DELETE FROM web_channel_access WHERE user_id='alice'",
])
def test_revoked_proof_blocks_state_override_and_review(events,mutation):
    e=events; e.review(); e.execute(mutation)
    with pytest.raises(Missing): e.repo.event_state('alice','tenant','direct:event',{'hidden':True})
    with pytest.raises(Missing): e.repo.override_event('alice','tenant','direct:event',{'title':'Denied'})
    with pytest.raises(Missing): e.repo.resolve_change('alice','tenant','change-alice',True)
    assert e.repo.changes('alice','tenant')==[]
    assert e.execute('SELECT count(*) FROM web_event_states')==[(0,)]
    assert e.execute('SELECT status FROM web_changes')==[('pending',)]


def test_calendar_applies_overridden_dates_hidden_channel_and_kind_filters(events):
    e=events
    task=e.repo.add_task('alice',personal())
    e.repo.override_event('alice','tenant','direct:event',{'start_date':'2026-11-08','end_date':'2026-11-08'})
    assert [item['id'] for item in e.repo.calendar('alice','tenant','2026-11-05','2026-11-05')]==[task['id']]
    e.repo.channel_preference('alice','tenant','class',False)
    assert e.repo.calendar('alice','tenant','2026-11-08','2026-11-08')==[]
    e.repo.event_state('alice','tenant','direct:event',{'hidden':True})
    hidden,=e.repo.calendar('alice','tenant','2026-11-08','2026-11-08',hidden_only=True)
    assert hidden['id']=='direct:event' and 'channel_enabled' not in hidden
    e.repo.event_state('alice','tenant','direct:event',{'hidden':False})
    e.repo.channel_preference('alice','tenant','class',True)
    prefs=e.repo.preferences('alice'); e.repo.save_preferences('alice',{**prefs,'disabled_kinds':['assignment']})
    assert e.repo.calendar('alice','tenant','2026-11-08','2026-11-08')==[]


def test_agenda_respects_personal_display_mode_and_always_includes_final_day(events):
    e=events
    selected=e.repo.add_task('alice',personal(title='Selected',display_mode='selected',selected_dates=[]))
    final=e.repo.add_task('alice',personal(title='Final',display_mode='final'))
    daily=e.repo.add_task('alice',personal(title='Daily'))
    first=e.repo.agenda('alice','tenant','2026-11-05')
    assert [item['id'] for item in first['personal']]==[daily['id']]
    assert [item['id'] for item in first['teams']]==['direct:event']
    last=e.repo.agenda('alice','tenant','2026-11-06')
    assert {item['id'] for item in last['personal']}=={selected['id'],final['id'],daily['id']}


@pytest.mark.parametrize('accept',[True,False])
def test_review_decision_updates_baseline_without_mutating_other_user(events,accept):
    e=events; e.review()
    change,=e.repo.changes('alice','tenant')
    assert change['before']['title']=='Original' and change['after']['title']=='Updated'
    assert e.repo.changes('bob','tenant')==[]
    with pytest.raises(Missing): e.repo.resolve_change('bob','tenant','change-alice',True)
    with pytest.raises(Missing): e.repo.resolve_change('alice','other','change-alice',True)
    e.repo.resolve_change('alice','tenant','change-alice',accept)
    assert e.repo.changes('alice','tenant')==[]
    assert e.execute("SELECT seen_hash FROM web_user_source_versions WHERE user_id='alice'")==[('new-hash',)]
    alice,=e.repo._shared('alice','tenant'); bob,=e.repo._shared('bob','tenant')
    assert alice['title']==('Updated' if accept else 'Original')
    assert bob['title']=='Original'
    if accept: assert alice['source_version']=='v2'
    with pytest.raises(Conflict): e.repo.resolve_change('alice','tenant','change-alice',accept)


def test_missing_baseline_rolls_back_accepted_override_and_review_status(events):
    e=events; e.review()
    e.execute("DELETE FROM web_user_source_versions WHERE user_id='alice'")
    with pytest.raises(Missing): e.repo.resolve_change('alice','tenant','change-alice',True)
    assert e.execute('SELECT * FROM web_event_states')==[]
    assert e.execute('SELECT status FROM web_changes')==[('pending',)]


def test_proof_expiring_during_row_lock_cannot_write(events):
    e=events; ticks=iter([1000,1100]); e.repo.clock=lambda:next(ticks)
    with pytest.raises(Missing): e.repo.event_state('alice','tenant','direct:event',{'hidden':True})
    assert e.execute('SELECT * FROM web_event_states')==[]


def test_proof_expiring_during_review_lock_cannot_decide(events):
    e=events; e.review(); ticks=iter([1000,1000,1100]); e.repo.clock=lambda:next(ticks)
    with pytest.raises(Missing): e.repo.resolve_change('alice','tenant','change-alice',True)
    assert e.execute('SELECT status FROM web_changes')==[('pending',)]
    assert e.execute('SELECT * FROM web_event_states')==[]


def test_override_payload_cannot_replace_identity_or_visibility_flags(events):
    e=events
    e.execute('INSERT INTO web_event_states(user_id,event_id,override_blob) VALUES(%s,%s,%s)',
              ('alice','direct:event',e.seal({'title':'Allowed','id':'forged','channel_id':'foreign',
                                           'source':'personal','hidden':True,'completed':True})))
    value,=e.repo._shared('alice','tenant')
    assert value['title']=='Allowed' and value['id']=='direct:event' and value['channel_id']=='class'
    assert value['source']=='teams' and not value['hidden'] and not value['completed']


def test_known_oracle_recovery_requires_verified_channels_and_user_source_scope(events):
    e=events
    for ident,channel,values in [('upcoming','class',{}),('cancelled','class',{'cancelled':True}),
                                  ('past','class',{'end_date':'1960-01-01','start_date':'1960-01-01'}),
                                  ('not-yours','class',{}),('foreign','foreign',{})]:
        fields=source(**values)
        e.execute('''INSERT INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob,cancelled)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',('oracle:'+ident,channel,fields['start_date'],fields['end_date'],
                                           fields['kind'],e.seal(fields),int(fields['cancelled'])))
        e.execute('INSERT INTO web_user_source_versions VALUES(%s,%s,%s)',
                  ('bob' if ident=='not-yours' else 'alice','oracle:'+ident,'seen'))
    e.execute("DELETE FROM web_channel_access WHERE user_id='alice'")
    known=e.repo.known_oracle_events('alice','tenant',[{'id':'class','team_id':'team'}])
    assert [item['id'] for item in known]==['upcoming']
    assert e.repo.known_oracle_events('alice','other',[{'id':'foreign'}])==[]
    assert e.repo.known_oracle_events('alice','tenant',[])==[]
    with pytest.raises(Missing):
        e.repo.known_oracle_events('alice','tenant',[{'id':'class','tenant_id':'other'}])
    e.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert e.repo.known_oracle_events('alice','tenant',[{'id':'class'}])==[]
