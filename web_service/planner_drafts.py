"""Encrypted personal-task drafts with monotonic, deletion-safe revisions.

The caller supplies authenticated sessions and transactional connections. This
module never installs its schema or contacts Graph. A cleared/expired draft
keeps its small revision row so an older autosave cannot recreate the content.
"""
from contextlib import contextmanager
from datetime import date
from typing import Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .storage import Conflict, Missing


DRAFT_RETENTION_SECONDS = 7 * 86400
DRAFT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS web_task_drafts (
 user_id TEXT PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
 payload_blob TEXT, version BIGINT NOT NULL CHECK(version>=1),
 updated_at BIGINT NOT NULL, expires_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS web_task_drafts_expiry ON web_task_drafts(expires_at);
"""


class DraftFields(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(default='', max_length=500)
    notes: str = Field(default='', max_length=10000)
    start_date: str = Field(default='', max_length=10)
    end_date: str = Field(default='', max_length=10)
    time: str | None = Field(default='', max_length=5,
                              pattern=r'^(?:|(?:[01]\d|2[0-3]):[0-5]\d)$')
    display_mode: Literal['daily', 'selected', 'final'] = 'daily'
    selected_dates: list[str] = Field(default_factory=list, max_length=3660)

    @field_validator('start_date', 'end_date')
    @classmethod
    def valid_optional_date(cls, value):
        if value and date.fromisoformat(value).isoformat() != value:
            raise ValueError('Invalid draft date')
        return value

    @field_validator('selected_dates')
    @classmethod
    def valid_selected_dates(cls, values):
        for value in values:
            if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError('Invalid selected draft date')
        return values


class TaskDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    task_id: str | None = Field(default=None, min_length=1, max_length=200)
    base_version: int | None = Field(default=None, ge=1)
    fields: DraftFields

    @model_validator(mode='after')
    def personal_context(self):
        if (self.task_id is None) != (self.base_version is None):
            raise ValueError('Draft task and version must be supplied together')
        return self


class DraftWrite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)
    draft: TaskDraft


class _TaskDrafts:
    _draft_placeholder = '%s'
    _draft_row_lock = ' FOR UPDATE'

    @contextmanager
    def _draft_connection(self):
        with self.connect() as db:
            yield db

    @staticmethod
    def _draft_version(value):
        if type(value) is not int or value < 0:
            raise ValueError('A non-negative draft revision is required')

    def _draft_user(self, db, user_id, tenant_id):
        p = self._draft_placeholder
        user = db.execute(f'SELECT tenant_id,active FROM web_users WHERE id={p}' +
                          self._draft_row_lock, (user_id,)).fetchone()
        if not user or user[0] != tenant_id or not user[1]:
            raise Missing()

    def _draft_load(self, db, user_id, now):
        p = self._draft_placeholder
        row = db.execute(f'''SELECT payload_blob,version,updated_at,expires_at
            FROM web_task_drafts WHERE user_id={p}''' + self._draft_row_lock,
                         (user_id,)).fetchone()
        if row and row[0] is not None and row[3] <= now:
            # The tombstone is kept indefinitely; only content has a TTL.
            db.execute(f'''UPDATE web_task_drafts SET payload_blob=NULL,
                version=version+1,updated_at={p},expires_at=0 WHERE user_id={p}''',
                       (now, user_id))
            return (None, row[1] + 1, now, 0)
        return row

    def _draft_result(self, row):
        if row is None:
            return {'draft': None, 'version': 0, 'updated_at': None, 'expires_at': None}
        return {'draft': self.unseal(row[0]) if row[0] is not None else None,
                'version': row[1], 'updated_at': row[2],
                'expires_at': row[3] if row[0] is not None else None}

    def get_task_draft(self, user_id, tenant_id):
        with self._draft_connection() as db:
            self._draft_user(db, user_id, tenant_id)
            row = self._draft_load(db, user_id, int(self.clock()))
            return self._draft_result(row)

    def save_task_draft(self, user_id, tenant_id, draft, version):
        self._draft_version(version)
        draft = TaskDraft.model_validate(draft).model_dump()
        p = self._draft_placeholder
        with self._draft_connection() as db:
            self._draft_user(db, user_id, tenant_id)
            now = int(self.clock())
            row = self._draft_load(db, user_id, now)
            if (row[1] if row else 0) != version:
                raise Conflict()
            if draft['task_id'] is not None:
                own = db.execute(f'''SELECT id FROM web_personal_tasks
                    WHERE user_id={p} AND id={p}''' + self._draft_row_lock,
                                 (user_id, draft['task_id'])).fetchone()
                if own is None:
                    raise Missing()
            blob = self.seal(draft)
            expiry = now + DRAFT_RETENTION_SECONDS
            if row is None:
                db.execute(f'''INSERT INTO web_task_drafts
                    (user_id,payload_blob,version,updated_at,expires_at)
                    VALUES({p},{p},{p},{p},{p})''',
                           (user_id, blob, 1, now, expiry))
            else:
                db.execute(f'''UPDATE web_task_drafts SET payload_blob={p},
                    version=version+1,updated_at={p},expires_at={p} WHERE user_id={p}''',
                           (blob, now, expiry, user_id))
            return self._draft_result((blob, version + 1, now, expiry))

    def clear_task_draft(self, user_id, tenant_id, version):
        self._draft_version(version)
        p = self._draft_placeholder
        with self._draft_connection() as db:
            self._draft_user(db, user_id, tenant_id)
            now = int(self.clock())
            row = self._draft_load(db, user_id, now)
            if (row[1] if row else 0) != version:
                raise Conflict()
            if row is None:
                db.execute(f'''INSERT INTO web_task_drafts
                    (user_id,payload_blob,version,updated_at,expires_at)
                    VALUES({p},NULL,1,{p},0)''', (user_id, now))
            else:
                db.execute(f'''UPDATE web_task_drafts SET payload_blob=NULL,
                    version=version+1,updated_at={p},expires_at=0 WHERE user_id={p}''',
                           (now, user_id))
            return self._draft_result((None, version + 1, now, 0))

    def prune_task_drafts(self):
        # Only an already-expired blob is removed. Revisions and owners remain.
        p = self._draft_placeholder
        with self._draft_connection() as db:
            now = int(self.clock())
            return db.execute(f'''UPDATE web_task_drafts SET payload_blob=NULL,
                version=version+1,updated_at={p},expires_at=0
                WHERE payload_blob IS NOT NULL AND expires_at<={p}''',
                              (now, now)).rowcount


class PostgresTaskDrafts(_TaskDrafts):
    """Mixin for the explicitly enabled PostgreSQL planner repository."""


class SQLiteTaskDrafts(_TaskDrafts):
    """Same draft contract in the single-instance development repository."""
    _draft_placeholder = '?'
    _draft_row_lock = ''

    @contextmanager
    def _draft_connection(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db


def install_draft_routes(app, database, require_session):
    """Install only after the caller explicitly enables/provisions the feature."""
    @app.get('/api/task-draft')
    def read_draft(session=Depends(require_session)):
        return database.get_task_draft(session['user_id'], session['tenant_id'])

    @app.put('/api/task-draft')
    def write_draft(body: DraftWrite, session=Depends(require_session)):
        return database.save_task_draft(session['user_id'], session['tenant_id'],
                                        body.draft.model_dump(), body.version)

    @app.delete('/api/task-draft')
    def remove_draft(version: int = Query(ge=0), session=Depends(require_session)):
        return database.clear_task_draft(session['user_id'], session['tenant_id'], version)
