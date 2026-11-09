"""Best-effort, content-free synchronization progress and failure diagnostics."""

import logging
from pathlib import Path
from time import monotonic


class SyncDiagnostics:
    def __init__(self) -> None:
        self.started = monotonic()
        self.logger = logging.getLogger("uvicorn.error")

    def progress(self, stage: str, outcome: str) -> None:
        # Only fixed stage/outcome labels and elapsed time leave this method.
        # Diagnostics must never change synchronization or expose inputs.
        try:
            self.logger.info(
                "CalendarSync sync stage=%s outcome=%s elapsed_ms=%d",
                stage,
                outcome,
                int((monotonic() - self.started) * 1000),
            )
        except Exception:
            pass

    def failure(self, saved: object) -> None:
        self.progress(
            "failure",
            "recorded" if saved is True else "not_recorded" if saved is False else "completed",
        )

    def exception(self, error: Exception) -> None:
        # Never interpolate exception text or traceback locals: they can
        # contain Graph bodies, SQL parameters, or credential material.
        try:
            safe_types = {
                "Missing",
                "PermissionPending",
                "Conflict",
                "ValueError",
                "TypeError",
                "KeyError",
                "IndexError",
                "RuntimeError",
                "TimeoutError",
                "ConnectionError",
                "IntegrityError",
                "OperationalError",
                "InterfaceError",
                "DataError",
                "SerializationFailure",
                "DeadlockDetected",
                "InvalidToken",
                "AssertionError",
                "ProgrammingError",
                "UndefinedColumn",
                "UndefinedTable",
                "UniqueViolation",
                "ForeignKeyViolation",
                "CheckViolation",
                "NotNullViolation",
                "QueryCanceled",
                "AdminShutdown",
                "ConnectionDoesNotExist",
                "InFailedSqlTransaction",
            }
            kind = type(error).__name__
            kind = kind if kind in safe_types else "OtherError"
            internal = "unknown"
            line = 0
            trace = error.__traceback__
            directory = Path(__file__).resolve().parent
            while trace is not None:
                filename = Path(trace.tb_frame.f_code.co_filename)
                if filename.resolve().parent == directory and filename.suffix == ".py":
                    internal = filename.name
                    line = trace.tb_lineno
                trace = trace.tb_next
            sqlstate = getattr(error, "sqlstate", None)
            if not (
                isinstance(sqlstate, str)
                and len(sqlstate) == 5
                and all("A" <= char <= "Z" or "0" <= char <= "9" for char in sqlstate)
            ):
                sqlstate = "none"
            self.logger.info(
                "CalendarSync sync exception_type=%s internal_file=%s internal_line=%d sqlstate=%s",
                kind,
                internal,
                line,
                sqlstate,
            )
        except Exception:
            pass
