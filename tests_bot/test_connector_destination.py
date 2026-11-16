import anyio
import httpx
import pytest

from app.services.bot_service import BotService

CONNECTOR_HOST = "connector.example.invalid"
CONNECTOR_URL = f"https://{CONNECTOR_HOST}/teams"
LOGIN_HOST = "login.microsoftonline.com"


async def send(client, operation, service_url):
    async with client:
        service = BotService(
            "test-app", "test-secret", "test-tenant", client, allowed_hosts={CONNECTOR_HOST}
        )
        if operation == "reply":
            return await service.send_teams_reply(service_url, "test-conversation", "test message")
        return await service.create_or_get_conversation(service_url, "test-user", "test-tenant")


@pytest.mark.parametrize(("operation", "expected"), [("reply", False), ("conversation", None)])
@pytest.mark.parametrize(
    "service_url",
    [
        "https://untrusted.example.invalid/teams",
        f"http://{CONNECTOR_HOST}/teams",
        f"https://user@{CONNECTOR_HOST}/teams",
        f"https://{CONNECTOR_HOST}.untrusted.example.invalid/teams",
    ],
)
def test_rejects_untrusted_destination_before_requesting_token(operation, expected, service_url):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(401)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    assert anyio.run(send, client, operation, service_url) is expected
    assert requests == []


@pytest.mark.parametrize(("operation", "expected"), [("reply", False), ("conversation", None)])
def test_never_follows_connector_redirect(operation, expected):
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.host == LOGIN_HOST:
            return httpx.Response(200, json={"access_token": "test-token"})
        return httpx.Response(307, headers={"Location": "https://untrusted.example.invalid"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True)
    assert anyio.run(send, client, operation, CONNECTOR_URL) is expected
    assert [request.url.host for request in requests] == [LOGIN_HOST, CONNECTOR_HOST]
    assert requests[-1].headers["Authorization"] == "Bearer test-token"


def test_creates_conversation_at_trusted_host():
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.host == LOGIN_HOST:
            return httpx.Response(200, json={"access_token": "test-token"})
        return httpx.Response(201, json={"id": "test-conversation"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    assert anyio.run(send, client, "conversation", CONNECTOR_URL) == "test-conversation"
    assert str(requests[-1].url) == f"{CONNECTOR_URL}/v3/conversations"
    assert requests[-1].headers["Authorization"] == "Bearer test-token"


def test_never_follows_token_endpoint_redirect():
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.host == LOGIN_HOST:
            return httpx.Response(307, headers={"Location": "https://untrusted.example.invalid"})
        return httpx.Response(200, json={"access_token": "test-token"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True)
    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(send, client, "reply", CONNECTOR_URL)
    assert [request.url.host for request in requests] == [LOGIN_HOST]
