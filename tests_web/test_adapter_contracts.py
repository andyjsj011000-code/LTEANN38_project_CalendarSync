"""The same observable contracts for doubles and concrete, isolated adapters."""

import base64
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from support.fakes import Cache, Extractor, Graph, Refresher, SharedCache, channels, event, message
from support.postgres import postgres_database as postgres_database

from web_service.connections import ConnectionTokens, acquire_snapshot
from web_service.direct_source import AIExtractor, DirectSource, Extraction, TeamsGraph
from web_service.school_graph import BASE, GraphFailure
from web_service.storage import digest
from web_service.test_storage import TestDatabase


def database_context(backend, request, tmp_path):
    if backend == "postgres":
        context = request.getfixturevalue("postgres_database")
        db, now, users, devices = context.repo, context.now, context.users, context.devices
        insert = "INSERT INTO web_channels VALUES(%s,%s,%s)"
    else:
        now = [1000]
        db = TestDatabase(
            tmp_path / "contracts.sqlite3", Fernet.generate_key().decode(), clock=lambda: now[0]
        )
        db.initialize()
        users, devices = {}, {}
        for name in ("alice", "bob"):
            user = db.save_user("tenant", name, {"name": name})
            session = db.issue_session(user, 3600)
            raw, _ = db.add_connection(
                session, user, "test-token-" + name, 3600, "test-cache-" + name, background=True
            )
            users[name], devices[name] = user, db.device(digest(raw))
        insert = "INSERT INTO web_channels VALUES(?,?,?)"
    candidates = channels(2)
    with db.connect() as connection:
        for channel in candidates:
            connection.execute(insert, (channel["id"], "tenant", db.seal({"name": "Class"})))

    def advance(seconds):
        now[0] += seconds

    return SimpleNamespace(
        db=db, users=users, devices=devices, channels=candidates, advance=advance
    )


@pytest.fixture(params=["fake", "sqlite", "postgres"])
def cache_context(request, tmp_path):
    if request.param == "fake":
        return SimpleNamespace(
            db=Cache(), users={"alice": "alice", "bob": "bob"}, channels=channels(2)
        )
    return database_context(request.param, request, tmp_path)


def source(channel, title="Deadline"):
    return {**event(title), "id": "direct:" + channel["id"], "channel_id": channel["id"]}


def test_cache_contract_replaces_one_revision_without_duplicate_events(cache_context):
    c = cache_context
    channel = c.channels[0]
    key = digest(channel["id"] + "|post")
    first, second = source(channel), source(channel, "New deadline")
    c.db.cache_analysis(key, channel["id"], "post", "v1", [first])
    c.db.cache_analysis(key, channel["id"], "post", "v2", [second])
    assert c.db.cached_messages(channel["id"]) == {"post": {"version": "v2", "events": [second]}}
    assert c.db.catalog_events([channel["id"]]) == [second]
    c.db.touch_analysis(key)
    assert c.db.catalog_events([channel["id"]]) == [second]


def test_cache_contract_cursor_is_per_user_and_visits_one_channel(cache_context):
    c = cache_context
    reverse = list(reversed(c.channels))
    assert c.db.next_channels(c.users["alice"], reverse, 1) == [c.channels[0]]
    assert c.db.next_channels(c.users["alice"], reverse, 1) == [c.channels[1]]
    assert c.db.next_channels(c.users["bob"], reverse, 1) == [c.channels[0]]
    assert c.db.next_channels(c.users["alice"], reverse, 1) == [c.channels[0]]


def test_cache_contract_catalog_keeps_only_requested_channels(cache_context):
    c = cache_context
    for channel in c.channels:
        c.db.cache_analysis(
            digest(channel["id"] + "|post"), channel["id"], "post", "v1", [source(channel)]
        )
    assert c.db.catalog_events([c.channels[1]["id"]]) == [source(c.channels[1])]
    assert c.db.catalog_events([]) == []


def test_cache_contract_cancellation_keeps_the_event_identity(cache_context):
    c = cache_context
    channel = c.channels[0]
    item = source(channel)
    key = digest(channel["id"] + "|post")
    c.db.cache_analysis(key, channel["id"], "post", "v1", [item])
    cancelled = {**item, "cancelled": True}
    c.db.cache_analysis(key, channel["id"], "post", "v2", [cancelled])
    assert c.db.catalog_events([channel["id"]]) == [cancelled]


@pytest.fixture(params=["fake", "postgres"])
def shared_context(request, tmp_path):
    if request.param == "fake":
        db = SharedCache()

        def advance(seconds):
            db.now += seconds

        return SimpleNamespace(db=db, channels=channels(2), advance=advance)
    c = database_context("postgres", request, tmp_path)
    # This fixture's explicitly disposable schema owns the additional table.
    with c.db.connect() as connection:
        connection.execute("""CREATE TABLE web_channel_ingestion (
            channel_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, team_id TEXT NOT NULL,
            checked_at BIGINT NOT NULL DEFAULT 0, lease_token TEXT,
            lease_until BIGINT NOT NULL DEFAULT 0)""")
    c.db.shared_ingestion_enabled = True
    return c


def test_shared_contract_replaced_owner_cannot_write_or_finish(shared_context):
    c = shared_context
    channel = c.channels[0]
    first = c.db.claim_channel_ingestion(channel)
    assert first and c.db.claim_channel_ingestion(channel) is None
    assert c.db.finish_channel_ingestion(channel, first, False)
    second = c.db.claim_channel_ingestion(channel)
    assert second and second != first
    key = digest(channel["id"] + "|post")
    assert (
        c.db.cache_analysis(
            key, channel["id"], "post", "stale", [source(channel)], ingestion_lease=first
        )
        is False
    )
    assert c.db.finish_channel_ingestion(channel, first, True) is False
    assert c.db.cache_analysis(
        key, channel["id"], "post", "current", [source(channel)], ingestion_lease=second
    )
    assert c.db.finish_channel_ingestion(channel, second, True)
    assert c.db.cached_messages(channel["id"])["post"]["version"] == "current"


def test_shared_contract_expiry_requires_a_new_owner(shared_context):
    c = shared_context
    channel = c.channels[0]
    first = c.db.claim_channel_ingestion(channel)
    c.advance(181)
    key = digest(channel["id"] + "|post")
    assert (
        c.db.cache_analysis(
            key, channel["id"], "post", "stale", [source(channel)], ingestion_lease=first
        )
        is False
    )
    assert c.db.finish_channel_ingestion(channel, first, True) is False
    second = c.db.claim_channel_ingestion(channel)
    assert second and second != first
    assert c.db.finish_channel_ingestion(channel, second, True)


def test_shared_contract_only_successful_completion_makes_a_channel_fresh(shared_context):
    c = shared_context
    channel = c.channels[0]
    lease = c.db.claim_channel_ingestion(channel)
    assert c.db.finish_channel_ingestion(channel, lease, False)
    assert not c.db.channel_ingestion_fresh(channel)
    lease = c.db.claim_channel_ingestion(channel)
    assert c.db.finish_channel_ingestion(channel, lease, True)
    assert c.db.channel_ingestion_fresh(channel)
    assert c.db.claim_channel_ingestion(channel) is None
    c.advance(301)
    assert not c.db.channel_ingestion_fresh(channel)
    assert c.db.claim_channel_ingestion(channel)


@pytest.fixture(params=["fake", "graph"])
def graph_context(request):
    channel = channels(1)[0]
    root = message("post", "Deadline")
    image = (
        BASE
        + "/teams/team/channels/"
        + channel["id"]
        + "/messages/post/hostedContents/image/$value"
    )
    image_bytes = b"test-only-image"
    data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
    state = {"denied": False}
    if request.param == "fake":
        api = Graph({channel["id"]: [root]})
        api.images[image] = data_uri

        def deny():
            api.failure = GraphFailure("reauth_required")
    else:
        path = "/v1.0/teams/team/channels/" + channel["id"] + "/messages"

        def handler(req):
            assert req.headers["authorization"] == "Bearer synthetic-token"
            if state["denied"]:
                return httpx.Response(401)
            if req.url.path == path:
                return httpx.Response(200, json={"value": [root]})
            if req.url.path == path + "/post":
                return httpx.Response(200, json=root)
            if req.url.path == path + "/post/replies":
                return httpx.Response(200, json={"value": []})
            if str(req.url) == image:
                return httpx.Response(
                    200, content=image_bytes, headers={"content-type": "image/png"}
                )
            return httpx.Response(404)

        api = TeamsGraph(
            lambda: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        )

        def deny():
            state["denied"] = True

    return SimpleNamespace(api=api, channel=channel, image=image, data_uri=data_uri, deny=deny)


def test_graph_contract_reads_a_post_and_distinguishes_a_missing_root(graph_context):
    c = graph_context
    assert [m["id"] for m in c.api.messages("synthetic-token", c.channel)] == ["post"]
    assert (
        c.api.message("synthetic-token", c.channel, "post", complete_replies=True)["body"][
            "content"
        ]
        == "Deadline"
    )
    assert c.api.message("synthetic-token", c.channel, "missing") is None


def test_graph_contract_revocation_is_not_an_empty_channel(graph_context):
    c = graph_context
    c.deny()
    with pytest.raises(GraphFailure) as error:
        c.api.messages("synthetic-token", c.channel)
    assert error.value.state == "reauth_required"


def test_graph_contract_hosted_image_is_a_data_uri(graph_context):
    c = graph_context
    assert c.api.hosted_image("synthetic-token", c.channel, "post", c.image) == c.data_uri


def test_graph_contract_runs_the_same_incremental_collector(graph_context):
    c = graph_context
    cache, extractor = Cache(), Extractor()
    collector = DirectSource(c.api, extractor, images=False)
    first = collector.collect("synthetic-token", [c.channel], "tenant", cache, "alice")
    second = collector.collect("synthetic-token", [c.channel], "tenant", cache, "alice")
    assert first == second and first[0]["id"].startswith("direct:")
    assert len(extractor.calls) == 1 and second.report["messages_cached"] == 1


@pytest.fixture(params=["fake", "openai"])
def extractor_context(request):
    state = {"mode": "normal"}
    if request.param == "fake":
        extractor = Extractor(drafts=[event("Deadline")])

        def mode(value):
            extractor.fail = ValueError("synthetic refusal") if value == "refusal" else None
            extractor.drafts = [] if value == "empty" else [event("Deadline")]
    else:

        def handler(req):
            assert req.headers["authorization"] == "Bearer synthetic-key"
            if state["mode"] == "refusal":
                message_value = {"refusal": "synthetic refusal"}
            else:
                values = [] if state["mode"] == "empty" else [event("Deadline")]
                message_value = {"content": Extraction(events=values).model_dump_json()}
            return httpx.Response(200, json={"choices": [{"message": message_value}]})

        extractor = AIExtractor(
            "synthetic-key",
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        )

        def mode(value):
            state["mode"] = value

    return SimpleNamespace(extractor=extractor, mode=mode)


def test_extractor_contract_returns_validated_json_event_fields(extractor_context):
    result = extractor_context.extractor.extract("Deadline", [], [])
    assert Extraction.model_validate({"events": result}).model_dump(mode="json")["events"] == [
        event("Deadline")
    ]


def test_extractor_contract_no_events_is_a_successful_empty_result(extractor_context):
    extractor_context.mode("empty")
    assert extractor_context.extractor.extract("No dates", [], []) == []


def test_extractor_contract_refusal_is_not_an_empty_success(extractor_context):
    extractor_context.mode("refusal")
    with pytest.raises(ValueError):
        extractor_context.extractor.extract("bad-message", [], [])


@pytest.fixture(params=["sqlite", "postgres"])
def connection_context(request, tmp_path):
    return database_context(request.param, request, tmp_path)


def test_connection_contract_fresh_grant_does_not_refresh(connection_context):
    c = connection_context
    refresher = Refresher()
    tokens = ConnectionTokens(c.db, refresher)
    assert tokens.acquire(c.devices["alice"]) == "test-token-alice"
    assert refresher.calls == []


def test_connection_contract_refresh_returns_the_current_generation(connection_context):
    c = connection_context
    c.advance(3541)
    token, snapshot = acquire_snapshot(
        ConnectionTokens(c.db, Refresher()), c.devices["alice"], c.db
    )
    assert token == "synthetic-fresh-token"
    assert snapshot["version"] == c.devices["alice"]["version"] + 1
    assert snapshot["user_id"] == c.users["alice"]


def test_connection_contract_transient_refresh_keeps_credentials_and_retries(connection_context):
    c = connection_context
    device = c.devices["alice"]
    before = c.db.unseal(device["credential_blob"])
    refresher = Refresher(GraphFailure("refresh_unavailable"))
    tokens = ConnectionTokens(c.db, refresher)
    c.advance(3541)
    with pytest.raises(GraphFailure) as error:
        tokens.acquire(device)
    assert error.value.state == "refresh_unavailable"
    assert c.db.unseal(c.db.device(device["id_hash"])["credential_blob"]) == before
    with pytest.raises(GraphFailure) as error:
        tokens.acquire(device)
    assert error.value.state == "refresh_busy"
    c.advance(31)
    refresher.failure = None
    assert tokens.acquire(device) == "synthetic-fresh-token"


def test_connection_contract_denial_requires_reauthentication(connection_context):
    c = connection_context
    device = c.devices["alice"]
    c.advance(3541)
    with pytest.raises(GraphFailure) as error:
        ConnectionTokens(c.db, Refresher(GraphFailure("reauth_required"))).acquire(device)
    assert error.value.state == "reauth_required"
    current = c.db.device(device["id_hash"])
    assert current["state"] == "reauth_required" and c.db.unseal(current["credential_blob"]) == {}
