"""PostgreSQL operations mixin; no schema creation or live connection occurs here.

The facade supplies transactional psycopg 3 connections with tuple rows,
encryption, a clock, and a stable secret ``_audit_key``. Device operations use
PostgresDevices' ``device`` and ``_lock_device`` methods, whose writers lock
the user before the device. SQLite-shim tests cannot verify PostgreSQL locks.
Worker owners must be fresh random values for every acquisition, as used by
ServerIntegration.tick; the schema cannot distinguish reused owner generations.
"""
import hashlib
import hmac
import secrets
import time

from .storage import Missing


class PostgresOperations:
    def __init__(self, connect, seal, unseal, clock=time.time, *, audit_key=None):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock
        if audit_key is not None:
            if not isinstance(audit_key, bytes) or not audit_key:
                raise ValueError('A nonempty secret audit key is required')
            self._audit_key = audit_key

    @staticmethod
    def _operation_integer(value, *, allow_zero=False):
        if (isinstance(value, bool) or not isinstance(value, int) or
                value < (0 if allow_zero else 1)):
            raise ValueError('A valid integer is required')

    def fingerprint(self, value):
        key = getattr(self, '_audit_key', None)
        if not isinstance(key, bytes) or not key:
            raise RuntimeError('Configure a stable secret audit key')
        return hmac.new(key, value.encode('utf-8'), hashlib.sha256).hexdigest()

    def audit(self, event, principal):
        if not isinstance(event, str) or not event or len(event) > 128:
            raise ValueError('Invalid audit event')
        principal_hash = self.fingerprint(principal)
        now = int(self.clock())
        with self.connect() as db:
            db.execute('DELETE FROM web_security_audit WHERE at<%s', (now - 7 * 86400,))
            db.execute('INSERT INTO web_security_audit VALUES(%s,%s,%s)',
                       (now, event, principal_hash))

    def allow(self, key, limit):
        self._operation_integer(limit)
        bucket, hashed = int(self.clock()) // 60, self.fingerprint(key)
        with self.connect() as db:
            db.execute('DELETE FROM web_security_limits WHERE bucket<%s', (bucket - 2,))
            row = db.execute('''INSERT INTO web_security_limits(key_hash,bucket,attempts)
                VALUES(%s,%s,1) ON CONFLICT(key_hash,bucket) DO UPDATE
                SET attempts=web_security_limits.attempts+1 RETURNING attempts''',
                (hashed, bucket)).fetchone()
        return row[0] <= limit

    def save_feedback(self, user, message):
        key, payload = secrets.token_hex(16), self.seal({'message': message})
        with self.connect() as db:
            active = db.execute('SELECT active FROM web_users WHERE id=%s FOR UPDATE',
                                (user,)).fetchone()
            if not active or not active[0]:
                raise Missing()
            db.execute('INSERT INTO web_feedback VALUES(%s,%s,%s,%s)',
                       (key, user, payload, int(self.clock())))
        return key

    def due_devices(self, limit=5):
        self._operation_integer(limit)
        if limit > 1000:
            raise ValueError('Device batch is too large')
        now = int(self.clock())
        with self.connect() as db:
            rows = db.execute('''SELECT d.id_hash FROM web_connections d
                JOIN web_users u ON u.id=d.user_id WHERE d.active=1 AND u.active=1
                AND d.expires_at>%s AND d.next_sync<=%s AND d.state<>'reauth_required'
                ORDER BY d.next_sync,d.id_hash LIMIT %s''', (now, now, limit)).fetchall()
        return [device for row in rows if (device := self.device(row[0]))]

    def _operational_device(self, db, device_hash):
        device = self._lock_device(db, device_hash)
        if (not device or not device['user_active'] or not device['active'] or
                device['expires_at'] <= int(self.clock()) or device['state'] == 'reauth_required'):
            return None
        return device

    def defer_device(self, device_hash, seconds=300):
        self._operation_integer(seconds, allow_zero=True)
        with self.connect() as db:
            if not self._operational_device(db, device_hash):
                return False
            db.execute('UPDATE web_connections SET next_sync=%s WHERE id_hash=%s',
                       (int(self.clock()) + seconds, device_hash))
        return True

    def mark_ingested(self, device_hash):
        with self.connect() as db:
            if not self._operational_device(db, device_hash):
                return False
            db.execute('UPDATE web_connections SET ingest_at=%s WHERE id_hash=%s',
                       (int(self.clock()) + 300, device_hash))
        return True

    @staticmethod
    def _operation_owner(owner):
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ValueError('A fresh worker owner is required')

    def worker_lock(self, owner):
        self._operation_owner(owner)
        with self.connect() as db:
            db.execute("INSERT INTO web_worker_lock(id,owner,expires_at) VALUES(1,'',0) ON CONFLICT(id) DO NOTHING")
            row = db.execute('SELECT expires_at FROM web_worker_lock WHERE id=1 FOR UPDATE').fetchone()
            # The row lock may wait: compute the expiry after acquiring it.
            now = int(self.clock())
            if row[0] > now:
                return False
            db.execute('UPDATE web_worker_lock SET owner=%s,expires_at=%s WHERE id=1',
                       (owner, now + 600))
        return True

    def worker_unlock(self, owner):
        self._operation_owner(owner)
        with self.connect() as db:
            changed = db.execute("UPDATE web_worker_lock SET owner='',expires_at=0 WHERE id=1 AND owner=%s",
                                 (owner,)).rowcount
        return bool(changed)

    def next_channels(self, user, channels, count):
        self._operation_integer(count, allow_zero=True)
        channels = sorted(channels, key=lambda channel: channel['id'])
        if not channels or not count:
            return []
        if len({channel['id'] for channel in channels}) != len(channels):
            raise ValueError('Duplicate channel candidates')
        # The caller must supply the current Graph-verified channel list. This
        # cursor chooses candidates and is never proof of delegated access.
        with self.connect() as db:
            active = db.execute('SELECT active FROM web_users WHERE id=%s FOR UPDATE',
                                (user,)).fetchone()
            if not active or not active[0]:
                raise Missing()
            db.execute('''INSERT INTO web_ingest_cursor(user_id,cursor) VALUES(%s,0)
                ON CONFLICT(user_id) DO NOTHING''', (user,))
            row = db.execute('SELECT cursor FROM web_ingest_cursor WHERE user_id=%s FOR UPDATE',
                             (user,)).fetchone()
            offset = row[0] % len(channels)
            selected = [channels[(offset + i) % len(channels)] for i in range(min(count, len(channels)))]
            db.execute('UPDATE web_ingest_cursor SET cursor=%s WHERE user_id=%s',
                       ((offset + len(selected)) % len(channels), user))
        return selected

    def _expire_operational_device(self, device_hash):
        # Do not check expiry and then call revoke_device in a new transaction:
        # a connection could have been extended in the gap. Recheck under the
        # same user/device locks and perform the revocation while they are held.
        with self.connect() as db:
            device = self._lock_device(db, device_hash)
            if not device or not device['active'] or device['expires_at'] > int(self.clock()):
                return False
            db.execute('DELETE FROM web_push_subscriptions WHERE device_hash=%s', (device_hash,))
            db.execute('''DELETE FROM web_sessions WHERE id_hash IN
                (SELECT session_hash FROM web_session_links WHERE device_hash=%s)''', (device_hash,))
            db.execute('''UPDATE web_connections SET active=0,credential_blob=%s,token_expires=0,
                refresh_lease=NULL,refresh_until=0,version=version+1 WHERE id_hash=%s''',
                (self.seal({}), device_hash))
            db.execute('DELETE FROM web_channel_access WHERE user_id=%s', (device['user_id'],))
        return True

    def maintenance(self):
        with self.connect() as db:
            expired = db.execute('''SELECT id_hash FROM web_connections
                WHERE active=1 AND expires_at<=%s ORDER BY user_id,id_hash''',
                (int(self.clock()),)).fetchall()
        for device_hash, in expired:
            self._expire_operational_device(device_hash)
        now = int(self.clock())
        with self.connect() as db:
            db.execute('DELETE FROM web_sessions WHERE expires_at<=%s', (now,))
            db.execute('DELETE FROM web_auth_flows WHERE expires_at<=%s', (now,))
            db.execute('DELETE FROM web_security_audit WHERE at<%s', (now - 7 * 86400,))
            db.execute('DELETE FROM web_security_limits WHERE bucket<%s', (now // 60 - 2,))
            db.execute('DELETE FROM web_source_cache WHERE updated_at<%s', (now - 90 * 86400,))
