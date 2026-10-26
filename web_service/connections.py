"""MSAL caches belong to a device connection, independently of a viewing session."""
import secrets
from .school_graph import GraphFailure, SCOPES
from .school import SchoolIdentity

class PersistentIdentity(SchoolIdentity):
    def application(self,cache=None,background=True):
        from msal import ConfidentialClientApplication
        return ConfidentialClientApplication(self.settings.client_id,client_credential=self.settings.client_secret,authority=f"https://login.microsoftonline.com/{self.settings.tenant_id}",token_cache=cache,exclude_scopes=[] if background else ["offline_access"],timeout=10)

    def begin(self,background=False):
        flow=self.application(background=background).initiate_auth_code_flow(scopes=SCOPES,redirect_uri=self.settings.callback_url,prompt="select_account")
        if not flow.get("auth_uri") or not flow.get("state"): raise ValueError("Authentication unavailable")
        flow["background_allowed"]=background
        return flow

    def finish(self,flow,query):
        from msal import SerializableTokenCache
        cache=SerializableTokenCache(); background=bool(flow.get("background_allowed"))
        result=self.application(cache,background).acquire_token_by_auth_code_flow(flow,query)
        claims=result.get("id_token_claims")
        granted={s.lower().removeprefix("https://graph.microsoft.com/") for s in result.get("scope","").split()}
        if not claims or claims.get("acct")==1 or not result.get("access_token") or not {s.lower() for s in SCOPES}<=granted: raise ValueError("School permissions required")
        return {**claims,"_graph":{"token":result["access_token"],"expires_in":min(int(result.get("expires_in",0)),3600)},"_connection":{"cache":cache.serialize() if background else "","background":background}}

    def refresh(self,serialized,oid,tenant):
        from msal import SerializableTokenCache
        cache=SerializableTokenCache(); cache.deserialize(serialized)
        app=self.application(cache)
        accounts=[a for a in app.get_accounts() if a.get("local_account_id","").lower()==oid.lower() and a.get("realm","").lower()==tenant.lower()]
        if len(accounts)!=1: raise GraphFailure("reauth_required")
        result=app.acquire_token_silent_with_error(SCOPES,account=accounts[0])
        granted={s.lower().removeprefix("https://graph.microsoft.com/") for s in (result or {}).get("scope","").split()}
        if not result or not result.get("access_token") or not {s.lower() for s in SCOPES}<=granted: raise GraphFailure("reauth_required")
        return result["access_token"],min(int(result.get("expires_in",0)),3600),cache.serialize()

class ConnectionTokens:
    def __init__(self,db,identity): self.db,self.identity=db,identity
    def acquire(self,device):
        current=self.db.device(device["id_hash"])
        if not current: raise GraphFailure("reauth_required")
        payload=self.db.unseal(current["credential_blob"])
        if current["token_expires"]>self.db.clock()+60 and payload.get("token"):
            return payload["token"]
        if not current["background"] or not payload.get("cache"):
            self.db.fail_connection(current["id_hash"]); raise GraphFailure("reauth_required")
        lease=self.db.lock_refresh(current["id_hash"])
        if not lease: raise GraphFailure("refresh_busy")
        try:
            token,lifetime,cache=self.identity.refresh(payload["cache"],current["object_id"],current["tenant_id"])
            if lifetime<60 or not self.db.save_refresh(current["id_hash"],lease,token,lifetime,cache): raise GraphFailure("reauth_required")
            return token
        except Exception:
            self.db.fail_connection(current["id_hash"],lease)
            raise GraphFailure("reauth_required") from None
