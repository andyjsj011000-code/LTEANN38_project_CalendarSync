import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from cryptography.fernet import Fernet
from web_service.notification_queue import NotificationQueue
from web_service.notification_runtime import NotificationRuntime
from web_service.school_graph import GraphFailure
from web_service.storage import digest
from web_service.test_storage import TestDatabase


def test_worker_tries_another_candidate_only_after_checking_graph(tmp_path):
    q=NotificationQueue(lambda:sqlite3.connect(tmp_path/'q.sqlite3'))
    q.register('sub','state','tenant','team','channel')
    q.accept([{'subscriptionId':'sub','clientState':'state'}])
    first={'id_hash':'a','user_id':'alice','tenant_id':'tenant','object_id':'a'}
    second={'id_hash':'b','user_id':'bob','tenant_id':'tenant','object_id':'b'}
    class DB:
        commits=[]
        def device(self,d): return {'a':first,'b':second}[d]
        def begin_sync(self,*args): return 123
        def fail_sync(self,*args): pass
        def commit_snapshot(self,device,*args): self.commits.append(device['user_id'])
    class Tokens:
        def acquire(self,device): return device['object_id']
    class Graph:
        def channels(self,token,*args):
            return [] if token=='a' else [{'id':'channel','team_id':'team'}]
    class Source:
        def collect(self,token,*args,**kwargs):
            assert token=='b'
            return []
    db=DB(); runtime=NotificationRuntime(q,db,Tokens(),Graph(),Source(),60)
    runtime.candidates=lambda job:[first,second]
    assert runtime.tick()=='processed'
    assert db.commits==['bob']
    assert q.claim() is None


def test_no_connection_retains_job_for_retry(tmp_path):
    now=[1000]
    q=NotificationQueue(lambda:sqlite3.connect(tmp_path/'q.sqlite3'),clock=lambda:now[0])
    q.register('sub','state','tenant','team','channel')
    q.accept([{'subscriptionId':'sub','clientState':'state'}])
    runtime=NotificationRuntime(q,SimpleNamespace(),None,None,None,60)
    runtime.candidates=lambda job:[]
    assert runtime.tick()=='no_authorized_connection'
    assert q.claim() is None
    now[0]+=60
    assert q.claim() is not None


def test_candidates_survive_cleared_access_and_rotate_past_first_ten(tmp_path):
    path=tmp_path/'candidates.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE web_users(id TEXT PRIMARY KEY,tenant_id TEXT,active INTEGER);
            CREATE TABLE web_channels(id TEXT PRIMARY KEY,tenant_id TEXT);
            CREATE TABLE web_channel_access(user_id TEXT,channel_id TEXT);
            CREATE TABLE web_connections(id_hash TEXT PRIMARY KEY,user_id TEXT,active INTEGER,
                background INTEGER,expires_at INTEGER,state TEXT);''')
        db.execute("INSERT INTO web_channels VALUES('channel','tenant')")
        for i in range(11):
            user=f'user{i:02d}'
            db.execute('INSERT INTO web_users VALUES(?,?,1)',(user,'tenant'))
            db.execute('INSERT INTO web_channel_access VALUES(?,?)',(user,'channel'))
            db.execute('INSERT INTO web_connections VALUES(?,?,1,1,9999,?)',(user,user,'connected'))
    q=NotificationQueue(lambda:sqlite3.connect(path))
    q.register('sub','state','tenant','team','channel')
    class DB:
        now=600
        def clock(self): return self.now
        @contextmanager
        def connect(self):
            with sqlite3.connect(path) as connection: yield connection
        def device(self, value): return {'id_hash':value}
    db=DB()
    runtime=NotificationRuntime(q,db,None,None,None,60)
    job={'tenant_id':'tenant','team_id':'team','channel_id':'channel'}
    first={item['id_hash'] for item in runtime.candidates(job)}
    with sqlite3.connect(path) as connection:
        connection.execute('DELETE FROM web_channel_access')
    db.now+=60
    second={item['id_hash'] for item in runtime.candidates(job)}
    assert len(first)==len(second)==10
    assert len(first | second)==11
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE web_connections SET active=0 WHERE id_hash='user00'")
    assert 'user00' not in {item['id_hash'] for item in runtime.candidates(job)}


def test_hint_candidates_still_require_live_graph_access(tmp_path):
    # No hint, even one retained after access revocation, is an access proof.
    q=NotificationQueue(lambda:sqlite3.connect(tmp_path/'q2.sqlite3'))
    q.register('sub','state','tenant','team','channel')
    q.accept([{'subscriptionId':'sub','clientState':'state'}])
    device={'id_hash':'a','user_id':'alice','tenant_id':'tenant','object_id':'a'}
    class DB:
        def device(self, key): return device
        def begin_sync(self,*args): return 123
        def fail_sync(self,*args): pass
        def commit_snapshot(self,*args): raise AssertionError('unauthorised commit')
    class Tokens:
        def acquire(self,*args): return 'token'
    class Graph:
        def channels(self,*args): return []
    runtime=NotificationRuntime(q,DB(),Tokens(),Graph(),None,60)
    runtime.candidates=lambda job:[device]
    assert runtime.tick()=='channel_not_authorized'


def test_failed_channel_preserves_another_channel_candidate_before_acl_clear(tmp_path):
    now=[1000]
    db=TestDatabase(tmp_path/'full-runtime.sqlite3',Fernet.generate_key().decode(),
                    clock=lambda:now[0])
    db.initialize()
    user=db.save_user('tenant','alice',{'name':'Alice'})
    session=db.issue_session(user,3600)
    raw,_=db.add_connection(session,user,'test-token',3600,'test-cache',background=True)
    device=db.device(digest(raw))
    channels=[{'id':'channel-a','team_id':'team-a','name':'A'},
              {'id':'channel-b','team_id':'team-b','name':'B'}]
    initial_lease=db.begin_sync(user,0)
    db.commit_snapshot(device,channels,[],60,initial_lease)
    q=NotificationQueue(db.connect,clock=db.clock)
    q.register('sub-a','state','tenant','team-a','channel-a')
    q.register('sub-b','state','tenant','team-b','channel-b')

    class Tokens:
        def acquire(self,current):
            assert current['user_id']==user
            return 'delegated'
    class Graph:
        unavailable=True
        def channels(self,*args):
            if self.unavailable:
                raise GraphFailure()
            return channels
    class Source:
        selected=[]
        def collect(self,token,allowed,tenant,storage,user_id,*,selected_channels):
            assert token=='delegated' and tenant=='tenant' and user_id==user
            assert allowed==channels
            self.selected.extend(channel['id'] for channel in selected_channels)
            return []
    graph=Graph(); source=Source()
    runtime=NotificationRuntime(q,db,Tokens(),graph,source,60)
    q.accept([{'subscriptionId':'sub-a','clientState':'state'}])
    assert runtime.tick()=='graph_unavailable'
    with db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM web_channel_access WHERE user_id=?',
                                  (user,)).fetchone()[0]==0
        hints={tuple(row) for row in connection.execute('''SELECT team_id,channel_id,user_id
            FROM graph_candidate_hints''')}
    assert hints=={('team-a','channel-a',user),('team-b','channel-b',user)}

    # A remains delayed for retry. B uses the retained hint even though the real
    # begin_sync/fail_sync path removed all of this user's access rows.
    graph.unavailable=False
    q.accept([{'subscriptionId':'sub-b','clientState':'state'}])
    assert runtime.tick()=='processed'
    assert source.selected==['channel-b']
    assert db.sync_status(user)['state']=='connected'


def test_hint_seed_uses_registered_team_scope_and_same_tenant_active_users(tmp_path):
    path=tmp_path/'hint-scope.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript('''CREATE TABLE web_users(id TEXT PRIMARY KEY,tenant_id TEXT,active INTEGER);
            CREATE TABLE web_channels(id TEXT PRIMARY KEY,tenant_id TEXT);
            CREATE TABLE web_channel_access(user_id TEXT,channel_id TEXT);
            CREATE TABLE web_connections(id_hash TEXT PRIMARY KEY,user_id TEXT,active INTEGER,
                background INTEGER,expires_at INTEGER,state TEXT);''')
        connection.executemany('INSERT INTO web_users VALUES(?,?,?)',
                               [('alice','tenant',1),('bob','foreign',1),('inactive','tenant',0)])
        connection.executemany('INSERT INTO web_channels VALUES(?,?)',
                               [('a','tenant'),('b','tenant'),('other','foreign'),('unmapped','tenant')])
        connection.executemany('INSERT INTO web_channel_access VALUES(?,?)',
                               [('alice','a'),('alice','b'),('alice','other'),('alice','unmapped'),
                                ('bob','a'),('bob','other'),('inactive','a')])
        for user in ('alice','bob','inactive'):
            connection.execute('INSERT INTO web_connections VALUES(?,?,1,1,9999,?)',
                               (user,user,'connected'))
    q=NotificationQueue(lambda:sqlite3.connect(path))
    q.register('sub-a','state','tenant','team-a','a')
    q.register('sub-b','state','tenant','team-b','b')
    q.register('sub-other','state','foreign','team-other','other')
    class DB:
        def clock(self): return 1000
        @contextmanager
        def connect(self):
            with sqlite3.connect(path) as connection: yield connection
        def device(self,value): return {'id_hash':value}
    runtime=NotificationRuntime(q,DB(),None,None,None,60)
    job={'tenant_id':'tenant','team_id':'team-a','channel_id':'a'}
    assert runtime.all_candidates(job)==[{'id_hash':'alice'}]
    with sqlite3.connect(path) as connection:
        assert set(connection.execute('SELECT * FROM graph_candidate_hints'))=={
            ('tenant','team-a','a','alice'),('tenant','team-b','b','alice')}
    assert runtime.all_candidates({**job,'channel_id':'b'})==[]
    assert runtime.all_candidates({**job,'tenant_id':'foreign','team_id':'team-other',
                                   'channel_id':'other'})==[{'id_hash':'bob'}]
