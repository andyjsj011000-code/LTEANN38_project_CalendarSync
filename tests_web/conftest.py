from datetime import datetime, timezone
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from web_service.api import SESSION_COOKIE, create_app
from web_service.config import Settings
from web_service.storage import Database

TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
ALICE = "33333333-3333-4333-8333-333333333333"
BOB = "44444444-4444-4444-8444-444444444444"


class FakeIdentity:
    """Test-only provider. Real runtime always uses MicrosoftIdentity."""

    def __init__(self, settings, clock):
        self.settings, self.clock = settings, clock
        self.claim_overrides = {}

    def begin(self):
        return {"state": "test-state", "nonce": "server-only-nonce", "code_verifier": "server-only-verifier", "auth_uri": "https://login.microsoftonline.com/authorize?state=test-state"}

    def finish(self, flow, query):
        if flow["state"] != query.get("state") or query.get("code") != "valid-test-code":
            raise ValueError("Invalid test code or state")
        return {"tid": TENANT, "oid": ALICE, "sub": "subject-a", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT, "exp": self.clock() + 3600, "name": "Alice", "preferred_username": "a@sshs.hs.kr", **self.claim_overrides}


@pytest.fixture
def env(tmp_path):
    now = [int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())]
    settings = Settings(database_path=tmp_path / "web.sqlite3", data_key=Fernet.generate_key().decode(), origin="https://calendar.example.test", tenant_id=TENANT, client_id=CLIENT, client_secret="test-only-secret")
    db = Database(settings.database_path, settings.data_key, clock=lambda: now[0])
    provider = FakeIdentity(settings, db.clock)
    app = create_app(settings, db, provider)
    alice = db.save_user(TENANT, ALICE, {"name": "Alice", "email": "a@sshs.hs.kr"})
    bob = db.save_user(TENANT, BOB, {"name": "Bob", "email": "b@sshs.hs.kr"})
    clients = []

    def client(user=None):
        c = TestClient(app, base_url=settings.origin)
        clients.append(c)
        if user:
            c.cookies.set(SESSION_COOKIE, db.issue_session(user, settings.session_seconds))
            c.headers["x-csrf-token"] = c.get("/api/me").json()["csrf_token"]
        return c

    state = SimpleNamespace(now=now, settings=settings, db=db, identity=provider, app=app, alice=alice, bob=bob, client=client)
    yield state
    for c in clients:
        c.close()


@pytest.fixture
def shared(env):
    with env.db.connect() as c:
        for channel in ("channel-a", "channel-b"):
            c.execute("INSERT INTO web_channels VALUES(?,?,?)", (channel, TENANT, env.db.seal({"name": channel})))
        for user, channel in ((env.alice, "channel-a"), (env.bob, "channel-b")):
            c.execute("INSERT INTO web_channel_access VALUES(?,?,?,?)", (user, channel, env.now[0], env.now[0] + 3600))
        for event, channel, title in (("event-a", "channel-a", "Alice channel deadline"), ("event-b", "channel-b", "Bob channel deadline")):
            c.execute("INSERT INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob) VALUES(?,?,?,?,?,?)", (event, channel, "2026-10-20", "2026-10-24", "assignment", env.db.seal({"title": title, "notes": "source-private-content", "time": "18:00"})))
    return env
