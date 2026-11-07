"""Unwired PostgreSQL candidate for device snapshot synchronization.

Requires the draft ``web_school_sync.lease_token`` column; existing databases
need an explicit migration before integration. ``begin_sync`` returns an opaque
token, never a timestamp. psycopg 3 transactional context managers with tuple
rows and caller-owned encryption are required. No network/schema initialization
occurs here. PostgreSQL concurrency has not been validated by the SQLite tests.

All writers must share the user -> device -> sync row lock order used here and
by PostgresDevices. Shared channels/events are locked in sorted identity order.
Token refresh changes the device version: callers must obtain a fresh device
snapshot after refreshing, before fetching a snapshot they intend to commit.
"""
import json
import secrets
import time
from datetime import date

from .storage import Missing, digest


def _source_hash(fields):
    keys = ('title', 'notes', 'time', 'source_url', 'start_date', 'end_date',
            'kind', 'cancelled', 'source_version')
    return digest(json.dumps({key: fields.get(key) for key in keys},
                             sort_keys=True, ensure_ascii=False))


class PostgresSync:
    def __init__(self, connect, seal, unseal, clock=time.time):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock

    @staticmethod
    def _seconds(value, *, allow_zero=False):
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
            raise ValueError('Duration must be an integer number of seconds')

    def sync_status(self, user_id):
        with self.connect() as db:
            row = db.execute('''SELECT checked_at,state,lease_until FROM web_school_sync
                WHERE user_id=%s''', (user_id,)).fetchone()
        return (dict(zip(('checked_at', 'state', 'lease_until'), row)) if row else
                {'checked_at': 0, 'state': 'not_connected', 'lease_until': 0})

    def begin_sync(self, user_id, ttl, *, preserve_access=False):
        self._seconds(ttl, allow_zero=True)
        if type(preserve_access) is not bool or (preserve_access and not getattr(self, 'shared_ingestion_enabled', False)):
            raise ValueError('Preserved access requires shared ingestion')
        with self.connect() as db:
            user = db.execute('SELECT active FROM web_users WHERE id=%s FOR UPDATE',
                              (user_id,)).fetchone()
            if not user or not user[0]:
                return False
            db.execute('''INSERT INTO web_school_sync(user_id) VALUES(%s)
                ON CONFLICT(user_id) DO NOTHING''', (user_id,))
            row = db.execute('''SELECT checked_at,state,lease_until FROM web_school_sync
                WHERE user_id=%s FOR UPDATE''', (user_id,)).fetchone()
            now = int(self.clock())
            if ((row[1] == 'checking' and row[2] > now) or
                    (row[0] > 0 and row[0] + ttl > now)):
                return False
            token = secrets.token_hex(32)
            db.execute('''UPDATE web_school_sync SET lease_token=%s,lease_until=%s,
                state='checking' WHERE user_id=%s''', (token, now + 180, user_id))
            # A shared final verification still owns the per-user lease. Keep
            # only its previously verified ACL with the ORIGINAL expiry while
            # Graph runs; failure clears it and success replaces it atomically.
            if not preserve_access:
                db.execute('DELETE FROM web_channel_access WHERE user_id=%s', (user_id,))
        return token

    def fail_sync(self, user_id, state, lease):
        if (not isinstance(state, str) or not state or len(state) > 64 or
                state in {'checking', 'connected'}):
            raise ValueError('A failure state is required')
        if not isinstance(lease, str) or not lease:
            return False
        with self.connect() as db:
            user = db.execute('SELECT active FROM web_users WHERE id=%s FOR UPDATE',
                              (user_id,)).fetchone()
            if user is None:
                return False
            now = int(self.clock())
            changed = db.execute('''UPDATE web_school_sync SET checked_at=%s,state=%s,
                lease_until=0,lease_token=NULL WHERE user_id=%s AND state='checking'
                AND lease_token=%s AND lease_until>%s''',
                (now, state, user_id, lease, now)).rowcount
            if changed:
                db.execute('DELETE FROM web_channel_access WHERE user_id=%s', (user_id,))
        return bool(changed)

    @staticmethod
    def _snapshot(channels, events):
        channels, events = list(channels), list(events)
        channel_ids = set()
        for channel in channels:
            if (not isinstance(channel.get('id'), str) or not channel['id'] or
                    not isinstance(channel.get('name'), str) or channel['id'] in channel_ids):
                raise ValueError('Invalid or duplicate snapshot channel')
            channel_ids.add(channel['id'])
        event_ids = set()
        result = []
        for event in events:
            if event.get('channel_id') not in channel_ids:
                raise ValueError('Source channel mismatch')
            if not isinstance(event.get('id'), str) or not event['id']:
                raise ValueError('Event identity is required')
            event_id = event['id'] if event['id'].startswith('direct:') else 'oracle:' + event['id']
            if event_id in event_ids:
                raise ValueError('Duplicate snapshot event')
            event_ids.add(event_id)
            start, end = date.fromisoformat(event['start_date']), date.fromisoformat(event['end_date'])
            if start > end or not isinstance(event.get('kind'), str) or not event['kind']:
                raise ValueError('Invalid event dates or kind')
            result.append((event_id, event))
        return sorted(channels, key=lambda row: row['id']), sorted(result, key=lambda row: row[0])

    def commit_snapshot(self, device, channels, events, ttl, lease):
        self._seconds(ttl)
        channels, events = self._snapshot(channels, events)
        if not isinstance(lease, str) or not lease:
            raise Missing()
        user_id, tenant = device['user_id'], device['tenant_id']
        if (isinstance(device.get('version'), bool) or not isinstance(device.get('version'), int) or
                any(row.get('tenant_id', tenant) != tenant for row in channels) or
                any(row.get('tenant_id', tenant) != tenant for _, row in events)):
            raise Missing()
        with self.connect() as db:
            user = db.execute('SELECT active,tenant_id FROM web_users WHERE id=%s FOR UPDATE',
                              (user_id,)).fetchone()
            current = db.execute('''SELECT user_id,version,active,expires_at,state
                FROM web_connections WHERE id_hash=%s AND user_id=%s FOR UPDATE''',
                (device['id_hash'], user_id)).fetchone()
            proof = db.execute('''SELECT lease_token,lease_until,state FROM web_school_sync
                WHERE user_id=%s FOR UPDATE''', (user_id,)).fetchone()
            now = int(self.clock())
            if (not user or not user[0] or user[1] != tenant or not current or
                    current[0] != user_id or current[1] != device.get('version') or
                    not current[2] or current[3] <= now or current[4] == 'reauth_required' or
                    not proof or proof[0] != lease or proof[1] <= now or proof[2] != 'checking'):
                raise Missing()
            if getattr(self, 'shared_ingestion_enabled', False):
                # Lock the cache before events, matching retention. Re-read the
                # authoritative shared catalog so an older caller snapshot can
                # never restore retired events or overwrite a newer analysis.
                from .postgres_retention import decode_cache_payload
                latest = []
                rows = db.execute('''SELECT channel_id,payload_blob FROM web_source_cache
                    WHERE channel_id=ANY(%s) ORDER BY cache_key FOR UPDATE''',
                    ([channel['id'] for channel in channels],)).fetchall()
                for channel_id, blob in rows:
                    latest.extend(decode_cache_payload(self.unseal(blob), channel_id)['events'])
                _, events = self._snapshot(channels, latest)
            db.execute('DELETE FROM web_channel_access WHERE user_id=%s', (user_id,))
            for channel in channels:
                # The conditional upsert guards a cross-tenant first-insert race.
                row = db.execute('''INSERT INTO web_channels(id,tenant_id,label_blob)
                    VALUES(%s,%s,%s) ON CONFLICT(id) DO UPDATE SET label_blob=excluded.label_blob
                    WHERE web_channels.tenant_id=excluded.tenant_id RETURNING tenant_id''',
                    (channel['id'], tenant, self.seal({'name': channel['name']}))).fetchone()
                if row is None:
                    raise ValueError('Channel tenant mismatch')
                db.execute('''INSERT INTO web_channel_access(user_id,channel_id,confirmed_at,valid_until)
                    VALUES(%s,%s,%s,%s)''', (user_id, channel['id'], now, now + ttl))
            for event_id, event in events:
                fields = {key: event.get(key) for key in ('title', 'notes', 'time', 'source_url',
                          'start_date', 'end_date', 'kind', 'source_version')}
                fields['cancelled'] = bool(event.get('cancelled', False))
                version, payload = _source_hash(fields), self.seal(fields)
                db.execute('''INSERT INTO web_events
                    (id,channel_id,start_date,end_date,kind,payload_blob,cancelled)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING''',
                    (event_id, event['channel_id'], event['start_date'], event['end_date'],
                     event['kind'], payload, int(fields['cancelled'])))
                old = db.execute('SELECT channel_id FROM web_events WHERE id=%s FOR UPDATE',
                                 (event_id,)).fetchone()
                if old[0] != event['channel_id']:
                    raise ValueError('Event channel changed')
                seen = db.execute('''SELECT seen_hash FROM web_user_source_versions
                    WHERE user_id=%s AND event_id=%s''', (user_id, event_id)).fetchone()
                if seen is None:
                    db.execute('INSERT INTO web_user_source_versions VALUES(%s,%s,%s)',
                               (user_id, event_id, version))
                    db.execute('''INSERT INTO web_event_states(user_id,event_id,override_blob)
                        VALUES(%s,%s,%s) ON CONFLICT(user_id,event_id) DO UPDATE
                        SET override_blob=excluded.override_blob''', (user_id, event_id, payload))
                elif seen[0] != version:
                    change_id = digest(user_id + '|' + event_id + '|' + version)
                    db.execute('''UPDATE web_changes SET status='superseded'
                        WHERE user_id=%s AND event_id=%s AND source_hash<>%s AND status='pending' ''',
                        (user_id, event_id, version))
                    db.execute('''INSERT INTO web_changes
                        (id,user_id,event_id,source_hash,payload_blob,created_at)
                        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE
                        SET payload_blob=excluded.payload_blob,
                        created_at=CASE WHEN web_changes.status='pending' THEN web_changes.created_at
                        ELSE excluded.created_at END,status='pending' ''',
                        (change_id, user_id, event_id, version, payload, now))
                else:
                    db.execute('''UPDATE web_changes SET status='superseded'
                        WHERE user_id=%s AND event_id=%s AND status='pending' ''', (user_id, event_id))
            # Shared-row locks above can wait even while this user's own rows
            # remain locked. Time expiry must still fence the final commit.
            finished=int(self.clock())
            if current[3]<=finished or proof[1]<=finished or now+ttl<=finished:
                raise Missing()
            db.execute('''UPDATE web_school_sync SET checked_at=%s,state='connected',
                lease_until=0,lease_token=NULL WHERE user_id=%s''', (now, user_id))
