"""Application-scoped request authentication shared by every route group.

Keep the same callable in app.state.require_session so school, server and
opt-in planner routes use the same session, tenant, CSRF and integration checks.
"""

import hmac
from collections.abc import Callable
from typing import TypedDict

from fastapi import HTTPException, Request


class SessionIdentity(TypedDict):
    """Only the identity fields consumed by these routes; the session has more."""

    user_id: str
    tenant_id: str


SessionDependency = Callable[[Request], SessionIdentity]


def create_session_dependency(settings, db, session_cookie, integration=None) -> SessionDependency:
    """Bind one application's collaborators without storing request state."""

    # 세션·학교 테넌트를 확인하고 쓰기 요청의 Origin·CSRF 검사 후 앱별 추가 검사를 실행한다.
    def current_session(request: Request) -> SessionIdentity:
        session = db.session(request.cookies.get(session_cookie))
        if not session or (settings.tenant_id and session["tenant_id"] != settings.tenant_id):
            raise HTTPException(401, "Sign in with your school account")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and origin != settings.origin:
                raise HTTPException(403, "Invalid request origin")
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(
                supplied.encode("utf-8"), session["csrf_token"].encode("ascii")
            ):
                raise HTTPException(403, "A valid CSRF token is required")
        if integration:
            integration.check_request(session, request)
        return session

    return current_session
