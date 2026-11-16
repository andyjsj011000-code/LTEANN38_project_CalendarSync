import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

from app.core.bot_auth import ISSUER, JWKS_URL, KEY_TTL_SECONDS, OPENID_URL, BotConnectorAuth

APP_ID = "test-app-id"
SERVICE_URL = "https://connector.example.invalid/teams/"
ACTIVITY = {"channelId": "msteams", "serviceUrl": SERVICE_URL}
KEY_ID = "test-signing-key"
METADATA = {
    "issuer": ISSUER,
    "jwks_uri": JWKS_URL,
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_keys(signing_key):
    public_key = RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    return [
        {
            **public_key,
            "kid": KEY_ID,
            "use": "sig",
            "alg": "RS256",
            "endorsements": ["msteams"],
        }
    ]


def bearer(signing_key, *, kid=KEY_ID, **claim_changes):
    now = int(time.time())
    claims = {
        "aud": APP_ID,
        "iss": ISSUER,
        "exp": now + 900,
        "nbf": now - 60,
        "serviceurl": SERVICE_URL,
        **claim_changes,
    }
    token = jwt.encode(
        {name: value for name, value in claims.items() if value is not None},
        signing_key,
        algorithm="RS256",
        headers={"kid": kid},
    )
    return f"Bearer {token}"


def authenticate(attempts, signing_keys, *, metadata=METADATA, metadata_status=200, elapsed=None):
    """Run the authenticator with local signing metadata and capture outcomes and HTTP requests."""
    calls = []

    def transport(request):
        url = str(request.url)
        assert request.method == "GET"
        calls.append(url)
        if url == OPENID_URL:
            return httpx.Response(metadata_status, json=metadata)
        assert url == JWKS_URL
        return httpx.Response(200, json={"keys": signing_keys})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            now = 0
            auth = BotConnectorAuth(
                client, APP_ID, {"connector.example.invalid"}, clock=lambda: now
            )
            outcomes = []
            for index, (authorization, activity) in enumerate(attempts):
                if elapsed is not None:
                    now = elapsed[index]
                try:
                    await auth.authenticate(authorization, activity)
                except HTTPException as error:
                    outcomes.append(error.status_code)
                else:
                    outcomes.append(None)
            return outcomes

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(exercise()), calls


def test_signed_teams_activity_uses_cached_signing_keys(signing_key, signing_keys):
    signed_activity = (bearer(signing_key), ACTIVITY)

    outcomes, calls = authenticate([signed_activity, signed_activity], signing_keys)

    assert outcomes == [None, None]
    assert calls == [OPENID_URL, JWKS_URL]


def test_expired_key_cache_is_refreshed(signing_key, signing_keys):
    signed_activity = (bearer(signing_key), ACTIVITY)

    outcomes, calls = authenticate(
        [signed_activity, signed_activity],
        signing_keys,
        elapsed=[0, KEY_TTL_SECONDS + 1],
    )

    assert outcomes == [None, None]
    assert calls == [OPENID_URL, JWKS_URL] * 2


def test_unknown_key_ids_do_not_force_repeated_metadata_lookups(signing_key, signing_keys):
    attempts = [
        (bearer(signing_key, kid="unknown-key-1"), ACTIVITY),
        (bearer(signing_key, kid="unknown-key-2"), ACTIVITY),
        (bearer(signing_key), ACTIVITY),
    ]

    outcomes, calls = authenticate(attempts, signing_keys)

    assert outcomes == [403, 403, None]
    assert calls == [OPENID_URL, JWKS_URL]


@pytest.mark.parametrize("authorization", [None, "Basic credentials", "Bearer not-a-token"])
def test_missing_or_malformed_authorization_is_denied_without_key_lookup(
    authorization, signing_keys
):
    outcomes, calls = authenticate([(authorization, ACTIVITY)], signing_keys)

    assert outcomes == [403]
    assert calls == []


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"aud": "different-app-id"}, id="wrong-audience"),
        pytest.param({"iss": "https://issuer.example.invalid"}, id="wrong-issuer"),
        pytest.param({"exp": 1}, id="expired"),
        pytest.param({"nbf": 4_000_000_000}, id="not-yet-valid"),
        pytest.param({"exp": None}, id="missing-expiry"),
        pytest.param({"serviceurl": None}, id="missing-service-url"),
        pytest.param({"serviceurl": f"{SERVICE_URL}different-path/"}, id="different-service-url"),
    ],
)
def test_invalid_token_claims_are_denied(signing_key, signing_keys, changes):
    outcomes, _ = authenticate([(bearer(signing_key, **changes), ACTIVITY)], signing_keys)

    assert outcomes == [403]


def test_signing_key_must_verify_the_signature(signing_keys):
    different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    outcomes, _ = authenticate([(bearer(different_key), ACTIVITY)], signing_keys)

    assert outcomes == [403]


def test_signing_key_must_endorse_the_teams_channel(signing_key, signing_keys):
    signing_keys[0]["endorsements"] = ["other-channel"]

    outcomes, _ = authenticate([(bearer(signing_key), ACTIVITY)], signing_keys)

    assert outcomes == [403]


def test_activity_must_come_from_teams(signing_key, signing_keys):
    activity = {**ACTIVITY, "channelId": "other-channel"}

    outcomes, _ = authenticate([(bearer(signing_key), activity)], signing_keys)

    assert outcomes == [403]


@pytest.mark.parametrize(
    "destination",
    [
        "http://connector.example.invalid/teams/",
        "https://another.example.invalid/teams/",
    ],
)
def test_signed_service_url_still_requires_an_allowed_https_destination(
    signing_key, signing_keys, destination
):
    activity = {**ACTIVITY, "serviceUrl": destination}

    outcomes, _ = authenticate(
        [(bearer(signing_key, serviceurl=destination), activity)], signing_keys
    )

    assert outcomes == [403]


def test_metadata_unavailable_fails_closed(signing_key, signing_keys):
    outcomes, calls = authenticate(
        [(bearer(signing_key), ACTIVITY)], signing_keys, metadata_status=503
    )

    assert outcomes == [503]
    assert calls == [OPENID_URL]


def test_metadata_cannot_redirect_key_lookup_to_another_host(signing_key, signing_keys):
    outcomes, calls = authenticate(
        [(bearer(signing_key), ACTIVITY)],
        signing_keys,
        metadata={**METADATA, "jwks_uri": "https://untrusted.example.invalid/keys"},
    )

    assert outcomes == [503]
    assert calls == [OPENID_URL]
