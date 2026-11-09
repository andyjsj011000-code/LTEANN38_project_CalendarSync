"""Environment settings; preserve the defaults of each existing entry point."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Settings:
    database_path: Path
    data_key: str = field(repr=False)
    origin: str = "http://localhost:8080"
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    session_seconds: int = 12 * 60 * 60
    idle_seconds: int = 900
    access_seconds: int = 60
    allow_local_login: bool = False

    def __post_init__(self) -> None:
        Fernet(self.data_key.encode("ascii"))
        url = urlsplit(self.origin)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("WEB_ORIGIN must be an absolute HTTP(S) origin")
        if url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError("WEB_ORIGIN must not contain credentials, path, or query")
        if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            raise ValueError("HTTP is allowed only on loopback for local development")
        if bool(self.tenant_id) != bool(self.client_id) or bool(self.client_id) != bool(
            self.client_secret
        ):
            raise ValueError("Configure all three WEB_ENTRA_* values or none")
        for value in (self.tenant_id, self.client_id):
            if value and str(UUID(value)) != value.lower():
                raise ValueError("Entra tenant and client IDs must be UUIDs")
        if not 300 <= self.session_seconds <= 24 * 60 * 60:
            raise ValueError("Session lifetime must be between 5 minutes and 24 hours")

        if not 60 <= self.idle_seconds <= 3600 or not 15 <= self.access_seconds <= 60:
            raise ValueError("Invalid idle or permission verification lifetime")

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def auth_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @property
    def callback_url(self) -> str:
        return self.origin + "/auth/callback"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        key = env.get("WEB_DATA_KEY", "")
        if not key:
            raise ValueError("WEB_DATA_KEY is required; generate and persist a Fernet key")
        return cls(
            database_path=Path(
                env.get("WEB_DATABASE_PATH", "web_service/var/web-foundation.sqlite3")
            ),
            data_key=key,
            origin=env.get("WEB_ORIGIN", "http://localhost:8080"),
            tenant_id=env.get("WEB_ENTRA_TENANT_ID", "").lower(),
            client_id=env.get("WEB_ENTRA_CLIENT_ID", "").lower(),
            client_secret=env.get("WEB_ENTRA_CLIENT_SECRET", ""),
            session_seconds=int(env.get("WEB_SESSION_SECONDS", "3600")),
            idle_seconds=int(env.get("WEB_IDLE_SECONDS", "900")),
            access_seconds=int(env.get("WEB_ACCESS_SECONDS", "60")),
            allow_local_login=env.get("WEB_ALLOW_LOCAL_LOGIN") == "1",
        )

    @classmethod
    def from_server_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Screen sessions keep the test server's existing 8h/30min defaults."""
        env = os.environ if environ is None else environ
        return replace(
            cls.from_env(env),
            session_seconds=int(env.get("WEB_SESSION_SECONDS", "28800")),
            idle_seconds=int(env.get("WEB_IDLE_SECONDS", "1800")),
        )


@dataclass(frozen=True)
class DirectSourceConfig:
    api_key: str = field(default="", repr=False)
    model: str = "gpt-4o-mini"
    images: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DirectSourceConfig:
        env = os.environ if environ is None else environ
        return cls(
            env.get("WEB_OPENAI_API_KEY", ""),
            env.get("WEB_LLM_MODEL", "gpt-4o-mini"),
            env.get("WEB_AI_IMAGES_ENABLED", "1") == "1",
        )


@dataclass(frozen=True)
class OracleConfig:
    user: str
    password: str = field(repr=False)
    dsn: str = field(repr=False)
    data_key: str = field(repr=False)
    wallet_dir: str = ""
    wallet_password: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OracleConfig | None:
        env = os.environ if environ is None else environ
        names = ("WEB_ORACLE_USER", "WEB_ORACLE_PASSWORD", "WEB_ORACLE_DSN", "WEB_ORACLE_DATA_KEY")
        values = [env.get(name, "") for name in names]
        if not any(values):
            return None
        if not all(values):
            raise ValueError("Configure all WEB_ORACLE_* credentials")
        return cls(
            *values,
            wallet_dir=env.get("WEB_ORACLE_WALLET_DIR", ""),
            wallet_password=env.get("WEB_ORACLE_WALLET_PASSWORD", ""),
        )


@dataclass(frozen=True)
class PushConfig:
    private: str = field(default="", repr=False)
    subject: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PushConfig:
        env = os.environ if environ is None else environ
        return cls(env.get("WEB_VAPID_PRIVATE_KEY", ""), env.get("WEB_VAPID_SUBJECT", ""))
