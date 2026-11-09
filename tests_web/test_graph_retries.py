"""Bounded throttling recovery never turns failed reads into permission proof."""

import logging
from threading import Event

import httpx
import pytest

from web_service import school_graph
from web_service.direct_source import TeamsGraph
from web_service.school_graph import BASE, DelegatedGraph, GraphFailure

USER = "33333333-3333-4333-8333-333333333333"
TEAM = "22222222-2222-4222-8222-222222222222"
TENANT = "11111111-1111-4111-8111-111111111111"
CHANNEL = {"team_id": TEAM, "id": "PRIVATE_CHANNEL_ID"}


def retry_graph(monkeypatch, responses, graph_type=DelegatedGraph):
    clock = [0.0]
    calls = []
    waits = []
    monkeypatch.setattr(school_graph, "monotonic", lambda: clock[0])

    def handler(request):
        calls.append(request)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    graph = graph_type(lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    def wait(seconds, stopped=None):
        waits.append(seconds)
        clock[0] += seconds
        return False

    graph._retry_wait = wait
    return graph, clock, calls, waits


def test_source_messages_recovers_after_permission_probe_channel_throttle(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    graph, clock, calls, waits = retry_graph(
        monkeypatch,
        [
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "PRIVATE_ERROR_BODY"}),
            httpx.Response(200, json={"value": [{"id": "post"}]}),
        ],
        TeamsGraph,
    )
    assert graph.messages("PRIVATE_BEARER_TOKEN", CHANNEL) == [{"id": "post"}]
    assert len(calls) == 2 and waits == [1] and clock[0] == 1
    assert "stage=source_messages outcome=throttled http_status=429 attempt=1" in caplog.text
    assert "stage=source_messages outcome=recovered http_status=200 attempt=2" in caplog.text
    for private in [
        "PRIVATE_CHANNEL_ID",
        "PRIVATE_BEARER_TOKEN",
        "PRIVATE_ERROR_BODY",
        "graph.microsoft.com",
    ]:
        assert private not in caplog.text


@pytest.mark.parametrize(
    "retry_after,expected_waits", [(None, [1, 2]), ("invalid", [1, 2]), ("2", [2, 2])]
)
def test_repeated_throttle_exhausts_three_attempts_without_empty_success(
    monkeypatch, retry_after, expected_waits
):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    graph, _, calls, waits = retry_graph(monkeypatch, [httpx.Response(429, headers=headers)])
    with graph.client_factory() as client, pytest.raises(GraphFailure) as error:
        graph._read(client, "token", BASE + "/me/joinedTeams", deadline=80)
    assert error.value.state == "verification_failed"
    assert len(calls) == 3 and waits == expected_waits


@pytest.mark.parametrize(
    "retry_after,deadline", [("12", 80), ("2", 1.5), ("999999999999999999999", 80)]
)
def test_delay_that_cannot_fit_is_refused_without_early_retry(monkeypatch, retry_after, deadline):
    graph, _, calls, waits = retry_graph(
        monkeypatch, [httpx.Response(429, headers={"Retry-After": retry_after})]
    )
    with graph.client_factory() as client, pytest.raises(GraphFailure):
        graph._read(client, "token", BASE + "/me", deadline=deadline)
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize(
    "status,state,result",
    [
        (401, "reauth_required", None),
        (403, None, None),
        (404, None, None),
        (500, "verification_failed", None),
    ],
)
def test_auth_denial_missing_and_other_errors_are_never_retried(monkeypatch, status, state, result):
    graph, _, calls, waits = retry_graph(monkeypatch, [httpx.Response(status)])
    with graph.client_factory() as client:
        if state:
            with pytest.raises(GraphFailure) as error:
                graph._read(client, "token", BASE + "/me")
            assert error.value.state == state
        else:
            assert graph._read(client, "token", BASE + "/me") is result
    assert len(calls) == 1 and waits == []


def test_permission_retry_reenters_pacer_with_bounded_deadline(monkeypatch):
    graph, _, calls, waits = retry_graph(
        monkeypatch, [httpx.Response(429), httpx.Response(200, json={"value": []})]
    )
    gates = []
    with graph.client_factory() as client:
        assert graph._read(
            client,
            "token",
            BASE + "/teams/" + TEAM + "/channels/channel/messages?$top=1",
            deadline=80,
            retry_gate=lambda deadline: gates.append(deadline) or True,
        ) == {"value": []}
    assert len(calls) == 2 and waits == [1] and gates == [10]


def test_member_and_pagination_do_not_retry_past_collection_deadline(monkeypatch):
    graph, clock, calls, waits = retry_graph(
        monkeypatch, [httpx.Response(429, headers={"Retry-After": "2"})]
    )
    with graph.client_factory() as client, pytest.raises(GraphFailure):
        graph._member(client, "token", USER, deadline=1)
    with graph.client_factory() as client, pytest.raises(GraphFailure):
        graph._pages(client, "token", "/me/joinedTeams", 1)
    assert len(calls) == 2 and waits == [] and clock[0] == 0


def test_diagnostic_logger_failure_does_not_change_graph_result(monkeypatch):
    def broken_logger(*args, **kwargs):
        raise RuntimeError("logger failed")

    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "info", broken_logger)
    graph, _, calls, waits = retry_graph(
        monkeypatch, [httpx.Response(429), httpx.Response(200, json={"value": []})]
    )
    with graph.client_factory() as client:
        assert graph._read(client, "token", BASE + "/me/joinedTeams") == {"value": []}
    assert len(calls) == 2 and waits == [1]


def test_sibling_auth_failure_cancels_throttled_permission_retry():
    waiting = Event()
    calls = []

    def handler(request):
        path = request.url.path
        if path == "/v1.0/me":
            return httpx.Response(200, json={"id": USER, "userType": "Member"})
        if path == "/v1.0/me/joinedTeams":
            return httpx.Response(200, json={"value": [{"id": TEAM}]})
        if path.endswith("/allChannels"):
            return httpx.Response(200, json={"value": [{"id": "throttled"}, {"id": "expired"}]})
        calls.append(path)
        if "/throttled/" in path:
            return httpx.Response(429, headers={"Retry-After": "1"})
        assert waiting.wait(timeout=3)
        return httpx.Response(401)

    graph = DelegatedGraph(lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    graph._wait_for_probe = lambda identity, deadline, stopped: not stopped.is_set()

    def wait(seconds, stopped=None):
        waiting.set()
        assert stopped is not None
        return stopped.wait(timeout=3)

    graph._retry_wait = wait
    with pytest.raises(GraphFailure) as error:
        graph.channels("token", USER, TENANT)
    assert error.value.state == "reauth_required"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status,kind,outcome,state",
    [
        (401, "image/png", "http_failure", "reauth_required"),
        (404, "image/png", "http_failure", "verification_failed"),
        (429, "image/png", "http_failure", "verification_failed"),
        (200, "PRIVATE_UNSUPPORTED_TYPE", "invalid_media", "verification_failed"),
    ],
)
def test_hosted_image_failure_logs_numeric_cause_without_private_data(
    caplog, status, kind, outcome, state
):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    graph = TeamsGraph(
        lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status, headers={"Content-Type": kind}, content=b"PRIVATE_RESPONSE_BODY"
                )
            )
        )
    )
    url = (
        BASE
        + graph.message_path(CHANNEL, "PRIVATE_MESSAGE_ID")
        + "/hostedContents/PRIVATE_IMAGE_ID/$value"
    )
    with pytest.raises(GraphFailure) as error:
        graph.hosted_image("PRIVATE_BEARER_TOKEN", CHANNEL, "PRIVATE_MESSAGE_ID", url)
    assert error.value.state == state
    assert f"stage=hosted_image outcome={outcome} http_status={status}" in caplog.text
    for private in [
        "PRIVATE_CHANNEL_ID",
        "PRIVATE_MESSAGE_ID",
        "PRIVATE_IMAGE_ID",
        "PRIVATE_BEARER_TOKEN",
        "PRIVATE_RESPONSE_BODY",
        "PRIVATE_UNSUPPORTED_TYPE",
        "graph.microsoft.com",
    ]:
        assert private not in caplog.text
