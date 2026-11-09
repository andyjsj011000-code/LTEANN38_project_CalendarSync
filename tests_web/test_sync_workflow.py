"""Exercise synchronization with a plain context rather than a web adapter."""

from types import SimpleNamespace

import pytest
from test_ingestion_status import CatalogSource
from test_ingestion_status_orchestration import tracked_server as tracked_server

from web_service.sync_workflow import run_sync


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_plain_context_commits_and_tracks_collection(tracked_server, shared, partial):
    s = tracked_server
    s.db.shared_ingestion_enabled = shared
    context = SimpleNamespace(
        settings=s.integration.settings,
        db=s.db,
        graph=s.graph,
        source=CatalogSource(partial=partial),
        tokens=s.integration.tokens,
        shared_ingestion_enabled=shared,
        _collection_proofs={},
        collection_proof=lambda device: None,
    )
    context.run_sync = lambda device, **options: run_sync(context, device, **options)

    original_due = s.db.device(s.device["id_hash"])["ingest_at"]
    result = run_sync(context, s.device, ingest=True)

    assert result == {
        "channels_total": 1,
        "channels_scanned": 1,
        "messages_seen": 2,
        "messages_analyzed": 0 if partial else 1,
        "messages_cached": 1,
        "messages_failed": 1 if partial else 0,
        "partial": partial,
        "state": "partial" if partial else "completed",
    }
    assert s.db.ingestion_status(s.device)["state"] == result["state"]
    assert [event["title"] for event in s.db.catalog_events(["class"])] == ["Cached deadline"]
    assert s.db.device(s.device["id_hash"])["ingest_at"] == (
        original_due if partial else s.now[0] + 300
    )
    if shared:
        proof = context._collection_proofs[s.device["id_hash"]]
        assert proof["valid_until"] == s.now[0] + context.settings.access_seconds
        assert set(proof) == {"device", "channels", "valid_until"}
