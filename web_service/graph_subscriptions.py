"""Graph subscription API client. It does not own credentials or schedule work."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"


class SubscriptionFailure(Exception):
    def __init__(self, reason, retry_after=None):
        self.reason, self.retry_after = reason, retry_after
        super().__init__(reason)


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid ID") from None


class GraphSubscriptions:
    def __init__(self, origin, client_factory=None, clock=None):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("A public HTTPS origin is required")
        self.origin = origin.rstrip("/")
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _request(self, method, path, token, body=None):
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("Missing delegated token")
        try:
            with self.client_factory() as client:
                with client.stream(
                    method, GRAPH + path, headers={"Authorization": "Bearer " + token}, json=body
                ) as response:
                    if response.status_code == 401:
                        raise SubscriptionFailure("reauth_required")
                    if response.status_code == 403:
                        raise SubscriptionFailure("forbidden")
                    if response.status_code == 429:
                        raw = response.headers.get("retry-after", "")
                        raise SubscriptionFailure(
                            "throttled", min(int(raw), 3600) if raw.isdigit() else None
                        )
                    if response.status_code == 404:
                        raise SubscriptionFailure("not_found")
                    if response.status_code == 409:
                        raise SubscriptionFailure("conflict")
                    if response.status_code not in (200, 201, 202, 204):
                        raise SubscriptionFailure("unavailable")
                    raw = b""
                    for chunk in response.iter_bytes():
                        raw += chunk
                        if len(raw) > 65536:
                            raise SubscriptionFailure("invalid_response")
                    if not raw:
                        return None
                    try:
                        obj = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise SubscriptionFailure("invalid_response") from None
                    if not isinstance(obj, dict):
                        raise SubscriptionFailure("invalid_response")
                    return obj
        except httpx.HTTPError:
            raise SubscriptionFailure("unavailable") from None

    def _expiration(self):
        return (
            (self.clock().astimezone(timezone.utc) + timedelta(hours=24))
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _parse(response):
        try:
            sub_id = _uuid(response["id"])
            expires = datetime.fromisoformat(response["expirationDateTime"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError()
            return {"id": sub_id, "expires_at": expires.astimezone(timezone.utc)}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SubscriptionFailure("invalid_response") from None

    def create(self, token, team_id, channel_id, client_state):
        team = _uuid(team_id)
        if (
            not isinstance(channel_id, str)
            or not 1 <= len(channel_id) <= 200
            or any(ord(c) < 32 for c in channel_id)
        ):
            raise ValueError("Invalid channel")
        if not isinstance(client_state, str) or not 32 <= len(client_state) <= 128:
            raise ValueError("A random client state is required")
        body = {
            "changeType": "created,updated",
            "notificationUrl": self.origin + "/hooks/graph",
            "lifecycleNotificationUrl": self.origin + "/hooks/graph/lifecycle",
            "resource": "/teams/" + team + "/channels/" + quote(channel_id, safe="") + "/messages",
            "expirationDateTime": self._expiration(),
            "clientState": client_state,
            "includeResourceData": False,
        }
        return self._parse(self._request("POST", "/subscriptions", token, body))

    def renew(self, token, subscription_id):
        return self._parse(
            self._request(
                "PATCH",
                "/subscriptions/" + _uuid(subscription_id),
                token,
                {"expirationDateTime": self._expiration()},
            )
        )

    def delete(self, token, subscription_id):
        try:
            self._request("DELETE", "/subscriptions/" + _uuid(subscription_id), token)
        except SubscriptionFailure as error:
            if error.reason != "not_found":
                raise
