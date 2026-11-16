import asyncio
from collections.abc import Collection
from time import monotonic
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException

ISSUER = "https://api.botframework.com"
OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
KEY_TTL_SECONDS = 3600
REFRESH_INTERVAL_SECONDS = 60
CLOCK_SKEW_SECONDS = 300


def is_trusted_connector_url(service_url: object, allowed_hosts: Collection[str]) -> bool:
    """HTTPS로 명시적으로 허용된 Teams 호스트에만 토큰을 전송한다."""
    if (
        not isinstance(service_url, str)
        or not service_url.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 or char in "\\?#" for char in service_url)
    ):
        return False
    try:
        url = urlsplit(service_url)
    except ValueError:
        return False
    host = url.hostname
    return (
        url.scheme == "https"
        and host is not None
        and any(host == allowed.casefold() for allowed in allowed_hosts)
        and url.netloc.lower() in {host, f"{host}:443"}
    )


class BotConnectorAuth:
    def __init__(self, client: httpx.AsyncClient, app_id: str, allowed_hosts, *, clock=monotonic):
        if not app_id or not allowed_hosts:
            raise ValueError("Bot Connector app ID and allowed hosts are required")
        self._client = client
        self._app_id = app_id
        self._allowed_hosts = frozenset(allowed_hosts)
        self._clock = clock
        self._keys: dict[str, dict] = {}
        self._valid_until = float("-inf")
        self._last_refresh = float("-inf")
        self._lock = asyncio.Lock()

    async def _get_json(self, url: str) -> dict:
        response = await self._client.get(url, follow_redirects=False, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected a Connector metadata object")
        return data

    async def _refresh_keys(self) -> None:
        metadata = await self._get_json(OPENID_URL)
        if (
            metadata.get("issuer") != ISSUER
            or metadata.get("jwks_uri") != JWKS_URL
            or not isinstance(metadata.get("id_token_signing_alg_values_supported"), list)
            or "RS256" not in metadata["id_token_signing_alg_values_supported"]
        ):
            raise ValueError("Unexpected Connector metadata")
        jwks = (await self._get_json(JWKS_URL)).get("keys")
        if not isinstance(jwks, list):
            raise ValueError("Expected a Connector signing key list")
        keys = {}
        for key in jwks:
            if (
                isinstance(key, dict)
                and isinstance(key.get("kid"), str)
                and key.get("kty") == "RSA"
                and key.get("alg", "RS256") == "RS256"
                and key.get("use", "sig") == "sig"
            ):
                if key["kid"] in keys:
                    raise ValueError("Duplicate Connector signing key ID")
                keys[key["kid"]] = key
        if not keys:
            raise ValueError("No Connector signing keys")
        self._keys = keys
        self._valid_until = self._clock() + KEY_TTL_SECONDS

    async def _signing_key(self, kid: str) -> dict | None:
        async with self._lock:
            now = self._clock()
            if now < self._valid_until and kid in self._keys:
                return self._keys[kid]
            # 새 키는 다시 조회하되 임의의 키 ID로 새로고침을 반복하지 못하게 한다.
            if now - self._last_refresh >= REFRESH_INTERVAL_SECONDS:
                self._last_refresh = now
                try:
                    await self._refresh_keys()
                except (httpx.HTTPError, ValueError):
                    if self._clock() >= self._valid_until:
                        raise HTTPException(503, "Bot Connector signing keys unavailable") from None
            if self._clock() >= self._valid_until:
                raise HTTPException(503, "Bot Connector signing keys unavailable")
            return self._keys.get(kid)

    async def authenticate(self, authorization: str | None, activity: dict) -> None:
        """Bot Connector의 서명과 Teams 발송 주소가 확인된 요청만 허용한다."""
        scheme, _, token = (authorization or "").partition(" ")
        service_url = activity.get("serviceUrl")
        if (
            scheme.lower() != "bearer"
            or not token
            or len(token) > 8192
            or activity.get("channelId") != "msteams"
            or not is_trusted_connector_url(service_url, self._allowed_hosts)
        ):
            raise HTTPException(403, "Invalid Bot Connector activity")

        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise jwt.InvalidTokenError
            key = await self._signing_key(kid)
            endorsements = key.get("endorsements") if key else None
            if not isinstance(endorsements, list) or "msteams" not in endorsements:
                raise jwt.InvalidTokenError
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key, algorithm="RS256"),
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=self._app_id,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "exp", "nbf"], "strict_aud": True},
            )
            signed_url = claims.get("serviceurl", claims.get("serviceUrl"))
            if signed_url != service_url or (
                "serviceUrl" in claims and claims["serviceUrl"] != signed_url
            ):
                raise jwt.InvalidTokenError
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise HTTPException(403, "Invalid Bot Connector activity") from None
