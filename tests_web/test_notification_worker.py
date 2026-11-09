import sqlite3
from web_service.notification_queue import NotificationQueue
from web_service.notification_worker import process_notification


def setup(tmp_path):
    inbox = NotificationQueue(lambda: sqlite3.connect(tmp_path / "q.sqlite3"))
    inbox.register("sub", "state", "tenant", "team", "channel")
    inbox.accept([{"subscriptionId": "sub", "clientState": "state"}])
    device = {"id_hash": "d", "user_id": "alice", "tenant_id": "tenant", "object_id": "a"}

    class DB:
        active = True
        commits = []

        def device(self, identity):
            return device if self.active else None

        def begin_sync(self, user, ttl):
            return 123

        def fail_sync(self, *args):
            pass

        def commit_snapshot(self, dev, channels, events, ttl, lease):
            if not self.active:
                raise ValueError("revoked")
            self.commits.append((channels, events))

    class Tokens:
        def acquire(self, d):
            return "delegated"

    class Graph:
        allowed = [{"id": "channel", "team_id": "team"}, {"id": "other", "team_id": "team"}]

        def channels(self, *args):
            return self.allowed

    class Source:
        selected = None

        def collect(self, token, channels, tenant, db, user_id, *, selected_channels):
            self.selected = selected_channels
            return ["cached catalogue"]

    return inbox, device, DB(), Tokens(), Graph(), Source()


def test_processes_only_authorized_channel_and_commits_full_access_list(tmp_path):
    q, d, db, t, g, s = setup(tmp_path)
    assert process_notification(q, d, db, t, g, s, 60) == "processed"
    assert s.selected == [{"id": "channel", "team_id": "team"}]
    assert db.commits == [(g.allowed, ["cached catalogue"])]
    assert q.claim() is None


def test_denied_or_revoked_user_never_processes_and_job_is_retained(tmp_path):
    q, d, db, t, g, s = setup(tmp_path)
    now = [1000]
    q.clock = lambda: now[0]
    g.allowed = []
    assert process_notification(q, d, db, t, g, s, 60) == "channel_not_authorized"
    assert s.selected is None and not db.commits
    assert q.claim() is None
    now[0] += 60
    db.active = False
    assert process_notification(q, d, db, t, g, s, 60) == "reauth_required"
    assert q.claim() is None
    now[0] += 60
    assert q.claim() is not None


def test_revocation_during_processing_never_commits_and_retains_job(tmp_path):
    q, d, db, t, g, s = setup(tmp_path)
    now = [1000]
    q.clock = lambda: now[0]

    def revoke(*args, **kwargs):
        db.active = False
        return ["private"]

    s.collect = revoke
    assert process_notification(q, d, db, t, g, s, 60) == "processing_failed"
    assert not db.commits and q.claim() is None
    now[0] += 60
    assert q.claim() is not None
