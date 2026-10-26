"""SQLite persistence for the single-instance test server. Credentials are encrypted."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from .school_storage import SchoolDatabase
from .storage import Missing, Conflict, digest

EXTRA_SCHEMA="""
CREATE TABLE IF NOT EXISTS web_connections (
 id_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),credential_blob TEXT NOT NULL,
 token_expires INTEGER NOT NULL,expires_at INTEGER NOT NULL,background INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1,refresh_lease TEXT,refresh_until INTEGER NOT NULL DEFAULT 0,next_sync INTEGER NOT NULL DEFAULT 0,ingest_at INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'connected');
CREATE TABLE IF NOT EXISTS web_session_links (
 session_hash TEXT PRIMARY KEY REFERENCES web_sessions(id_hash) ON DELETE CASCADE,
 device_hash TEXT NOT NULL REFERENCES web_connections(id_hash),last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_source_cache (
 cache_key TEXT PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,version TEXT NOT NULL,
 payload_blob TEXT NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_user_source_versions (
 user_id TEXT NOT NULL REFERENCES web_users(id),event_id TEXT NOT NULL REFERENCES web_events(id),seen_hash TEXT NOT NULL,PRIMARY KEY(user_id,event_id));
CREATE TABLE IF NOT EXISTS web_changes (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),event_id TEXT NOT NULL REFERENCES web_events(id),
 source_hash TEXT NOT NULL,payload_blob TEXT NOT NULL,created_at INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',UNIQUE(user_id,event_id,source_hash));
CREATE TABLE IF NOT EXISTS web_push_subscriptions (
 id TEXT PRIMARY KEY,device_hash TEXT NOT NULL REFERENCES web_connections(id_hash),endpoint_hash TEXT NOT NULL UNIQUE,
 subscription_blob TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_reminder_rules (
 user_id TEXT NOT NULL REFERENCES web_users(id),item_id TEXT NOT NULL,source TEXT NOT NULL,payload_blob TEXT NOT NULL,PRIMARY KEY(user_id,item_id,source));
CREATE TABLE IF NOT EXISTS web_deliveries (
 id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0,lease_until INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_feedback (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),payload_blob TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_ingest_cursor (user_id TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_worker_lock (id INTEGER PRIMARY KEY,owner TEXT NOT NULL,expires_at INTEGER NOT NULL);
"""

def source_hash(e):
    return digest(json.dumps({k:e.get(k) for k in ('title','notes','time','source_url','start_date','end_date','kind','cancelled','source_version')},sort_keys=True,ensure_ascii=False))

class TestDatabase(SchoolDatabase):
    __test__=False
    def initialize(self):
        super().initialize()
        with self.connect() as c: c.executescript(EXTRA_SCHEMA)

    def add_connection(self,raw_session,user_id,token,lifetime,cache='',background=False):
        raw=secrets.token_urlsafe(32); now=int(self.clock())
        expires=now+(30*86400 if background and cache else min(lifetime,3600))
        with self.connect() as c:
            c.execute('INSERT INTO web_connections(id_hash,user_id,credential_blob,token_expires,expires_at,background) VALUES(?,?,?,?,?,?)',(digest(raw),user_id,self.seal({'token':token,'cache':cache}),now+lifetime,expires,int(background and bool(cache))))
            c.execute('INSERT INTO web_session_links VALUES(?,?,?)',(digest(raw_session),digest(raw),now))
        return raw,expires-now

    def device(self,device_hash):
        with self.connect() as c:
            row=c.execute('SELECT d.*,u.tenant_id,u.object_id FROM web_connections d JOIN web_users u ON u.id=d.user_id WHERE d.id_hash=? AND d.active=1 AND d.expires_at>? AND u.active=1',(device_hash,int(self.clock()))).fetchone()
        return dict(row) if row else None

    def session_device(self,session):
        with self.connect() as c: row=c.execute('SELECT device_hash FROM web_session_links WHERE session_hash=?',(session['id_hash'],)).fetchone()
        device=self.device(row[0]) if row else None
        if not device or device['user_id']!=session['user_id']: raise Missing()
        return device

    def school_session(self,raw,idle_seconds):
        session=self.session(raw)
        if not session: return None
        try: self.session_device(session)
        except Missing: return None
        with self.connect() as c:
            row=c.execute('SELECT last_seen FROM web_session_links WHERE session_hash=?',(session['id_hash'],)).fetchone()
            if not row or row[0]+idle_seconds<=self.clock():
                c.execute('DELETE FROM web_sessions WHERE id_hash=?',(session['id_hash'],)); return None
            c.execute('UPDATE web_session_links SET last_seen=? WHERE session_hash=?',(int(self.clock()),session['id_hash']))
        return session

    def lock_refresh(self,device_hash):
        lease=secrets.token_hex(16); now=int(self.clock())
        with self.connect() as c:
            changed=c.execute('UPDATE web_connections SET refresh_lease=?,refresh_until=? WHERE id_hash=? AND active=1 AND expires_at>? AND refresh_until<=?',(lease,now+30,device_hash,now,now)).rowcount
        return lease if changed else None

    def save_refresh(self,device_hash,lease,token,lifetime,cache):
        with self.connect() as c:
            changed=c.execute("UPDATE web_connections SET credential_blob=?,token_expires=?,refresh_lease=NULL,refresh_until=0,version=version+1,state='connected' WHERE id_hash=? AND refresh_lease=? AND active=1 AND expires_at>?",(self.seal({'token':token,'cache':cache}),int(self.clock())+lifetime,device_hash,lease,int(self.clock()))).rowcount
        return bool(changed)

    def fail_connection(self,device_hash,lease=None):
        with self.connect() as c:
            row=c.execute('SELECT user_id,refresh_lease FROM web_connections WHERE id_hash=?',(device_hash,)).fetchone()
            if not row or (lease is not None and row[1]!=lease): return
            c.execute("UPDATE web_connections SET credential_blob=?,token_expires=0,refresh_lease=NULL,refresh_until=0,state='reauth_required' WHERE id_hash=?",(self.seal({}),device_hash))
            c.execute('DELETE FROM web_channel_access WHERE user_id=?',(row[0],))
            c.execute('DELETE FROM web_sessions WHERE id_hash IN (SELECT session_hash FROM web_session_links WHERE device_hash=?)',(device_hash,))

    def revoke_device(self,device_hash,user_id=None):
        with self.connect() as c:
            row=c.execute('SELECT user_id FROM web_connections WHERE id_hash=?',(device_hash,)).fetchone()
            if not row or (user_id and row[0]!=user_id): return
            c.execute('DELETE FROM web_push_subscriptions WHERE device_hash=?',(device_hash,))
            c.execute('DELETE FROM web_sessions WHERE id_hash IN (SELECT session_hash FROM web_session_links WHERE device_hash=?)',(device_hash,))
            c.execute('UPDATE web_connections SET active=0,credential_blob=?,version=version+1 WHERE id_hash=?',(self.seal({}),device_hash))
            c.execute('DELETE FROM web_channel_access WHERE user_id=?',(row[0],))

    def revoke_all(self,user_id):
        with self.connect() as c: ids=[r[0] for r in c.execute('SELECT id_hash FROM web_connections WHERE user_id=?',(user_id,))]
        for device_hash in ids: self.revoke_device(device_hash,user_id)
        super().revoke_all(user_id)

    def due_devices(self,limit=5):
        with self.connect() as c:
            rows=c.execute("SELECT id_hash FROM web_connections WHERE active=1 AND expires_at>? AND next_sync<=? AND state<>'reauth_required' ORDER BY next_sync LIMIT ?",(int(self.clock()),int(self.clock()),limit)).fetchall()
        return [d for r in rows if (d:=self.device(r[0]))]

    def defer_device(self,device_hash,seconds=300):
        with self.connect() as c: c.execute('UPDATE web_connections SET next_sync=? WHERE id_hash=?',(int(self.clock())+seconds,device_hash))

    def worker_lock(self,owner):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE'); c.execute("INSERT OR IGNORE INTO web_worker_lock VALUES(1,'',0)")
            return bool(c.execute('UPDATE web_worker_lock SET owner=?,expires_at=? WHERE id=1 AND expires_at<=?',(owner,int(self.clock())+600,int(self.clock()))).rowcount)

    def worker_unlock(self,owner):
        with self.connect() as c: c.execute('UPDATE web_worker_lock SET expires_at=0 WHERE id=1 AND owner=?',(owner,))

    def cached_analysis(self,key,version):
        with self.connect() as c: row=c.execute('SELECT payload_blob FROM web_source_cache WHERE cache_key=? AND version=?',(key,version)).fetchone()
        return self.unseal(row[0]) if row else None

    def cache_analysis(self,key,channel,message,version,events):
        with self.connect() as c:
            c.execute('INSERT INTO web_source_cache VALUES(?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET version=excluded.version,payload_blob=excluded.payload_blob,updated_at=excluded.updated_at',(key,channel,message,version,self.seal(events),int(self.clock())))

    def commit_snapshot(self,device,channels,events,ttl,lease):
        now=int(self.clock()); user=device['user_id']; tenant=device['tenant_id']; allowed={ch['id'] for ch in channels}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active=c.execute('SELECT 1 FROM web_connections d JOIN web_users u ON u.id=d.user_id WHERE d.id_hash=? AND d.active=1 AND d.expires_at>? AND u.active=1',(device['id_hash'],now)).fetchone()
            proof=c.execute('SELECT lease_until FROM web_school_sync WHERE user_id=?',(user,)).fetchone()
            if not active or not proof or proof[0]!=lease or lease<=now: raise Missing()
            c.execute('DELETE FROM web_channel_access WHERE user_id=?',(user,))
            for ch in channels:
                prior=c.execute('SELECT tenant_id FROM web_channels WHERE id=?',(ch['id'],)).fetchone()
                if prior and prior[0]!=tenant: raise ValueError('Tenant mismatch')
                c.execute('INSERT INTO web_channels VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET label_blob=excluded.label_blob',(ch['id'],tenant,self.seal({'name':ch['name']})))
                c.execute('INSERT INTO web_channel_access VALUES(?,?,?,?)',(user,ch['id'],now,now+ttl))
            for e in events:
                if e['channel_id'] not in allowed: raise ValueError('Source channel mismatch')
                event_id=e['id'] if e['id'].startswith('direct:') else 'oracle:'+e['id']
                fields={k:e.get(k) for k in ('title','notes','time','source_url','start_date','end_date','kind','source_version')}; fields['cancelled']=bool(e.get('cancelled',False))
                version=source_hash(fields)
                old=c.execute('SELECT channel_id FROM web_events WHERE id=?',(event_id,)).fetchone()
                if old and old[0]!=e['channel_id']: raise ValueError('Event channel changed')
                if not old:
                    c.execute('INSERT INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob,cancelled) VALUES(?,?,?,?,?,?,?)',(event_id,e['channel_id'],e['start_date'],e['end_date'],e['kind'],self.seal(fields),int(fields['cancelled'])))
                seen=c.execute('SELECT seen_hash FROM web_user_source_versions WHERE user_id=? AND event_id=?',(user,event_id)).fetchone()
                if not seen:
                    c.execute('INSERT INTO web_user_source_versions VALUES(?,?,?)',(user,event_id,version))
                    # First discovery shows the current source, even if another user saw an older version.
                    c.execute('INSERT INTO web_event_states(user_id,event_id,override_blob) VALUES(?,?,?) ON CONFLICT(user_id,event_id) DO UPDATE SET override_blob=excluded.override_blob',(user,event_id,self.seal(fields)))
                elif seen[0]!=version:
                    change_id=digest(user+'|'+event_id+'|'+version)
                    c.execute("UPDATE web_changes SET status='superseded' WHERE user_id=? AND event_id=? AND source_hash<>? AND status='pending'",(user,event_id,version))
                    c.execute('INSERT OR IGNORE INTO web_changes(id,user_id,event_id,source_hash,payload_blob,created_at) VALUES(?,?,?,?,?,?)',(change_id,user,event_id,version,self.seal(fields),now))
            c.execute("UPDATE web_school_sync SET checked_at=?,state='connected',lease_until=0 WHERE user_id=?",(now,user))

    def _shared(self,user_id,tenant_id):
        items=super()._shared(user_id,tenant_id)
        with self.connect() as c: overrides={r[0]:self.unseal(r[1]) for r in c.execute('SELECT event_id,override_blob FROM web_event_states WHERE user_id=? AND override_blob IS NOT NULL',(user_id,))}
        return [{**e,**{k:v for k,v in overrides.get(e['id'],{}).items() if k in {'title','notes','time','source_url','start_date','end_date','kind','cancelled'}}} for e in items]

    def changes(self,user,tenant):
        accessible={e['id']:e for e in self._shared(user,tenant)}
        with self.connect() as c: rows=c.execute("SELECT * FROM web_changes WHERE user_id=? AND status='pending' ORDER BY created_at DESC",(user,)).fetchall()
        return [{'id':r['id'],'event_id':r['event_id'],'before':accessible[r['event_id']],'after':self.unseal(r['payload_blob']),'created_at':r['created_at']} for r in rows if r['event_id'] in accessible]

    def resolve_change(self,user,tenant,change_id,accept):
        allowed={e['id'] for e in self._shared(user,tenant)}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM web_changes WHERE id=? AND user_id=? AND status='pending'",(change_id,user)).fetchone()
            if not row or row['event_id'] not in allowed: raise Missing()
            if accept: c.execute('UPDATE web_event_states SET override_blob=? WHERE user_id=? AND event_id=?',(row['payload_blob'],user,row['event_id']))
            c.execute('UPDATE web_user_source_versions SET seen_hash=? WHERE user_id=? AND event_id=?',(row['source_hash'],user,row['event_id']))
            c.execute("UPDATE web_changes SET status=? WHERE id=?",('accepted' if accept else 'kept',change_id))

    def save_feedback(self,user,message):
        key=secrets.token_hex(16)
        with self.connect() as c: c.execute('INSERT INTO web_feedback VALUES(?,?,?,?)',(key,user,self.seal({'message':message}),int(self.clock())))
        return key

    def rules(self,user,item_id,source):
        with self.connect() as c: row=c.execute('SELECT payload_blob FROM web_reminder_rules WHERE user_id=? AND item_id=? AND source=?',(user,item_id,source)).fetchone()
        return self.unseal(row[0]) if row else {'offsets':[-1440,-60],'custom_times':[]}

    def save_rules(self,user,item_id,source,values):
        with self.connect() as c: c.execute('INSERT INTO web_reminder_rules VALUES(?,?,?,?) ON CONFLICT(user_id,item_id,source) DO UPDATE SET payload_blob=excluded.payload_blob',(user,item_id,source,self.seal(values)))

    def subscribe(self,device,subscription):
        endpoint=digest(subscription['endpoint']); key=secrets.token_hex(16)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active=c.execute("SELECT 1 FROM web_connections d JOIN web_users u ON u.id=d.user_id WHERE d.id_hash=? AND d.user_id=? AND d.active=1 AND d.expires_at>? AND d.background=1 AND d.state<>'reauth_required' AND u.active=1",(device['id_hash'],device['user_id'],int(self.clock()))).fetchone()
            if not active: raise Missing()
            old=c.execute('SELECT id,device_hash FROM web_push_subscriptions WHERE endpoint_hash=?',(endpoint,)).fetchone()
            if old and old[1]!=device['id_hash']: raise Conflict()
            if old:
                c.execute('UPDATE web_push_subscriptions SET subscription_blob=? WHERE id=?',(self.seal(subscription),old[0])); return old[0]
            if c.execute('SELECT count(*) FROM web_push_subscriptions WHERE device_hash=?',(device['id_hash'],)).fetchone()[0]>=3: raise Conflict()
            c.execute('INSERT INTO web_push_subscriptions VALUES(?,?,?,?,?)',(key,device['id_hash'],endpoint,self.seal(subscription),int(self.clock())))
        return key

    def subscriptions(self,device_hash):
        with self.connect() as c: rows=c.execute('SELECT * FROM web_push_subscriptions WHERE device_hash=?',(device_hash,)).fetchall()
        return [{**dict(r),'subscription':self.unseal(r['subscription_blob'])} for r in rows]

    def unsubscribe(self,device_hash):
        with self.connect() as c: c.execute('DELETE FROM web_push_subscriptions WHERE device_hash=?',(device_hash,))

    def unsubscribe_subscription(self,device_hash,subscription_id):
        with self.connect() as c: c.execute('DELETE FROM web_push_subscriptions WHERE id=? AND device_hash=?',(subscription_id,device_hash))

    def claim_delivery(self,key,subscription_id):
        now=int(self.clock())
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute("SELECT 1 FROM web_push_subscriptions s JOIN web_connections d ON d.id_hash=s.device_hash JOIN web_users u ON u.id=d.user_id WHERE s.id=? AND d.active=1 AND d.expires_at>? AND d.background=1 AND d.state<>'reauth_required' AND u.active=1",(subscription_id,now)).fetchone()
            if not current: return False
            c.execute('DELETE FROM web_deliveries WHERE created_at<?',(now-35*86400,))
            c.execute('INSERT OR IGNORE INTO web_deliveries(id,subscription_id,created_at) VALUES(?,?,?)',(key,subscription_id,now))
            return bool(c.execute("UPDATE web_deliveries SET lease_until=?,attempts=attempts+1 WHERE id=? AND state='pending' AND attempts<5 AND next_attempt<=? AND lease_until<=?",(now+30,key,now,now)).rowcount)

    def finish_delivery(self,key,success):
        with self.connect() as c: c.execute('UPDATE web_deliveries SET state=?,lease_until=0,next_attempt=? WHERE id=?',('sent' if success else 'pending',int(self.clock())+60,key))

    def next_channels(self,user,channels,count):
        if not channels: return []
        channels=sorted(channels,key=lambda ch:ch['id'])
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE'); c.execute('INSERT OR IGNORE INTO web_ingest_cursor VALUES(?,0)',(user,))
            offset=c.execute('SELECT cursor FROM web_ingest_cursor WHERE user_id=?',(user,)).fetchone()[0]%len(channels)
            selected=[channels[(offset+i)%len(channels)] for i in range(min(count,len(channels)))]
            c.execute('UPDATE web_ingest_cursor SET cursor=? WHERE user_id=?',((offset+len(selected))%len(channels),user))
        return selected

    def cached_messages(self,channel):
        with self.connect() as c: rows=c.execute('SELECT message_id,version,payload_blob FROM web_source_cache WHERE channel_id=? ORDER BY updated_at ASC',(channel,)).fetchall()
        return {r[0]:{'version':r[1],'events':self.unseal(r[2])} for r in rows}

    def catalog_events(self,channels):
        # The bounded recheck queue must not truncate the authoritative catalog.
        result=[]
        with self.connect() as c:
            for channel in dict.fromkeys(channels):
                for row in c.execute('SELECT payload_blob FROM web_source_cache WHERE channel_id=? ORDER BY message_id',(channel,)):
                    result.extend(self.unseal(row[0]))
        return result

    def begin_sync(self,user_id,ttl):
        now=int(self.clock())
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE'); c.execute('INSERT OR IGNORE INTO web_school_sync(user_id) VALUES(?)',(user_id,))
            row=c.execute('SELECT checked_at,lease_until FROM web_school_sync WHERE user_id=?',(user_id,)).fetchone()
            if row[1]>now or row[0]+ttl>now: return False
            lease=now+300
            c.execute("UPDATE web_school_sync SET lease_until=?,state='checking' WHERE user_id=?",(lease,user_id))
            c.execute('DELETE FROM web_channel_access WHERE user_id=?',(user_id,))
        return lease

    def mark_ingested(self,device_hash):
        with self.connect() as c: c.execute('UPDATE web_connections SET ingest_at=? WHERE id_hash=?',(int(self.clock())+300,device_hash))

    def has_pending_change(self,user,event):
        with self.connect() as c: return bool(c.execute("SELECT 1 FROM web_changes WHERE user_id=? AND event_id=? AND status='pending'",(user,event)).fetchone())

    def touch_analysis(self,key):
        with self.connect() as c: c.execute('UPDATE web_source_cache SET updated_at=? WHERE cache_key=?',(int(self.clock()),key))

    def maintenance(self):
        now=int(self.clock())
        with self.connect() as c:
            expired=[r[0] for r in c.execute('SELECT id_hash FROM web_connections WHERE active=1 AND expires_at<=?',(now,))]
        for device_hash in expired: self.revoke_device(device_hash)
        with self.connect() as c:
            c.execute('DELETE FROM web_sessions WHERE expires_at<=?',(now,))
            c.execute('DELETE FROM web_auth_flows WHERE expires_at<=?',(now,))
            c.execute('DELETE FROM web_security_audit WHERE at<?',(now-7*86400,))
            c.execute('DELETE FROM web_source_cache WHERE updated_at<?',(now-90*86400,))
