"""SQLite transaction adapter for opt-in device ingestion status."""

from contextlib import contextmanager

from .postgres_ingestion_status import PostgresIngestionStatus


class SQLiteIngestionStatus(PostgresIngestionStatus):
    @contextmanager
    def _status_connect(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _status_execute(self, db, query, values=()):
        return db.execute(query.replace(" FOR UPDATE", "").replace("%s", "?"), values)
