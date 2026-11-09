"""Opt-in, content-free ingestion progress for one authenticated device."""

from contextlib import contextmanager
import secrets

from .storage import Missing


STATUS_SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS web_ingestion_status (
    device_hash TEXT PRIMARY KEY REFERENCES web_connections(id_hash) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','partial','error')),
    requested_at BIGINT NOT NULL DEFAULT 0, started_at BIGINT NOT NULL DEFAULT 0,
    finished_at BIGINT NOT NULL DEFAULT 0, last_success_at BIGINT NOT NULL DEFAULT 0,
    attempt_token TEXT, attempt_until BIGINT NOT NULL DEFAULT 0,
    report_blob TEXT, error_code TEXT
)"""

COUNT_FIELDS = (
    "channels_total",
    "channels_checked",
    "channels_scanned",
    "messages_seen",
    "messages_analyzed",
    "messages_cached",
    "messages_failed",
)
ERROR_CODES = frozenset(
    {
        "source_unavailable",
        "source_not_configured",
        "verification_failed",
        "reauth_required",
        "account_not_allowed",
        "refresh_unavailable",
        "refresh_busy",
        "interrupted",
    }
)
ATTEMPT_SECONDS = 300


def safe_counts(report):
    if report is None:
        return None
    if not isinstance(report, dict):
        raise ValueError("Invalid ingestion counts")
    result = {}
    for key in COUNT_FIELDS:
        if key in report:
            if type(report[key]) is not int or report[key] < 0:
                raise ValueError("Invalid ingestion count")
            result[key] = report[key]
    return result or None


class PostgresIngestionStatus:
    @contextmanager
    def _status_connect(self):
        with self.connect() as db:
            yield db

    def _status_execute(self, db, query, values=()):
        return db.execute(query, values)

    def _status_device(self, db, device):
        user = self._status_execute(
            db,
            """SELECT active,tenant_id FROM web_users
            WHERE id=%s FOR UPDATE""",
            (device["user_id"],),
        ).fetchone()
        current = self._status_execute(
            db,
            """SELECT user_id,version,active,expires_at,state
            FROM web_connections WHERE id_hash=%s AND user_id=%s FOR UPDATE""",
            (device["id_hash"], device["user_id"]),
        ).fetchone()
        now = int(self.clock())
        if (
            not user
            or not user[0]
            or user[1] != device["tenant_id"]
            or not current
            or type(device.get("version")) is not int
            or current[1] != device["version"]
            or not current[2]
            or current[3] <= now
            or current[4] == "reauth_required"
        ):
            raise Missing()
        return current

    def _status_row(self, db, device_hash):
        return self._status_execute(
            db,
            """SELECT state,requested_at,started_at,finished_at,
            last_success_at,attempt_token,attempt_until,report_blob,error_code
            FROM web_ingestion_status WHERE device_hash=%s FOR UPDATE""",
            (device_hash,),
        ).fetchone()

    def _queue_ingestion_in(self, db, device_hash, now):
        """The caller already owns the active user's device lock."""
        if not getattr(self, "planner_enhancements_enabled", False):
            return
        row = self._status_row(db, device_hash)
        if row and row[0] == "running" and row[6] > now:
            return  # Repeated refresh requests share the active collection.
        self._status_execute(
            db,
            """INSERT INTO web_ingestion_status(device_hash,state,requested_at)
            VALUES(%s,'queued',%s) ON CONFLICT(device_hash) DO UPDATE SET
            state='queued',requested_at=excluded.requested_at,started_at=0,finished_at=0,
            attempt_token=NULL,attempt_until=0,report_blob=NULL,error_code=NULL""",
            (device_hash, now),
        )

    def claim_ingestion(self, device):
        if not getattr(self, "planner_enhancements_enabled", False):
            return None
        with self._status_connect() as db:
            current = self._status_device(db, device)
            row = self._status_row(db, device["id_hash"])
            now = int(self.clock())
            if current[3] <= now:
                raise Missing()
            if row and row[0] == "running" and row[6] > now:
                return None
            token = secrets.token_hex(32)
            requested = row[1] if row and row[0] == "queued" else now
            self._status_execute(
                db,
                """INSERT INTO web_ingestion_status
                (device_hash,state,requested_at,started_at,attempt_token,attempt_until)
                VALUES(%s,'running',%s,%s,%s,%s) ON CONFLICT(device_hash) DO UPDATE SET
                state='running',requested_at=excluded.requested_at,started_at=excluded.started_at,
                finished_at=0,attempt_token=excluded.attempt_token,attempt_until=excluded.attempt_until,
                report_blob=NULL,error_code=NULL""",
                (device["id_hash"], requested, now, token, now + ATTEMPT_SECONDS),
            )
        return token

    def update_ingestion(self, device, attempt, report):
        counts = safe_counts(report)
        with self._status_connect() as db:
            current = self._status_device(db, device)
            row = self._status_row(db, device["id_hash"])
            now = int(self.clock())
            if current[3] <= now:
                raise Missing()
            if not row or row[0] != "running" or row[5] != attempt or row[6] <= now:
                return False
            self._status_execute(
                db,
                "UPDATE web_ingestion_status SET report_blob=%s WHERE device_hash=%s",
                (self.seal(counts) if counts is not None else None, device["id_hash"]),
            )
        return True

    def end_ingestion(self, device, attempt, *, error_code=None, queued=False):
        """Release an unfinished attempt without advancing its success time."""
        if error_code is not None and error_code not in ERROR_CODES:
            error_code = "source_unavailable"
        with self._status_connect() as db:
            current = self._status_device(db, device)
            row = self._status_row(db, device["id_hash"])
            now = int(self.clock())
            if current[3] <= now:
                raise Missing()
            if not row or row[0] != "running" or row[5] != attempt or row[6] <= now:
                return False
            self._status_execute(
                db,
                """UPDATE web_ingestion_status SET state=%s,finished_at=%s,
                attempt_token=NULL,attempt_until=0,error_code=%s WHERE device_hash=%s""",
                (
                    "queued" if queued else "error",
                    0 if queued else now,
                    None if queued else error_code or "interrupted",
                    device["id_hash"],
                ),
            )
        return True

    def finish_ingestion_snapshot_in(self, db, device, context):
        """Called inside the final snapshot transaction, before its expiry fence."""
        current = self._status_device(db, device)
        row = self._status_row(db, device["id_hash"])
        now = int(self.clock())
        if current[3] <= now:
            raise Missing()
        if not row or row[0] != "running" or row[5] != context["attempt"] or row[6] <= now:
            return False
        context["_attempt_until"] = row[6]
        report = context["report"]
        if not isinstance(report, dict) or type(report.get("partial")) is not bool:
            raise ValueError("A complete ingestion outcome is required")
        counts = safe_counts(report) or {}
        previous = safe_counts(self.unseal(row[7])) if row[7] else None
        # An interrupted collect can return only zero counts. Keep observations
        # already persisted by this same fenced attempt instead of inventing loss.
        for key, value in (previous or {}).items():
            if context.get("reset_channels") and key.startswith("channels_"):
                continue
            counts[key] = max(value, counts.get(key, 0))
        partial = report["partial"]
        self._status_execute(
            db,
            """UPDATE web_ingestion_status SET state=%s,finished_at=%s,
            last_success_at=CASE WHEN %s THEN last_success_at ELSE %s END,
            attempt_token=NULL,attempt_until=0,report_blob=%s,error_code=NULL WHERE device_hash=%s""",
            (
                "partial" if partial else "completed",
                now,
                partial,
                now,
                self.seal(counts) if counts else None,
                device["id_hash"],
            ),
        )
        if not partial:
            self._status_execute(
                db,
                "UPDATE web_connections SET ingest_at=%s WHERE id_hash=%s",
                (now + 300, device["id_hash"]),
            )
        finished = int(self.clock())
        if current[3] <= finished or row[6] <= finished:
            raise Missing()
        return True

    def ingestion_status(self, device):
        with self._status_connect() as db:
            current = self._status_device(db, device)
            row = self._status_row(db, device["id_hash"])
            now = int(self.clock())
            if current[3] <= now:
                raise Missing()
        if row is None:
            return {
                "state": "idle",
                "requested_at": None,
                "started_at": None,
                "finished_at": None,
                "last_success_at": None,
                "counts": None,
                "error_code": None,
            }
        state, requested, started, finished, success, _, until, blob, error = row
        if state == "running" and until <= now:
            state, error, finished = "error", "interrupted", until
        return {
            "state": state,
            "requested_at": requested or None,
            "started_at": started or None,
            "finished_at": finished or None,
            "last_success_at": success or None,
            "counts": safe_counts(self.unseal(blob)) if blob else None,
            "error_code": error,
        }
