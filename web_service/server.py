"""Test-server entry point: persistent delegated ingestion, reviews and Web Push."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import create_app
from .collection_types import (
    DeviceSnapshot,
    PermissionReadiness,
    PermissionSnapshot,
    PreparedCollection,
)
from .composition import compose_server
from .connections import ConnectionTokens
from .push import Notifications
from .push_routes import install_push_routes
from .reminder_routes import ReminderRule as ReminderRule
from .reminder_routes import install_reminder_routes
from .review_routes import ChangeDecision as ChangeDecision
from .review_routes import install_review_routes
from .school import SchoolIntegration
from .school_security import SecurityBoundary
from .server_routes import Feedback as Feedback
from .server_routes import install_server_routes
from .storage import Missing, PermissionPending, digest
from .sync_ports import SyncResult
from .sync_workflow import run_sync


class ServerIntegration(SchoolIntegration):
    def __init__(self, settings, db, identity, graph, source, sender):
        super().__init__(settings, db, graph, source)
        self.identity = identity
        self.tokens = ConnectionTokens(db, identity)
        self.sender = sender
        self.notifications = Notifications(db, sender)
        self._cookie_responses = {}
        self._collection_proofs: dict[str, PreparedCollection] = {}
        self.notification_runtime = None
        self.device_cookie = (
            "__Host-calendarsync_device" if settings.secure_cookies else "calendarsync_device"
        )

    @property
    def push_enabled(self):
        return self.sender.configured

    @property
    def shared_ingestion_enabled(self):
        return bool(
            getattr(self.db, "shared_ingestion_enabled", False) and hasattr(self.source, "collect")
        )

    # 장치 정보와 DB의 권한 증명이 여전히 유효할 때만 이전 채널 경로를 재사용한다.
    def collection_proof(self, device: DeviceSnapshot) -> PreparedCollection | None:
        """Reuse verified routes only within the original permission lifetime."""
        prepared = self._collection_proofs.get(device["id_hash"])
        if not prepared or prepared["valid_until"] <= self.db.clock():
            return None
        if any(prepared["device"][key] != device[key] for key in prepared["device"]):
            return None
        proof: PermissionSnapshot = self.db.teams_read_proof(device["user_id"], device["tenant_id"])
        if (
            proof["state"] not in {"connected", "checking"}
            or proof["checked_at"] + self.settings.access_seconds <= self.db.clock()
            or proof["channels"] != frozenset(ch["id"] for ch in prepared["channels"])
        ):
            return None
        return prepared

    def begin_login(self, provider, request):
        options = {"background": request.query_params.get("background") == "1"}
        if request.query_params.get("assignments") == "1":
            if not getattr(self, "assignments_enabled", False):
                raise ValueError("Assignments unavailable")
            options["assignments"] = True
        return provider.begin(**options)

    # 이 브라우저의 이전 장치 연결을 해제하고 새 로그인 세션과 장치 연결을 묶는다.
    def complete_sign_in(self, raw, user, claims, request):
        old = request.cookies.get(self.device_cookie)
        if old:
            self.db.revoke_device(digest(old))
        proof = claims["_graph"]
        link = claims.get("_connection", {})
        extra = {"assignment_access": True} if link.get("assignment_access") else {}
        device, age = self.db.add_connection(
            raw,
            user,
            proof["token"],
            int(proof["expires_in"]),
            link.get("cache", ""),
            bool(link.get("background")),
            **extra,
        )
        self._cookie_responses[digest(raw)] = (device, age)
        self.db.audit("connected", user)

    def login_response(self, response, raw):
        device, age = self._cookie_responses.pop(digest(raw))
        response.set_cookie(
            self.device_cookie,
            device,
            max_age=age,
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def logout_response(self, response):
        response.delete_cookie(
            self.device_cookie,
            path="/",
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )

    # 세션·전용 헤더 조건을 확인해 조회 세션이 만료된 뒤에도 장치 연결을 해제할 수 있게 한다.
    def logout_request(self, request):
        session = self.db.session(request.cookies.get(self.session_cookie))
        if request.headers.get("x-calendarsync-logout") != "1":
            # Retain session+CSRF compatibility for existing clients. The dedicated
            # non-simple header also permits device revocation after session expiry.
            if not session:
                raise HTTPException(401, "Sign in or request device logout")
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(
                supplied.encode("utf-8"), session["csrf_token"].encode("ascii")
            ):
                raise HTTPException(403, "A valid CSRF token is required")
        # SecurityBoundary rejects foreign Origin/Fetch-Site values. Cross-origin
        # browsers cannot add the header because this app grants no CORS access.
        raw = request.cookies.get(self.device_cookie)
        if raw and len(raw) <= 128:
            self.db.revoke_device(digest(raw))
        elif session:
            try:
                device = self.db.session_device(session)
            except Missing:
                return
            self.db.revoke_device(device["id_hash"], session["user_id"])

    def explicit_logout(self, session):
        self.db.revoke_device(self.db.session_device(session)["id_hash"], session["user_id"])

    def status(self, session):
        result = super().status(session)
        readiness = self._teams_readiness(session) if self.shared_ingestion_enabled else None
        if readiness is not None:
            result.update(
                state=readiness["state"],
                teams_verified=readiness["verified"],
                retryable=readiness["retryable"],
                retain_teams_snapshot=not readiness["verified"] and readiness["retryable"],
            )
        try:
            device = self.db.session_device(session)
        except Missing:
            return {
                **result,
                "connection_state": "reauth_required",
                "background_enabled": False,
                "push_configured": self.sender.configured,
                "push_registered": False,
                "ingestion_pending": False,
            }
        pending = self.db.ingestion_pending(device) if self.shared_ingestion_enabled else False
        return {
            **result,
            "mode": "test-server",
            "background_enabled": bool(device["background"]),
            "connection_expires": device["expires_at"],
            "connection_state": device["state"],
            "push_configured": self.sender.configured,
            "push_registered": bool(self.db.subscriptions(device["id_hash"])),
            "ingestion_mode": "direct" if hasattr(self.source, "collect") else "oracle",
            "ingestion_pending": pending,
        }

    def run_sync(
        self,
        device: DeviceSnapshot,
        ingest: bool = False,
        force: bool = False,
        *,
        prepare_collection: bool = False,
        prepared: PreparedCollection | None = None,
    ) -> SyncResult:
        return run_sync(
            self, device, ingest, force, prepare_collection=prepare_collection, prepared=prepared
        )

    # 응답 후 수집을 실행하며, 실패해도 이미 보낸 응답 대신 다음 worker 재시도로 이어지게 한다.
    def run_deferred_ingestion(self, device, prepared=None):
        try:
            self.run_sync(device, ingest=True, prepared=prepared)
        except Exception:
            # A response has already been sent. The durable due-device request
            # will retry database failures without failing a finished response.
            try:
                self.db.audit("worker_failed", device["user_id"])
            except Exception:
                pass

    # 공유 모드는 권한 확인과 수집 예약 후 먼저 응답하고, 그 외 모드는 수집 결과까지 받아 응답한다.
    def sync(self, session, background_tasks=None):
        if self.shared_ingestion_enabled:
            device = self.db.session_device(session)
            prepared = self.collection_proof(device) or self.run_sync(
                device, force=True, prepare_collection=True
            )
            result = self.status(session)
            if result["state"] == "checking":
                result["ingestion"] = {"state": "busy"}
            elif result["state"] == "connected" and self.db.request_ingestion(device["id_hash"]):
                # Durable due-device scheduling survives a lost response or
                # process restart; the response task simply starts it sooner.
                if background_tasks is not None:
                    background_tasks.add_task(
                        self.run_deferred_ingestion, device, prepared=prepared
                    )
                result["ingestion"] = {"state": "queued"}
            return result
        ingestion = self.run_sync(self.db.session_device(session), ingest=True)
        result = self.status(session)
        if ingestion is not None:
            result["ingestion"] = ingestion
        return result

    # 기존 권한 증명의 유효기간과 채널 범위를 읽어 확인 상태를 계산하며 권한을 연장하지 않는다.
    def _teams_readiness(self, session) -> PermissionReadiness:
        """Read the original proof without creating or extending access."""
        if hasattr(self.db, "teams_read_proof"):
            proof = self.db.teams_read_proof(session["user_id"], session["tenant_id"])
            channel_ids = proof["channels"]
            coherent = True
        else:
            # Compatibility for small injected test adapters only. Production
            # reads the state and its channel scope in one database snapshot.
            before = self.db.sync_status(session["user_id"])
            channels = self.db.channels(session["user_id"], session["tenant_id"])
            proof = self.db.sync_status(session["user_id"])
            channel_ids = frozenset(channel["id"] for channel in channels)
            coherent = all(before.get(key) == proof.get(key) for key in ("state", "checked_at"))
        state = proof["state"]
        if state not in PermissionPending.STATES:
            state = "verification_failed"
        checked = proof.get("checked_at", 0)
        fresh = checked > 0 and checked + self.settings.access_seconds > self.db.clock()
        # A freshly verified zero-channel result is authoritative. A checking
        # result needs at least one existing, still-unexpired channel proof.
        verified = bool(
            coherent and fresh and (state == "connected" or (state == "checking" and channel_ids))
        )
        return {
            "state": state,
            "verified": verified,
            "retryable": (not verified or state == "checking")
            and state not in {"reauth_required", "account_not_allowed", "source_not_configured"},
            "channels": frozenset(channel_ids),
        }

    def _raise_unverified(self, readiness: PermissionReadiness):
        raise PermissionPending(
            state=readiness["state"],
            retryable=readiness["retryable"] or readiness["verified"],
            retain_snapshot=readiness["retryable"] or readiness["verified"],
            snapshot_channels=sorted(readiness["channels"]) if readiness["verified"] else None,
        )

    def require_permission_ready(self, session):
        if not self.shared_ingestion_enabled:
            return
        readiness = self._teams_readiness(session)
        if not readiness["verified"]:
            self._raise_unverified(readiness)

    # 조회 전후의 권한과 채널 범위를 비교해 조회 도중 권한이 바뀐 결과는 반환하지 않는다.
    def read_teams(self, session, operation):
        """Never publish a read that crossed proof expiry or a scope change."""
        if not self.shared_ingestion_enabled:
            return operation()
        before = self._teams_readiness(session)
        if not before["verified"]:
            self._raise_unverified(before)
        result = operation()
        after = self._teams_readiness(session)
        if not after["verified"] or before["channels"] != after["channels"]:
            self._raise_unverified(after)
        return result

    # 요청의 세션·빈도를 검사하고 Teams 경로에만 별도의 권한 확인을 적용한다.
    def check_request(self, session, request):
        checked = self.db.school_session(
            request.cookies.get(self.session_cookie), self.settings.idle_seconds
        )
        if not checked:
            raise HTTPException(401, "Viewing session expired; device notifications remain linked")
        if not self.db.allow("api:" + session["user_id"], 300):
            raise HTTPException(429, "Too many requests")
        path = request.url.path
        if path in {
            "/api/channels",
            "/api/today",
            "/api/calendar",
            "/api/hidden",
            "/api/changes",
        } or path.startswith(("/api/events/", "/api/channels/", "/api/changes/")):
            self.run_sync(self.db.session_device(session), ingest=False)
            self.require_permission_ready(session)
            if (
                self.shared_ingestion_enabled
                and request.method == "GET"
                and (
                    path in {"/api/today", "/api/calendar", "/api/hidden", "/api/changes"}
                    or path.startswith(("/api/events/", "/api/changes/"))
                )
            ):
                # Polling can expose each completed post while collection is
                # running. The stored ACL and its original expiry remain the
                # only permission proof; a view never extends that proof.
                try:
                    self.db.materialize_cached_events(self.db.session_device(session))
                except PermissionPending:
                    self._raise_unverified(self._teams_readiness(session))

    # worker 실행권을 얻은 프로세스가 정리 작업, 예정된 동기화와 알림 발송을 차례로 수행한다.
    def tick(self):
        owner = secrets.token_hex(16)
        if not self.db.worker_lock(owner):
            return
        try:
            self.db.maintenance()
            if getattr(self.db, "planner_enhancements_enabled", False):
                self.db.planner_maintenance()
            for device in self.db.due_devices(3):
                self.db.defer_device(device["id_hash"], 60)
                # In injected notification mode, do not poll Graph on the timer.
                if self.notification_runtime is None or not hasattr(self.source, "collect"):
                    self.run_sync(device, ingest=device["ingest_at"] <= self.db.clock())
                current = self.db.device(device["id_hash"])
                if current and current["state"] != "reauth_required":
                    self.notifications.dispatch(current)
        finally:
            self.db.worker_unlock(owner)


# 서버 구성 요소와 기능별 라우트를 조립하고 앱 수명에 맞춰 주기 작업을 시작·종료한다.
def create_test_server(
    settings=None,
    database=None,
    identity=None,
    graph=None,
    source=None,
    sender=None,
    worker=True,
    preview=False,
    notification_runtime=None,
    assignments_enabled=False,
    *,
    environ=None,
):
    components = compose_server(
        settings,
        database,
        identity,
        graph,
        source,
        sender,
        preview=preview,
        notification_runtime=notification_runtime,
        environ=environ,
    )
    settings, db, provider, graph, source = (
        components.settings,
        components.database,
        components.identity,
        components.graph,
        components.source,
    )
    integration = ServerIntegration(settings, db, provider, graph, source, components.sender)
    integration.notification_runtime = notification_runtime
    static = Path(__file__).with_name("static")
    app = create_app(
        settings,
        db,
        provider,
        frontend_directory=static,
        integration=integration,
        frontend_entry="school.html",
    )
    app.state.integration = integration
    integration.assignments_enabled = bool(assignments_enabled and not preview)
    if integration.assignments_enabled:
        from .assignments import AssignmentGraph, AssignmentService

        assignment_graph = AssignmentGraph(client_factory=getattr(graph, "client_factory", None))
        app.state.assignments = AssignmentService(db, integration.tokens, assignment_graph)
        app.state.assignments.initialize()
    if notification_runtime is not None:
        # Explicit injection only: the default entry point is not ready for deployment.
        from .notification_webhook import create_notification_router

        app.include_router(create_notification_router(notification_runtime.inbox))
    app.add_middleware(SecurityBoundary, settings=settings)
    require = app.state.require_session

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse({"detail": "Check the input and dates"}, status_code=422)

    @app.exception_handler(Exception)
    async def failed(request, error):
        return JSONResponse({"detail": "Request failed; check service status"}, status_code=500)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def loop():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(integration.tick)
                    if notification_runtime is not None:
                        await asyncio.to_thread(notification_runtime.tick)
                except Exception:
                    try:
                        db.audit("worker_failed", "worker")
                    except Exception:
                        # A database outage can also prevent the audit write.
                        # Keep the retry loop alive without logging private errors.
                        pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                except TimeoutError:
                    pass

        task = asyncio.create_task(loop()) if worker else None
        yield
        stop.set()
        if task:
            try:
                await asyncio.wait_for(task, timeout=5)
            except TimeoutError:
                task.cancel()

    app.router.lifespan_context = lifespan

    install_server_routes(app, settings, db, integration, require, static, preview)
    install_review_routes(app, db, integration, require)
    install_reminder_routes(app, db, integration, require)
    install_push_routes(app, db, integration, require)

    return app
