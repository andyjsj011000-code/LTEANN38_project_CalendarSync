"""Encrypted, account-owned personal-task recovery with a seven-day lifetime.

The application opts in and installs the fixed schema explicitly. These mixins
never initialize schema or contact notification services. User locks serialize
archive/restore with ordinary personal-task writes and account deactivation.
"""
from uuid import uuid4

from fastapi import Depends
from pydantic import Field

from .schemas import Input, TaskInput
from .storage import Conflict, Missing


TRASH_SECONDS = 7 * 24 * 60 * 60
TRASH_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS web_task_trash (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
        task_id TEXT NOT NULL,
        payload_blob TEXT NOT NULL,
        deleted_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        UNIQUE(user_id,task_id))''',
    '''CREATE INDEX IF NOT EXISTS web_task_trash_expiry
        ON web_task_trash(expires_at,id)''',
)


class TrashTaskInput(Input):
    version: int = Field(ge=1, strict=True)


class _TaskTrash:
    _trash_postgres = False

    def _trash_enabled(self):
        if not getattr(self, 'planner_enhancements_enabled', False):
            raise Missing()

    def _trash_execute(self, db, query, values=()):
        # Only fixed SQL in this module passes through this small dialect shim.
        if not self._trash_postgres:
            query = query.replace('%s', '?').replace(' FOR UPDATE', '')
        return db.execute(query, values)

    def _trash_begin(self, db):
        if not self._trash_postgres:
            db.execute('BEGIN IMMEDIATE')

    def _trash_lock_user(self, db, user_id, tenant_id=None):
        user = self._trash_execute(db, '''SELECT tenant_id,active FROM web_users
            WHERE id=%s FOR UPDATE''', (user_id,)).fetchone()
        if not user or not user[1] or (tenant_id is not None and user[0] != tenant_id):
            raise Missing()

    @staticmethod
    def _trash_version(version):
        if type(version) is not int or version < 1:
            raise ValueError('A task version is required')

    @staticmethod
    def _trash_snapshot(snapshot):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('task'), dict):
            raise ValueError('Invalid archived task')
        task = snapshot['task']
        if (not isinstance(task.get('payload'), dict) or
                type(task.get('version')) is not int or task['version'] < 1 or
                type(task.get('completed')) is not bool or type(task.get('hidden')) is not bool):
            raise ValueError('Invalid archived task')
        value = TaskInput.model_validate(task['payload'])
        if (value.start_date.isoformat() != task.get('start_date') or
                value.end_date.isoformat() != task.get('end_date')):
            raise ValueError('Invalid archived task dates')
        rule = snapshot.get('reminder_rule')
        if rule is not None and not isinstance(rule, dict):
            raise ValueError('Invalid archived reminder')
        return task, rule

    @staticmethod
    def _trash_value(task_id, task, version=None):
        return {**task['payload'], 'id': task_id, 'source': 'personal',
                'kind': 'personal', 'cancelled': False,
                'completed': task['completed'], 'hidden': task['hidden'],
                'version': task['version'] if version is None else version}

    def trash_task(self, user_id, task_id, version):
        self._trash_enabled()
        self._trash_version(version)
        identity = str(uuid4())
        with self.connect() as db:
            self._trash_begin(db)
            self._trash_lock_user(db, user_id)
            row = self._trash_execute(db, '''SELECT id,start_date,end_date,payload_blob,
                completed,hidden,version FROM web_personal_tasks
                WHERE user_id=%s AND id=%s FOR UPDATE''', (user_id, task_id)).fetchone()
            if row is None:
                raise Missing()
            if row[6] != version:
                raise Conflict()
            previous = self._trash_execute(db, '''SELECT id FROM web_task_trash
                WHERE user_id=%s AND task_id=%s FOR UPDATE''', (user_id, task_id)).fetchone()
            if previous is not None:
                raise Conflict()
            rule = self._trash_execute(db, '''SELECT payload_blob FROM web_reminder_rules
                WHERE user_id=%s AND item_id=%s AND source='personal' ''',
                (user_id, task_id)).fetchone()
            snapshot = {'task': {'payload': self.unseal(row[3]),
                        'start_date': row[1], 'end_date': row[2],
                        'completed': bool(row[4]), 'hidden': bool(row[5]), 'version': row[6]},
                        'reminder_rule': self.unseal(rule[0]) if rule else None}
            self._trash_snapshot(snapshot)
            now = int(self.clock())
            expires = now + TRASH_SECONDS
            self._trash_execute(db, '''INSERT INTO web_task_trash
                (id,user_id,task_id,payload_blob,deleted_at,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s)''',
                (identity, user_id, task_id, self.seal(snapshot), now, expires))
            removed = self._trash_execute(db, '''DELETE FROM web_personal_tasks
                WHERE user_id=%s AND id=%s AND version=%s''',
                (user_id, task_id, version)).rowcount
            if removed != 1:
                raise Conflict()
            self._trash_execute(db, '''DELETE FROM web_reminder_rules
                WHERE user_id=%s AND item_id=%s AND source='personal' ''', (user_id, task_id))
        return {'id': identity, 'task_id': task_id, 'deleted_at': now, 'expires_at': expires}

    def list_task_trash(self, user_id, tenant_id):
        self._trash_enabled()
        with self.connect() as db:
            self._trash_begin(db)
            self._trash_lock_user(db, user_id, tenant_id)
            rows = self._trash_execute(db, '''SELECT id,task_id,payload_blob,deleted_at,expires_at
                FROM web_task_trash WHERE user_id=%s AND expires_at>%s
                ORDER BY deleted_at DESC,id''', (user_id, int(self.clock()))).fetchall()
            items = []
            for identity, task_id, blob, deleted, expires in rows:
                task, _ = self._trash_snapshot(self.unseal(blob))
                values = self._trash_value(task_id, task)
                items.append({key: values.get(key) for key in (
                    'title', 'start_date', 'end_date', 'time', 'completed', 'hidden')} |
                    {'id': identity, 'task_id': task_id, 'deleted_at': deleted, 'expires_at': expires})
        return {'items': items, 'retention_days': 7}

    def restore_task_trash(self, user_id, tenant_id, trash_id):
        self._trash_enabled()
        with self.connect() as db:
            self._trash_begin(db)
            self._trash_lock_user(db, user_id, tenant_id)
            row = self._trash_execute(db, '''SELECT task_id,payload_blob,expires_at
                FROM web_task_trash WHERE id=%s AND user_id=%s FOR UPDATE''',
                (trash_id, user_id)).fetchone()
            if row is None or row[2] <= int(self.clock()):
                raise Missing()
            task_id, blob, expires = row
            task, rule = self._trash_snapshot(self.unseal(blob))
            # Incrementing the original version prevents stale pre-delete
            # writes in another tab from becoming valid after recovery.
            version = task['version'] + 1
            inserted = self._trash_execute(db, '''INSERT INTO web_personal_tasks
                (id,user_id,start_date,end_date,payload_blob,completed,hidden,version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id''',
                (task_id, user_id, task['start_date'], task['end_date'], self.seal(task['payload']),
                 int(task['completed']), int(task['hidden']), version)).fetchone()
            if inserted is None:
                raise Conflict()
            if rule is None:
                self._trash_execute(db, '''DELETE FROM web_reminder_rules
                    WHERE user_id=%s AND item_id=%s AND source='personal' ''', (user_id, task_id))
            else:
                self._trash_execute(db, '''INSERT INTO web_reminder_rules
                    (user_id,item_id,source,payload_blob) VALUES(%s,%s,'personal',%s)
                    ON CONFLICT(user_id,item_id,source) DO UPDATE
                    SET payload_blob=excluded.payload_blob''', (user_id, task_id, self.seal(rule)))
            if expires <= int(self.clock()):
                raise Missing()
            consumed = self._trash_execute(db, '''DELETE FROM web_task_trash
                WHERE id=%s AND user_id=%s''', (trash_id, user_id)).rowcount
            if consumed != 1:
                raise Conflict()
        # Delivery records are deliberately preserved. Their keys prevent a
        # restored schedule from sending an already-delivered notification again.
        return self._trash_value(task_id, task, version)

    def prune_task_trash(self, limit=100):
        self._trash_enabled()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid cleanup batch size')
        with self.connect() as db:
            self._trash_begin(db)
            return self._trash_execute(db, '''DELETE FROM web_task_trash WHERE id IN (
                SELECT id FROM web_task_trash WHERE expires_at<=%s
                ORDER BY expires_at,id LIMIT %s)''', (int(self.clock()), limit)).rowcount


class PostgresTaskTrash(_TaskTrash):
    _trash_postgres = True


class SQLiteTaskTrash(_TaskTrash):
    pass


def install_trash_routes(app, db, require_session):
    """Install opt-in routes using the application's existing auth boundary."""
    @app.post('/api/tasks/{task_id}/trash')
    def trash(task_id: str, body: TrashTaskInput, session=Depends(require_session)):
        return db.trash_task(session['user_id'], task_id, body.version)

    @app.get('/api/task-trash')
    def listing(session=Depends(require_session)):
        return db.list_task_trash(session['user_id'], session['tenant_id'])

    @app.post('/api/task-trash/{trash_id}/restore')
    def restore(trash_id: str, session=Depends(require_session)):
        return db.restore_task_trash(session['user_id'], session['tenant_id'], trash_id)
