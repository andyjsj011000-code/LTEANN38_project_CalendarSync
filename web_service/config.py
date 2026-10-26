from dataclasses import dataclass, field
import os
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

    def __post_init__(self):
        Fernet(self.data_key.encode("ascii"))
        url = urlsplit(self.origin)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("WEB_ORIGIN must be an absolute HTTP(S) origin")
        if url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError("WEB_ORIGIN must not contain credentials, path, or query")
        if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            raise ValueError("HTTP is allowed only on loopback for local development")
        if bool(self.tenant_id) != bool(self.client_id) or bool(self.client_id) != bool(self.client_secret):
            raise ValueError("Configure all three WEB_ENTRA_* values or none")
        for value in (self.tenant_id, self.client_id):
            if value and str(UUID(value)) != value.lower():
                raise ValueError("Entra tenant and client IDs must be UUIDs")
        if not 300 <= self.session_seconds <= 24 * 60 * 60:
            raise ValueError("Session lifetime must be between 5 minutes and 24 hours")

        if not 60 <= self.idle_seconds <= 3600 or not 15 <= self.access_seconds <= 60:
            raise ValueError("Invalid idle or permission verification lifetime")

    @property
    def secure_cookies(self):
        return self.origin.startswith("https://")

    @property
    def auth_configured(self):
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @property
    def callback_url(self):
        return self.origin + "/auth/callback"

    @classmethod
    def from_env(cls):
        key = os.environ.get("WEB_DATA_KEY", "")
        if not key:
            raise ValueError("WEB_DATA_KEY is required; generate and persist a Fernet key")
        return cls(
            database_path=Path(os.environ.get("WEB_DATABASE_PATH", "web_service/var/web-foundation.sqlite3")),
            data_key=key,
            origin=os.environ.get("WEB_ORIGIN", "http://localhost:8080"),
            tenant_id=os.environ.get("WEB_ENTRA_TENANT_ID", "").lower(),
            client_id=os.environ.get("WEB_ENTRA_CLIENT_ID", "").lower(),
            client_secret=os.environ.get("WEB_ENTRA_CLIENT_SECRET", ""),
            session_seconds=int(os.environ.get("WEB_SESSION_SECONDS", "3600")),
            idle_seconds=int(os.environ.get("WEB_IDLE_SECONDS", "900")),
            access_seconds=int(os.environ.get("WEB_ACCESS_SECONDS", "60")),
            allow_local_login=os.environ.get("WEB_ALLOW_LOCAL_LOGIN") == "1",
        )
