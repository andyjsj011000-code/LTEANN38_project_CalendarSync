"""Explicit PostgreSQL connection configuration; never reads environment values.

Construction/describe() only validate configuration and do not import a driver
or connect. Supply this callable to PostgresDatabase after provisioning its
isolated schema. Each context gets a fresh transactional psycopg 3 connection
with tuple rows. It does not create or migrate schema objects.

TLS defaults to verify-full. Operators may explicitly choose require, but this
module never retries with weaker TLS. Actual provider certificates, CA paths,
driver installation and server connectivity still need integration verification.
"""

from contextlib import contextmanager
import ipaddress
import re
from urllib.parse import parse_qsl, unquote, urlsplit


_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}", re.ASCII)
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", re.ASCII)
_USER = re.compile(r"[A-Za-z0-9_.@+-]{1,128}", re.ASCII)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
_TLS_MODES = frozenset({"verify-full", "require", "disable"})


def _integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid PostgreSQL timeout configuration")
    return value


def _uri_parameters(uri):
    """Return validated connection values without delegating URI options to libpq."""
    try:
        if (
            not isinstance(uri, str)
            or not 0 < len(uri) <= 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in uri)
            or "#" in uri
            or re.search(r"%(?![0-9a-fA-F]{2})", uri)
        ):
            raise ValueError()
        parts = urlsplit(uri)
        if parts.scheme not in {"postgresql", "postgres"} or not parts.netloc:
            raise ValueError()
        host = parts.hostname
        if not host or "%" in host or "," in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(
                _HOST_LABEL.fullmatch(label) is None for label in host.split(".")
            ):
                raise ValueError()
            loopback = host.lower() == "localhost"
        else:
            if address.is_unspecified or address.is_multicast:
                raise ValueError()
            loopback = address.is_loopback
        port = parts.port if parts.port is not None else 5432
        if not 1 <= port <= 65535:
            raise ValueError()
        user = unquote(parts.username or "", encoding="utf-8", errors="strict")
        password = unquote(parts.password or "", encoding="utf-8", errors="strict")
        if _USER.fullmatch(user) is None:
            raise ValueError()
        if (
            len(password) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in password)
            or (not loopback and not password)
        ):
            raise ValueError()
        if not parts.path.startswith("/"):
            raise ValueError()
        database = unquote(parts.path[1:], encoding="utf-8", errors="strict")
        if _DATABASE.fullmatch(database) is None:
            raise ValueError()
        options = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if any(key != "sslmode" for key, _ in options) or len(options) > 1:
            raise ValueError()
        query_mode = options[0][1] if options else None
        if query_mode is not None and query_mode not in _TLS_MODES:
            raise ValueError()
        return (
            {"host": host, "port": port, "user": user, "password": password, "dbname": database},
            query_mode,
            loopback,
        )
    except (TypeError, ValueError, UnicodeError):
        # Neither URI fragments nor decoder/port errors may reveal credentials.
        raise ValueError("Invalid PostgreSQL URI configuration") from None


class PostgresConnectionFactory:
    """Callable transaction factory compatible with the experimental web adapter."""

    __slots__ = (
        "_parameters",
        "schema",
        "sslmode",
        "connect_timeout",
        "statement_timeout_ms",
        "lock_timeout_ms",
        "_sslrootcert",
    )

    def __init__(
        self,
        uri,
        schema,
        *,
        sslmode=None,
        sslrootcert=None,
        connect_timeout=5,
        statement_timeout_ms=15000,
        lock_timeout_ms=5000,
    ):
        if (
            not isinstance(schema, str)
            or _SCHEMA.fullmatch(schema) is None
            or schema.startswith("pg_")
        ):
            raise ValueError("Invalid PostgreSQL schema identifier")
        parameters, query_mode, loopback = _uri_parameters(uri)
        if sslmode is not None and (not isinstance(sslmode, str) or sslmode not in _TLS_MODES):
            raise ValueError("Invalid PostgreSQL TLS configuration")
        if sslmode is not None and query_mode is not None and sslmode != query_mode:
            raise ValueError("Conflicting PostgreSQL TLS configuration")
        mode = sslmode or query_mode or "verify-full"
        if mode == "disable" and not loopback:
            raise ValueError("Plaintext PostgreSQL is allowed only on loopback")
        if sslrootcert is not None and (
            not isinstance(sslrootcert, str)
            or not sslrootcert
            or len(sslrootcert) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in sslrootcert)
        ):
            raise ValueError("Invalid PostgreSQL certificate configuration")
        self._parameters, self.schema, self.sslmode = parameters, schema, mode
        self.connect_timeout = _integer(connect_timeout, 1, 30)
        self.statement_timeout_ms = _integer(statement_timeout_ms, 1, 120000)
        self.lock_timeout_ms = _integer(lock_timeout_ms, 1, self.statement_timeout_ms)
        self._sslrootcert = sslrootcert

    def __repr__(self):
        return (
            f"PostgresConnectionFactory(schema={self.schema!r}, sslmode={self.sslmode!r}, "
            "connection=<redacted>)"
        )

    def describe(self):
        """Validation-only, content-free metadata; no host/user/database/DSN returned."""
        return {
            "backend": "postgresql",
            "schema": self.schema,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout,
            "statement_timeout_ms": self.statement_timeout_ms,
            "lock_timeout_ms": self.lock_timeout_ms,
            "sslrootcert_configured": self._sslrootcert is not None,
        }

    @contextmanager
    def __call__(self):
        try:
            import psycopg
            from psycopg import sql
            from psycopg.rows import tuple_row
        except ImportError:
            raise RuntimeError("Install the optional PostgreSQL driver before connecting") from None
        options = {
            **self._parameters,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout,
            "autocommit": False,
            "row_factory": tuple_row,
        }
        if self._sslrootcert is not None:
            options["sslrootcert"] = self._sslrootcert
        try:
            with psycopg.connect(**options) as connection:
                connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema))
                )
                connection.execute(
                    "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
                    (str(self.statement_timeout_ms) + "ms",),
                )
                connection.execute(
                    "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
                    (str(self.lock_timeout_ms) + "ms",),
                )
                yield connection
        except psycopg.Error:
            # Preserve domain exceptions from adapter methods; suppress only
            # driver errors, which can contain connection values or SQL data.
            raise RuntimeError("PostgreSQL connection or transaction failed") from None
