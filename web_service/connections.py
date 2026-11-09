"""MSAL caches belong to a device connection, independently of a viewing session."""

from __future__ import annotations

from requests.exceptions import ConnectionError, RequestException, Timeout

from .collection_types import DeviceSnapshot
from .connection_snapshot import acquire_snapshot as acquire_snapshot
from .ports import ConnectionStore, TokenRefresher
from .school import SchoolIdentity
from .school_graph import SCOPES, GraphFailure


class PersistentIdentity(SchoolIdentity):
    def application(self, cache=None, background=True):
        from msal import ConfidentialClientApplication

        return ConfidentialClientApplication(
            self.settings.client_id,
            client_credential=self.settings.client_secret,
            authority=f"https://login.microsoftonline.com/{self.settings.tenant_id}",
            token_cache=cache,
            exclude_scopes=[] if background else ["offline_access"],
            timeout=10,
        )

    def begin(self, background=False, assignments=False):
        from .assignments import ASSIGNMENT_SCOPE

        scopes = SCOPES + ([ASSIGNMENT_SCOPE] if assignments else [])
        flow = self.application(background=background).initiate_auth_code_flow(
            scopes=scopes, redirect_uri=self.settings.callback_url, prompt="select_account"
        )
        if not flow.get("auth_uri") or not flow.get("state"):
            raise ValueError("Authentication unavailable")
        flow["background_allowed"] = background
        flow["assignments_requested"] = bool(assignments)
        return flow

    def finish(self, flow, query):
        from msal import SerializableTokenCache

        cache = SerializableTokenCache()
        background = bool(flow.get("background_allowed"))
        result = self.application(cache, background).acquire_token_by_auth_code_flow(flow, query)
        claims = result.get("id_token_claims")
        granted = {
            s.lower().removeprefix("https://graph.microsoft.com/")
            for s in result.get("scope", "").split()
        }
        if (
            not claims
            or claims.get("acct") == 1
            or not result.get("access_token")
            or not {s.lower() for s in SCOPES} <= granted
        ):
            raise ValueError("School permissions required")
        from .assignments import ASSIGNMENT_SCOPE

        access = ASSIGNMENT_SCOPE.lower() in granted
        if flow.get("assignments_requested") and not access:
            raise ValueError("Assignment permission required")
        return {
            **claims,
            "_graph": {
                "token": result["access_token"],
                "expires_in": min(int(result.get("expires_in", 0)), 3600),
            },
            "_connection": {
                "cache": cache.serialize() if background else "",
                "background": background,
                "assignment_access": access,
            },
        }

    def refresh(
        self, serialized: str, oid: str, tenant: str, *, assignments: bool = False
    ) -> tuple[str, int, str]:
        from msal import SerializableTokenCache

        cache = SerializableTokenCache()
        cache.deserialize(serialized)
        app = self.application(cache)
        accounts = [
            a
            for a in app.get_accounts()
            if a.get("local_account_id", "").lower() == oid.lower()
            and a.get("realm", "").lower() == tenant.lower()
        ]
        if len(accounts) != 1:
            raise GraphFailure("reauth_required")
        from .assignments import ASSIGNMENT_SCOPE

        scopes = SCOPES + ([ASSIGNMENT_SCOPE] if assignments else [])
        result = app.acquire_token_silent_with_error(scopes, account=accounts[0])
        if (result or {}).get("error") in {"temporarily_unavailable", "server_error"}:
            raise GraphFailure("refresh_unavailable")
        granted = {
            s.lower().removeprefix("https://graph.microsoft.com/")
            for s in (result or {}).get("scope", "").split()
        }
        if (
            not result
            or not result.get("access_token")
            or not {s.lower() for s in scopes} <= granted
        ):
            raise GraphFailure("reauth_required")
        return (
            result["access_token"],
            min(int(result.get("expires_in", 0)), 3600),
            cache.serialize(),
        )


class ConnectionTokens:
    def __init__(self, db: ConnectionStore, identity: TokenRefresher) -> None:
        self.db, self.identity = db, identity

    def acquire(self, device: DeviceSnapshot) -> str:
        current = self.db.device(device["id_hash"])
        if not current:
            raise GraphFailure("reauth_required")
        payload = self.db.unseal(current["credential_blob"])
        token = payload.get("token")
        if current["token_expires"] > self.db.clock() + 60 and token:
            return token
        cache = payload.get("cache")
        if not current["background"] or not cache:
            self.db.fail_connection(current["id_hash"])
            raise GraphFailure("reauth_required")
        lease = self.db.lock_refresh(current["id_hash"])
        if not lease:
            raise GraphFailure("refresh_busy")
        try:
            options = {"assignments": True} if payload.get("assignment_access") else {}
            token, lifetime, cache = self.identity.refresh(
                cache, current["object_id"], current["tenant_id"], **options
            )
            if lifetime < 60 or not self.db.save_refresh(
                current["id_hash"], lease, token, lifetime, cache
            ):
                raise GraphFailure("reauth_required")
            return token
        except GraphFailure as error:
            if error.state == "refresh_unavailable":
                # Keep the encrypted cache and existing refresh lease as a short retry backoff.
                raise GraphFailure("refresh_unavailable") from None
            self.db.fail_connection(current["id_hash"], lease)
            raise GraphFailure("reauth_required") from None
        except RequestException as error:
            status = error.response.status_code if error.response is not None else None
            if (
                isinstance(error, (Timeout, ConnectionError))
                or status == 429
                or (status is not None and 500 <= status < 600)
            ):
                raise GraphFailure("refresh_unavailable") from None
            self.db.fail_connection(current["id_hash"], lease)
            raise GraphFailure("reauth_required") from None
        except Exception:
            self.db.fail_connection(current["id_hash"], lease)
            raise GraphFailure("reauth_required") from None
