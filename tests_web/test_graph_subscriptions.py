import json
from datetime import datetime, timezone

import httpx
import pytest

from web_service.graph_subscriptions import GraphSubscriptions, SubscriptionFailure

TEAM = "11111111-1111-4111-8111-111111111111"
SUB = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def api(handler):
    transport = httpx.MockTransport(handler)
    return GraphSubscriptions(
        "https://calendar.example.org", lambda: httpx.Client(transport=transport), clock=lambda: NOW
    )


def test_create_and_renew_only_use_fixed_graph_host():
    requests = []

    def handle(req):
        requests.append(req)
        return httpx.Response(
            201 if req.method == "POST" else 200,
            json={"id": SUB, "expirationDateTime": "2026-11-05T00:00:00Z"},
        )

    client = api(handle)
    response = client.create("token", TEAM, "19:example@thread.tacv2", "x" * 40)
    assert response["id"] == SUB
    body = json.loads(requests[0].content)
    assert requests[0].url.host == "graph.microsoft.com"
    assert body["resource"].startswith("/teams/" + TEAM + "/channels/")
    assert body["notificationUrl"] == "https://calendar.example.org/hooks/graph"
    assert body["lifecycleNotificationUrl"] == "https://calendar.example.org/hooks/graph/lifecycle"
    assert body["includeResourceData"] is False
    assert client.renew("token", SUB)["id"] == SUB
    assert requests[1].method == "PATCH"


def test_throttle_and_invalid_responses_do_not_leak_secrets():
    client = api(
        lambda req: httpx.Response(429, headers={"Retry-After": "42"}, text="secret in response")
    )
    with pytest.raises(SubscriptionFailure) as exc:
        client.create("secret-token", TEAM, "channel", "x" * 40)
    assert exc.value.reason == "throttled" and exc.value.retry_after == 42
    assert "secret" not in str(exc.value)
    client = api(lambda req: httpx.Response(201, json={"id": "invalid", "expirationDateTime": "x"}))
    with pytest.raises(SubscriptionFailure) as exc:
        client.create("token", TEAM, "channel", "x" * 40)
    assert exc.value.reason == "invalid_response"


def test_delete_missing_is_idempotent_and_origins_are_restricted():
    api(lambda req: httpx.Response(404)).delete("token", SUB)
    for origin in (
        "http://calendar.example.org",
        "https://u:p@example.org",
        "https://example.org/path",
    ):
        with pytest.raises(ValueError):
            GraphSubscriptions(origin)
