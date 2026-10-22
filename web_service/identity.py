"""Microsoft sign-in adapter. Browser-provided identity claims are never trusted."""
from uuid import UUID


class Unavailable(Exception):
    pass


class MicrosoftIdentity:
    def __init__(self, settings):
        self.settings = settings

    def _application(self):
        if not self.settings.auth_configured:
            raise Unavailable("School sign-in is not configured")
        try:
            from msal import ConfidentialClientApplication
        except ImportError as error:
            raise Unavailable("Install requirements-web.txt to enable Microsoft sign-in") from error
        return ConfidentialClientApplication(
            self.settings.client_id,
            client_credential=self.settings.client_secret,
            authority=f"https://login.microsoftonline.com/{self.settings.tenant_id}",
            exclude_scopes=["offline_access"],
            timeout=10,
        )

    def begin(self):
        # MSAL generates state, nonce, and the PKCE verifier. Keep the entire flow
        # encrypted on the server; only a random reference goes into the browser.
        flow = self._application().initiate_auth_code_flow(scopes=[], redirect_uri=self.settings.callback_url)
        if not flow.get("auth_uri") or not flow.get("state"):
            raise Unavailable("Microsoft sign-in could not be started")
        return flow

    def finish(self, flow, query):
        # MSAL checks the authorization response against the server-side flow.
        # Do not replace this with decoding a token or accepting a user ID.
        result = self._application().acquire_token_by_auth_code_flow(flow, query)
        claims = result.get("id_token_claims")
        if not claims:
            raise ValueError("Microsoft sign-in did not return an identity")
        return claims


def validate_identity(claims, settings, now):
    """Additional application boundary checks after the provider code flow."""
    tenant = str(UUID(claims.get("tid", "")))
    object_id = str(UUID(claims.get("oid", "")))
    expected_issuer = f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0"
    if tenant != settings.tenant_id or claims.get("iss") != expected_issuer:
        raise ValueError("Unexpected school tenant")
    if claims.get("aud") != settings.client_id or not claims.get("sub"):
        raise ValueError("Unexpected application audience")
    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)) or expiry <= now:
        raise ValueError("Expired identity")
    profile = {
        "name": str(claims.get("name") or "")[:200],
        "email": str(claims.get("preferred_username") or claims.get("email") or "")[:320],
    }
    # Email is display data. Tenant + object ID is the stable principal key.
    return tenant, object_id, profile
