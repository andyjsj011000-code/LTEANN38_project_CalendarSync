"""Unwired PostgreSQL source-cache candidate for delegated ingestion.

The schema keys channel IDs globally and has no tenant column. Callers MUST
obtain channel IDs from the current user's verified Graph channel list, and
must never pass arbitrary user-supplied IDs to these methods. This repository
is not an authorization boundary. It neither provisions nor connects a DB.
Connections must be transactional psycopg 3 context managers with tuple rows.
Cache keys must be derived server-side from those verified channel IDs.
Use only after enforcing the application's single-school tenant boundary;
multi-tenant support requires tenant-scoped cache keys/schema first.
"""
import time

from .storage import digest


class PostgresSourceCache:
    def __init__(self, connect, seal, unseal, clock=time.time):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock

    @staticmethod
    def _validate_events(events, channel):
        if not isinstance(events, list) or any(
                not isinstance(event, dict) or event.get('channel_id') != channel
                for event in events):
            raise ValueError('Source cache channel mismatch')
        return events

    def cached_analysis(self, key, version):
        with self.connect() as db:
            row = db.execute('''SELECT channel_id,payload_blob FROM web_source_cache
                WHERE cache_key=%s AND version=%s''', (key, version)).fetchone()
        return self._validate_events(self.unseal(row[1]), row[0]) if row else None

    def cache_analysis(self, key, channel, message, version, events):
        # Match DirectSource's deterministic key contract, preventing multiple
        # rows for the same source through alternative caller-supplied keys.
        if key != digest(channel + '|' + message):
            raise ValueError('Source cache key mismatch')
        self._validate_events(events, channel)
        blob = self.seal(events)
        with self.connect() as db:
            row = db.execute('''INSERT INTO web_source_cache
                (cache_key,channel_id,message_id,version,payload_blob,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(cache_key) DO UPDATE SET version=excluded.version,
                payload_blob=excluded.payload_blob,updated_at=excluded.updated_at
                WHERE web_source_cache.channel_id=excluded.channel_id
                  AND web_source_cache.message_id=excluded.message_id
                RETURNING cache_key''',
                (key, channel, message, version, blob, int(self.clock()))).fetchone()
            if row is None:
                raise ValueError('Source cache identity changed')

    def cached_messages(self, channel):
        with self.connect() as db:
            rows = db.execute('''SELECT message_id,version,payload_blob FROM web_source_cache
                WHERE channel_id=%s ORDER BY updated_at ASC,message_id ASC''', (channel,)).fetchall()
        return {row[0]: {'version': row[1],
                         'events': self._validate_events(self.unseal(row[2]), channel)}
                for row in rows}

    def catalog_events(self, channels):
        # Preserve the complete authorized catalog, not the bounded recheck set.
        result = []
        with self.connect() as db:
            for channel in dict.fromkeys(channels):
                rows = db.execute('''SELECT payload_blob FROM web_source_cache
                    WHERE channel_id=%s ORDER BY message_id''', (channel,)).fetchall()
                for row in rows:
                    result.extend(self._validate_events(self.unseal(row[0]), channel))
        return result

    def touch_analysis(self, key):
        with self.connect() as db:
            db.execute('UPDATE web_source_cache SET updated_at=%s WHERE cache_key=%s',
                       (int(self.clock()), key))
