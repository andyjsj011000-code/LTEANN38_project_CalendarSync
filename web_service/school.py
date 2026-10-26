"""School web entry point. Start with uvicorn web_service.school:create_school_app --factory."""
from pathlib import Path
from fastapi import Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from .api import create_app, SESSION_COOKIE
from .config import Settings
from .identity import MicrosoftIdentity, validate_identity
from .oracle_source import source_from_env
from .school_graph import DelegatedGraph, GraphFailure, SCOPES
from .school_storage import SchoolDatabase
from .school_security import SecurityBoundary

class SchoolIdentity(MicrosoftIdentity):
    def begin(self):
        flow=self._application().initiate_auth_code_flow(scopes=SCOPES,redirect_uri=self.settings.callback_url,prompt="select_account")
        if not flow.get("auth_uri") or not flow.get("state"):
            raise ValueError("Authentication unavailable")
        return flow

    def finish(self,flow,query):
        result=self._application().acquire_token_by_auth_code_flow(flow,query)
        claims=result.get("id_token_claims")
        if not claims or not result.get("access_token"):
            raise ValueError("Authentication failed")
        granted={s.lower().removeprefix("https://graph.microsoft.com/") for s in result.get("scope","").split()}
        if not {s.lower() for s in SCOPES} <= granted or claims.get("acct")==1:
            raise ValueError("School permissions required")
        return {**claims,"_graph":{"token":result["access_token"],"expires_in":min(int(result.get("expires_in",0)),3600)}}

class SchoolIntegration:
    def __init__(self,settings,db,graph,source):
        self.settings,self.db,self.graph,self.source=settings,db,graph,source
        self.session_cookie="__Host-calendarsync_session" if settings.secure_cookies else SESSION_COOKIE

    def limit_login(self,request):
        # Trust the transport peer only. Proxy rate limiting is configured separately.
        peer=request.client.host if request.client else "unknown"
        if not self.db.allow("auth:"+peer,20) or not self.db.allow("auth-global",200):
            raise HTTPException(429,"Too many sign-in attempts; try again later")

    def validate_sign_in(self,claims):
        proof=claims.get("_graph",{}); expiry=int(proof.get("expires_in",0))
        if not 60<=expiry<=3600 or not proof.get("token"):
            raise ValueError("No delegated credential")
        # Guest accounts and a token for a different principal cannot get a session.
        self.graph.assert_member(proof["token"],claims["oid"])

    def bind_sign_in(self,raw,user_id,claims):
        proof = claims["_graph"]
        self.db.bind_token(raw,proof["token"],int(self.db.clock())+int(proof["expires_in"]))
        self.db.audit("sign_in",user_id)

    def status(self,session):
        status=self.db.sync_status(session["user_id"])
        return {"state":status["state"],"checked_at":status["checked_at"],"source_configured":self.source.configured,"permission_cache_seconds":self.settings.access_seconds}

    def sync(self,session):
        lease = self.db.begin_sync(session["user_id"],self.settings.access_seconds)
        if not lease:
            return self.status(session)
        try:
            channels=self.graph.channels(self.db.token(session),session["object_id"],session["tenant_id"])
            if not self.source.configured:
                self.db.fail_sync(session["user_id"],"source_not_configured",lease)
            else:
                events=self.source.events(channels,session["tenant_id"])
                self.db.apply_snapshot(session,channels,events,self.settings.access_seconds,lease)
        except GraphFailure as error:
            self.db.fail_sync(session["user_id"],error.state,lease)
            self.db.audit("graph_denied",session["user_id"])
        except Exception:
            # Never log HTTP errors, OAuth codes, tokens or raw Oracle exceptions.
            self.db.fail_sync(session["user_id"],"source_unavailable",lease)
            self.db.audit("source_unavailable",session["user_id"])
        return self.status(session)

    def check_request(self,session,request):
        checked=self.db.school_session(request.cookies.get(self.session_cookie),self.settings.idle_seconds)
        if not checked:
            raise HTTPException(401,"School session expired; sign in again")
        if not self.db.allow("api:"+session["user_id"],300):
            raise HTTPException(429,"Too many requests; try again later")
        path=request.url.path
        if path in {"/api/channels","/api/today","/api/calendar","/api/hidden"} or path.startswith(("/api/events/","/api/channels/")):
            self.sync(session)


def create_school_app(settings=None,database=None,identity=None,graph=None,source=None):
    settings=settings or Settings.from_env()
    if not settings.auth_configured:
        raise ValueError("Configure the school's Entra tenant, client and secret")
    if not settings.secure_cookies and not settings.allow_local_login:
        raise ValueError("HTTPS is required; local login must be explicitly enabled for development")
    db=database or SchoolDatabase(settings.database_path,settings.data_key)
    if not isinstance(db,SchoolDatabase):
        raise ValueError("School accounts require isolated school storage")
    integration=SchoolIntegration(settings,db,graph or DelegatedGraph(),source if source is not None else source_from_env())
    frontend=Path(__file__).with_name("static")
    app=create_app(settings,db,identity or SchoolIdentity(settings),frontend_directory=frontend,integration=integration,frontend_entry="school.html")
    app.state.integration=integration
    app.add_middleware(SecurityBoundary,settings=settings)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,error):
        return JSONResponse({"detail":"Check the input and dates"},status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request,error):
        return JSONResponse({"detail":"The request could not be completed"},status_code=500)

    require=app.state.require_session

    @app.get("/api/service")
    def service():
        return {"mode":"school","school_login":True,"synthetic_data_only":False,"idle_seconds":settings.idle_seconds}

    @app.post("/api/teams/sync")
    def sync(session=Depends(require)):
        return integration.sync(session)

    @app.post("/auth/logout-all",status_code=204)
    def logout_all(session=Depends(require)):
        db.revoke_all(session["user_id"]); db.audit("logout_all",session["user_id"])
        response=Response(status_code=204); response.delete_cookie(integration.session_cookie,path="/",secure=settings.secure_cookies,httponly=True,samesite="lax")
        return response

    @app.get("/privacy")
    def privacy():
        return FileResponse(Path(__file__).parent.parent/"docs"/"SCHOOL_PRIVACY.md",media_type="text/plain; charset=utf-8")

    return app
