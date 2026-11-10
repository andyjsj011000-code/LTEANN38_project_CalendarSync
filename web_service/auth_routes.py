"""Sign-in and sign-out HTTP adapters; preserve the existing cookie contract."""

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .identity import Unavailable, validate_identity


def install_auth_routes(
    app, settings, db, provider, current_session, integration, session_cookie, flow_cookie
):
    def cookie(response, name, value, max_age):
        response.set_cookie(
            name,
            value,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            max_age=max_age,
            path="/",
        )

    # 일회용 로그인 흐름을 저장하고 그 흐름을 식별하는 쿠키와 함께 학교 인증 화면으로 이동시킨다.
    @app.get("/auth/login")
    def login(request: Request):
        if integration:
            integration.limit_login(request)
        try:
            flow = (
                integration.begin_login(provider, request)
                if integration and hasattr(integration, "begin_login")
                else provider.begin()
            )
        except Exception:
            # Do not return provider errors, tokens, or credential configuration.
            raise HTTPException(503, "School sign-in is not available yet") from None
        raw = db.store_flow(flow)
        response = RedirectResponse(flow["auth_uri"], status_code=303)
        cookie(response, flow_cookie, raw, 600)
        return response

    # 일회용 인증 흐름과 계정 정보를 검증해 세션을 만들고, 실패하면 생성 중인 세션과 쿠키를 정리한다.
    @app.get("/auth/callback")
    def callback(request: Request):
        raw = None
        try:
            if integration:
                integration.limit_login(request)
            if len(request.url.query) > 16000:
                raise ValueError("Invalid authorization response")
            query = dict(request.query_params)
            if any(len(request.query_params.getlist(k)) != 1 for k in query):
                raise ValueError("Duplicate authorization fields")
            flow = db.consume_flow(request.cookies.get(flow_cookie))
            claims = provider.finish(flow, query)
            tenant, oid, profile = validate_identity(claims, settings, db.clock())
            if integration:
                integration.validate_sign_in(claims)
            user_id = db.save_user(tenant, oid, profile)
            old_session = request.cookies.get(session_cookie)
            if old_session:
                db.revoke_session(old_session)
            raw = db.issue_session(user_id, settings.session_seconds)
            if integration:
                if hasattr(integration, "complete_sign_in"):
                    integration.complete_sign_in(raw, user_id, claims, request)
                else:
                    integration.bind_sign_in(raw, user_id, claims)
        except Unavailable:
            return JSONResponse({"detail": "School sign-in is not available yet"}, status_code=503)
        except Exception:
            if raw:
                db.revoke_session(raw)
            response = (
                RedirectResponse("/?login=failed", status_code=303)
                if integration
                else JSONResponse({"detail": "Sign-in failed; start again"}, status_code=400)
            )
            response.delete_cookie(
                flow_cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
            )
            return response
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            flow_cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
        )
        cookie(response, session_cookie, raw, settings.session_seconds)
        if integration and hasattr(integration, "login_response"):
            integration.login_response(response, raw)
        return response

    # 통합 서버의 장치 연결과 조회 세션을 해제하고 브라우저의 로그인 쿠키를 지운다.
    @app.post("/auth/logout", status_code=204)
    def logout(request: Request):
        if integration and hasattr(integration, "logout_request"):
            # A persistent device must remain revocable after its viewing session expires.
            integration.logout_request(request)
        else:
            session = current_session(request)
            if integration and hasattr(integration, "explicit_logout"):
                integration.explicit_logout(session)
        raw_session = request.cookies.get(session_cookie)
        if raw_session:
            db.revoke_session(raw_session)
        response = Response(status_code=204)
        response.delete_cookie(
            session_cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
        )
        if integration and hasattr(integration, "logout_response"):
            integration.logout_response(response)
        return response
