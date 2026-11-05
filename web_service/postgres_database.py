"""Explicitly injected experimental web adapter; no environment auto-selection.

The caller supplies transactional psycopg 3 connections with tuple rows and a
pre-provisioned isolated web schema. initialize() validates only; it does not
create, alter or migrate tables. The normal hosted entry point remains blocked
until real PostgreSQL and deployment checks have passed.
"""
import hashlib
import json
import time

from cryptography.fernet import Fernet

from .postgres_auth_store import PostgresAuthStore
from .postgres_devices import PostgresDevices
from .postgres_events import PostgresEvents
from .postgres_operations import PostgresOperations
from .postgres_push import PostgresPush
from .postgres_source_cache import PostgresSourceCache
from .postgres_sync import PostgresSync


class PostgresDatabase(PostgresAuthStore, PostgresDevices, PostgresEvents,
                       PostgresSourceCache, PostgresSync, PostgresPush, PostgresOperations):
    storage_backend = 'postgresql-experimental'
    # Closed identifiers: no caller-controlled SQL identifiers are interpolated.
    _REQUIRED_SCHEMA = {
        'web_users': 'id,tenant_id,object_id,profile_blob,active',
        'web_auth_flows': 'id_hash,flow_blob,expires_at',
        'web_sessions': 'id_hash,user_id,csrf_token,expires_at',
        'web_connections': 'id_hash,user_id,credential_blob,token_expires,expires_at,background,active,version,refresh_lease,refresh_until,next_sync,ingest_at,state',
        'web_session_links': 'session_hash,device_hash,last_seen',
        'web_channels': 'id,tenant_id,label_blob',
        'web_channel_access': 'user_id,channel_id,confirmed_at,valid_until',
        'web_channel_preferences': 'user_id,channel_id,enabled',
        'web_events': 'id,channel_id,start_date,end_date,kind,payload_blob,cancelled,revision',
        'web_event_states': 'user_id,event_id,hidden,completed,override_blob,base_revision',
        'web_user_source_versions': 'user_id,event_id,seen_hash',
        'web_changes': 'id,user_id,event_id,source_hash,payload_blob,created_at,status',
        'web_personal_tasks': 'id,user_id,start_date,end_date,payload_blob,completed,hidden,version',
        'web_preferences': 'user_id,payload_blob',
        'web_reminder_rules': 'user_id,item_id,source,payload_blob',
        'web_source_cache': 'cache_key,channel_id,message_id,version,payload_blob,updated_at',
        'web_school_sync': 'user_id,checked_at,state,lease_until,lease_token',
        'web_security_limits': 'key_hash,bucket,attempts',
        'web_security_audit': 'at,event,principal_hash',
        'web_feedback': 'id,user_id,payload_blob,created_at',
        'web_push_subscriptions': 'id,device_hash,endpoint_hash,subscription_blob,created_at',
        'web_deliveries': 'id,subscription_id,state,attempts,next_attempt,lease_until,created_at,lease_token',
        'web_ingest_cursor': 'user_id,cursor',
        'web_worker_lock': 'id,owner,expires_at',
    }

    def __init__(self, connect, data_key, tenant_id, clock=time.time):
        if not callable(connect) or not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError('An explicit connection factory and school tenant are required')
        self.connect, self.clock, self.tenant_id = connect, clock, tenant_id
        self.cipher = Fernet(data_key.encode('ascii'))
        self._audit_key = hashlib.sha256(('school-audit:' + data_key).encode()).digest()

    def seal(self, payload):
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode('ascii')

    def unseal(self, blob):
        return json.loads(self.cipher.decrypt(blob.encode('ascii')))

    def initialize(self):
        try:
            with self.connect() as db:
                if db.execute('SELECT version FROM web_schema_version').fetchall() != [(1,)]:
                    raise ValueError('Unsupported schema version')
                for table, columns in self._REQUIRED_SCHEMA.items():
                    db.execute(f'SELECT {columns} FROM {table} WHERE 1=0')
                for table in ('web_users','web_channels'):
                    if db.execute(f'SELECT 1 FROM {table} WHERE tenant_id<>%s LIMIT 1',
                                  (self.tenant_id,)).fetchone():
                        raise ValueError('The web schema must belong to one school tenant')
        except Exception:
            raise RuntimeError('PostgreSQL web schema validation failed; an explicit reviewed migration is required') from None

    def save_user(self, tenant_id, object_id, profile):
        if tenant_id != self.tenant_id:
            raise ValueError('School tenant mismatch')
        return PostgresAuthStore.save_user(self, tenant_id, object_id, profile)

    def device(self, device_hash):
        current=PostgresDevices.device(self,device_hash)
        return current if current and current['tenant_id']==self.tenant_id else None
