"""Opt-in Graph webhook router; subscription provisioning is not implemented."""
import json
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from .notification_queue import UnknownSubscription


def create_notification_router(inbox):
    router = APIRouter()

    async def handle(request: Request, lifecycle: bool):
        token = request.query_params.get('validationToken')
        if token is not None:
            if not token or len(token) > 4096:
                return Response(status_code=400)
            return PlainTextResponse(token, headers={'Cache-Control':'no-store'})
        size, chunks = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return Response(status_code=413)
            chunks.append(chunk)
        try:
            payload = json.loads(b''.join(chunks))
            if not isinstance(payload, dict) or not isinstance(payload.get('value'), list):
                return Response(status_code=400)
            if lifecycle:
                inbox.accept_lifecycle(payload['value'], defer_unknown=True)
            else:
                inbox.accept(payload['value'], defer_unknown=True)
        except UnknownSubscription:
            return Response(status_code=503)
        except (ValueError, UnicodeError):
            return Response(status_code=400)
        except Exception:
            # A non-2xx status asks Graph to retry; never include data in logs.
            return Response(status_code=503)
        return Response(status_code=202)

    @router.post('/hooks/graph')
    async def receive(request: Request):
        return await handle(request, False)

    @router.post('/hooks/graph/lifecycle')
    async def lifecycle(request: Request):
        return await handle(request, True)

    return router
