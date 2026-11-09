"""Worker lifespan recovery; no providers, external connections or long sleeps."""

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_server import server as server

import web_service.server as server_module


@pytest.mark.parametrize("audit_fails", [False, True])
def test_lifespan_retries_after_worker_failure_even_when_audit_is_unavailable(
    server, monkeypatch, audit_fails
):
    app = server_module.create_test_server(
        server.settings,
        server.db,
        server.identity,
        server.graph,
        server.source,
        server.sender,
        worker=True,
    )
    recovered = Event()
    attempts = []
    audits = []
    pauses = []

    def tick():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("simulated worker database failure")
        recovered.set()

    def audit(event, principal):
        audits.append((event, principal))
        if audit_fails:
            raise RuntimeError("simulated audit database failure")

    async def worker_wait(awaitable, *, timeout):
        if timeout == 60:
            pauses.append(timeout)
            # Shorten only the first worker backoff. Later waits retain the
            # production duration and end through the real lifespan stop event,
            # so the regression never relies on a busy loop or a 60-second sleep.
            if len(pauses) == 1:
                return await asyncio.wait_for(awaitable, timeout=0.01)
        return await asyncio.wait_for(awaitable, timeout=timeout)

    # Replace only this module's asyncio reference. Other test/client asyncio
    # timeouts remain unchanged, and the real Event/task/to_thread APIs are used.
    worker_asyncio = SimpleNamespace(
        Event=asyncio.Event,
        to_thread=asyncio.to_thread,
        create_task=asyncio.create_task,
        wait_for=worker_wait,
    )
    monkeypatch.setattr(server_module, "asyncio", worker_asyncio)
    monkeypatch.setattr(app.state.integration, "tick", tick)
    monkeypatch.setattr(server.db, "audit", audit)
    with TestClient(app, base_url=server.settings.origin):
        assert recovered.wait(timeout=3), "The lifespan worker stopped instead of retrying"
    assert attempts == [1, 2]
    assert audits == [("worker_failed", "worker")]
    assert pauses and all(timeout == 60 for timeout in pauses)
