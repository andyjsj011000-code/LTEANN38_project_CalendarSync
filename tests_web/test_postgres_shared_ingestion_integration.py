"""Real PostgreSQL evidence for cross-account channel leases and late writers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from test_ingestion_status import CatalogSource, seed_catalog, session_device
from test_postgres_server_integration import postgres_server as postgres_server

from web_service.postgres_shared_schema import enable_shared_channel_schema
from web_service.storage import Missing, PermissionPending, digest


@pytest.fixture
def shared(postgres_server):
    server = postgres_server
    enable_shared_channel_schema(server.db.connect, server.settings.tenant_id)
    server.db.shared_ingestion_enabled = True
    return server


def channel():
    return {"id": "shared-class", "team_id": "verified-team", "name": "School · General"}


def test_additive_schema_is_idempotent_and_preserves_existing_rows(shared):
    s = shared
    c = s.client()
    with s.db.connect() as db:
        before = db.execute("SELECT id,profile_blob FROM web_users ORDER BY id").fetchall()
    enable_shared_channel_schema(s.db.connect, s.settings.tenant_id)
    with s.db.connect() as db:
        assert db.execute("SELECT id,profile_blob FROM web_users ORDER BY id").fetchall() == before
    assert c.get("/api/me").status_code == 200


def test_shared_channel_only_one_collector_and_empty_success_is_fresh(shared):
    s, ch = shared, channel()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: s.db.claim_channel_ingestion(ch), range(2)))
    owners = [lease for lease in claims if lease]
    assert len(owners) == 1
    assert not s.db.channel_ingestion_fresh(ch)
    assert s.db.finish_channel_ingestion(ch, owners[0], True)
    assert s.db.channel_ingestion_fresh(ch)
    assert s.db.claim_channel_ingestion(ch) is None
    assert s.db.catalog_events([ch["id"]]) == []
    s.now[0] += 301
    assert s.db.claim_channel_ingestion(ch)


def test_expired_collector_cannot_replace_or_touch_newer_analysis(shared):
    s, ch = shared, channel()
    old = s.db.claim_channel_ingestion(ch)
    key = digest(ch["id"] + "|post")
    first = {"channel_id": ch["id"], "id": "direct:one", "title": "First"}
    assert s.db.cache_analysis(key, ch["id"], "post", "v1", [first], ingestion_lease=old)
    s.now[0] += 181
    new = s.db.claim_channel_ingestion(ch)
    latest = {**first, "title": "Newer"}
    assert s.db.cache_analysis(key, ch["id"], "post", "v2", [latest], ingestion_lease=new)
    assert s.db.cache_analysis(key, ch["id"], "post", "v1", [first], ingestion_lease=old) is False
    assert s.db.touch_analysis(key, ingestion_lease=old) is False
    assert s.db.finish_channel_ingestion(ch, old, True) is False
    assert s.db.cached_analysis(key, "v2") == [latest]
    assert s.db.finish_channel_ingestion(ch, new, True)


def test_failed_scan_remains_due_and_channel_identity_is_fixed(shared):
    s, ch = shared, channel()
    lease = s.db.claim_channel_ingestion(ch)
    assert s.db.finish_channel_ingestion(ch, lease, False)
    assert not s.db.channel_ingestion_fresh(ch)
    assert s.db.claim_channel_ingestion(ch)
    with pytest.raises(ValueError):
        s.db.claim_channel_ingestion({**ch, "team_id": "other-team"})
    with pytest.raises(ValueError):
        s.db.claim_channel_ingestion({**ch, "tenant_id": "other-school"})


def test_final_verification_lease_preserves_only_original_unexpired_access(shared):
    s = shared
    client = s.client()
    assert client.get("/api/today").status_code == 200
    session = s.db.session(client.cookies.get("__Host-calendarsync_session"))
    user, tenant = session["user_id"], session["tenant_id"]
    with s.db.connect() as db:
        before = db.execute(
            """SELECT channel_id,confirmed_at,valid_until FROM web_channel_access
            WHERE user_id=%s ORDER BY channel_id""",
            (user,),
        ).fetchall()
    assert before
    lease = s.db.begin_sync(user, 0, preserve_access=True)
    assert lease and s.db.begin_sync(user, 0) is False
    with s.db.connect() as db:
        assert (
            db.execute(
                """SELECT channel_id,confirmed_at,valid_until FROM web_channel_access
            WHERE user_id=%s ORDER BY channel_id""",
                (user,),
            ).fetchall()
            == before
        )
    assert s.db.channels(user, tenant)
    # Holding a final verification lease does not extend an earlier permission.
    s.now[0] = min(row[2] for row in before)
    assert s.db.channels(user, tenant) == []
    assert s.db.fail_sync(user, "verification_failed", lease)
    with s.db.connect() as db:
        assert (
            db.execute("SELECT 1 FROM web_channel_access WHERE user_id=%s", (user,)).fetchone()
            is None
        )


@pytest.mark.parametrize("operation", ["cache_write", "touch"])
def test_lease_expiry_during_cache_row_lock_wait_prevents_late_mutation(
    shared, monkeypatch, operation
):
    s, ch = shared, channel()
    lease = s.db.claim_channel_ingestion(ch)
    key = digest(ch["id"] + "|blocked-post")
    prior = {"channel_id": ch["id"], "id": "direct:blocked", "title": "Prior analysis"}
    assert s.db.cache_analysis(key, ch["id"], "blocked-post", "v1", [prior], ingestion_lease=lease)

    first_check = Event()
    checks = []
    active_lease = s.db._active_channel_lease

    def observed_check(db, channel_id, supplied_lease):
        valid = active_lease(db, channel_id, supplied_lease)
        checks.append(valid)
        if len(checks) == 1:
            first_check.set()
        return valid

    monkeypatch.setattr(s.db, "_active_channel_lease", observed_check)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with s.db.connect() as blocker:
            before = blocker.execute(
                """SELECT version,payload_blob,updated_at FROM web_source_cache
                WHERE cache_key=%s FOR UPDATE""",
                (key,),
            ).fetchone()
            if operation == "cache_write":
                pending = pool.submit(
                    s.db.cache_analysis,
                    key,
                    ch["id"],
                    "blocked-post",
                    "v2",
                    [{**prior, "title": "Late replacement"}],
                    ingestion_lease=lease,
                )
            else:
                pending = pool.submit(s.db.touch_analysis, key, ingestion_lease=lease)
            try:
                assert first_check.wait(timeout=3), "Collector must validate its initial live lease"
                assert checks[0] is True
                # The collector passed the first check but cannot acquire the
                # cache row until this transaction releases it.
                s.now[0] += 181
            finally:
                blocker.commit()
        assert pending.result(timeout=5) is False

    assert checks[-1] is False
    with s.db.connect() as db:
        assert (
            db.execute(
                """SELECT version,payload_blob,updated_at FROM web_source_cache
            WHERE cache_key=%s""",
                (key,),
            ).fetchone()
            == before
        )
    assert s.db.cached_analysis(key, "v1") == [prior]
    assert s.db.finish_channel_ingestion(ch, lease, True) is False
    assert not s.db.channel_ingestion_fresh(ch)


def progressive_context(s):
    s.integration.source = CatalogSource()
    client = s.client()
    session, device = session_device(s, client)
    assert client.get("/api/today").status_code == 200
    return client, session, device


def permission_rows(s, user):
    with s.db.connect() as db:
        return db.execute(
            """SELECT channel_id,confirmed_at,valid_until FROM web_channel_access
            WHERE user_id=%s ORDER BY channel_id""",
            (user,),
        ).fetchall()


def test_get_publishes_completed_posts_before_collection_finishes_without_extending_proof(shared):
    s = shared
    client, session, device = progressive_context(s)
    access = permission_rows(s, session["user_id"])
    status = s.db.sync_status(session["user_id"])
    ingest_at = s.db.device(device["id_hash"])["ingest_at"]
    calls = s.graph.calls
    seed_catalog(s)  # One authorized post and one from an inaccessible class.

    first = client.get("/api/today")
    assert first.status_code == 200
    assert [item["title"] for item in first.json()["teams"]] == ["Cached deadline"]
    event = s.db.catalog_events(["class"])[0]
    s.db.cache_analysis(
        digest("class|second"),
        "class",
        "second",
        "v1",
        [{**event, "id": "direct:second", "title": "Second completed post"}],
    )
    assert {item["title"] for item in client.get("/api/today").json()["teams"]} == {
        "Cached deadline",
        "Second completed post",
    }
    assert client.get("/api/calendar?start=2026-10-01&end=2026-10-31").status_code == 200
    assert s.integration.source.calls == 0 and s.graph.calls == calls
    assert permission_rows(s, session["user_id"]) == access
    assert s.db.sync_status(session["user_id"]) == status
    assert s.db.device(device["id_hash"])["ingest_at"] == ingest_at
    with s.db.connect() as db:
        assert db.execute("SELECT id FROM web_events ORDER BY id").fetchall() == [
            ("direct:cached",),
            ("direct:second",),
        ]


def test_progressive_updates_preserve_hidden_completion_override_and_review(shared):
    s = shared
    _, session, device = progressive_context(s)
    seed_catalog(s)
    s.db.materialize_cached_events(device)
    user, tenant = session["user_id"], session["tenant_id"]
    s.db.event_state(user, tenant, "direct:cached", {"hidden": True, "completed": True})
    s.db.override_event(user, tenant, "direct:cached", {"title": "My own title"})
    event = s.db.catalog_events(["class"])[0]
    s.db.cache_analysis(
        digest("class|cached"),
        "class",
        "cached",
        "v2",
        [{**event, "title": "Changed source", "source_version": "v2"}],
    )
    s.db.materialize_cached_events(device)
    s.db.materialize_cached_events(device)
    items = s.db.calendar(user, tenant, "2026-10-22", "2026-10-22", hidden_only=True)
    assert len(items) == 1 and items[0]["title"] == "My own title"
    assert items[0]["hidden"] and items[0]["completed"]
    changes = s.db.changes(user, tenant)
    assert len(changes) == 1 and changes[0]["after"]["title"] == "Changed source"


@pytest.mark.parametrize("expiry", ["permission", "device"])
def test_progressive_publish_rolls_back_when_proof_expires_during_cache_lock(
    shared, monkeypatch, expiry
):
    s = shared
    _, session, device = progressive_context(s)
    seed_catalog(s)
    access = permission_rows(s, session["user_id"])
    original = s.db.connect
    if expiry == "device":
        with original() as db:
            db.execute(
                "UPDATE web_connections SET expires_at=%s WHERE id_hash=%s",
                (s.now[0] + 1, device["id_hash"]),
            )
    waiting = Event()

    @contextmanager
    def observed_connect():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    if query.startswith("SELECT channel_id,payload_blob FROM web_source_cache"):
                        waiting.set()
                    return db.execute(query, values)

            yield Connection()

    monkeypatch.setattr(s.db, "connect", observed_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original() as blocker:
            blocker.execute(
                "SELECT cache_key FROM web_source_cache WHERE cache_key=%s FOR UPDATE",
                (digest("class|cached"),),
            )
            result = pool.submit(s.db.materialize_cached_events, device)
            assert waiting.wait(2), "Materialization must reach the authorized cache lock"
            s.now[0] = min(row[2] for row in access) if expiry == "permission" else s.now[0] + 2
        with pytest.raises(PermissionPending if expiry == "permission" else Missing):
            result.result(timeout=5)
    with original() as db:
        assert db.execute("SELECT id FROM web_events").fetchall() == []
        assert db.execute("SELECT event_id FROM web_user_source_versions").fetchall() == []
    assert permission_rows(s, session["user_id"]) == access


@pytest.mark.parametrize("invalid", ["version", "tenant", "revoked"])
def test_progressive_publish_rejects_stale_or_revoked_device(shared, invalid):
    s = shared
    _, _, device = progressive_context(s)
    seed_catalog(s)
    if invalid == "version":
        device = {**device, "version": device["version"] + 1}
    elif invalid == "tenant":
        device = {**device, "tenant_id": "another-school"}
    else:
        s.db.revoke_device(device["id_hash"])
    with pytest.raises(Missing):
        s.db.materialize_cached_events(device)
    assert s.db.ingestion_pending(device) is False
    with s.db.connect() as db:
        assert db.execute("SELECT id FROM web_events").fetchall() == []


def test_ingestion_pending_tracks_current_queue_and_only_authorized_channel_leases(shared):
    s = shared
    client, session, device = progressive_context(s)
    s.db.mark_ingested(device["id_hash"])
    assert s.db.ingestion_pending(device) is False
    other = channel()
    foreign_lease = s.db.claim_channel_ingestion(other)
    assert foreign_lease and s.db.ingestion_pending(device) is False
    allowed = {"id": "class", "team_id": "verified-team", "name": "Class"}
    lease = s.db.claim_channel_ingestion(allowed)
    assert lease and s.db.ingestion_pending(device) is True
    assert client.get("/api/settings").json()["connection"]["ingestion_pending"] is True
    s.db.finish_channel_ingestion(allowed, lease, True)
    assert s.db.ingestion_pending(device) is False
    assert s.db.request_ingestion(device["id_hash"])
    assert s.db.ingestion_pending(device) is True
    proof = s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    assert s.db.fail_sync(session["user_id"], "verification_failed", proof)
    seed_catalog(s)
    assert s.db.materialize_cached_events(device) == 0
    assert s.db.ingestion_pending(device) is False
    with s.db.connect() as db:
        assert db.execute("SELECT id FROM web_events").fetchall() == []
