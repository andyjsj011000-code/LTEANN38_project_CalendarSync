"""ASGI boundary: canonical host, bounded input, generic errors and browser headers."""
from urllib.parse import urlsplit
from starlette.responses import JSONResponse

class SecurityBoundary:
    def __init__(self,app,settings):
        self.app=app; self.settings=settings

    async def __call__(self,scope,receive,send):
        if scope["type"]!="http":
            return await self.app(scope,receive,send)
        async def secure_send(message):
            if message["type"]=="http.response.start":
                headers=list(message.get("headers",[]))
                policy="default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
                values={"content-security-policy":policy,"x-frame-options":"DENY","x-content-type-options":"nosniff","referrer-policy":"no-referrer","cache-control":"no-store","cross-origin-opener-policy":"same-origin","permissions-policy":"camera=(), microphone=(), geolocation=()"}
                if self.settings.secure_cookies:
                    values["strict-transport-security"]="max-age=31536000"
                headers=[(k,v) for k,v in headers if k.lower() not in {x.encode() for x in values}]
                headers.extend((k.encode(),v.encode()) for k,v in values.items()); message["headers"]=headers
            await send(message)
        async def reject(status,detail):
            await JSONResponse({"detail":detail},status_code=status)(scope,receive,secure_send)
        headers={}
        for key,value in scope.get("headers",[]):
            key=key.lower()
            if key in (b"host",b"content-length") and key in headers:
                return await reject(400,"Invalid request")
            headers[key]=value.decode("latin-1")
        if headers.get(b"host","").lower()!=urlsplit(self.settings.origin).netloc.lower():
            return await reject(400,"Invalid request host")
        if scope["path"].startswith(("/api/","/auth/logout")):
            if headers.get(b"sec-fetch-site") in {"cross-site","same-site"} or headers.get(b"origin",self.settings.origin)!=self.settings.origin:
                return await reject(403,"Use the service from its own website")
        try:
            if int(headers.get(b"content-length","0"))>65536:
                return await reject(413,"Request is too large")
        except ValueError:
            return await reject(400,"Invalid request")
        buffered=[]; size=0
        if scope["method"] in {"POST","PUT","PATCH"}:
            while True:
                message=await receive()
                if message["type"]=="http.disconnect":
                    return
                size+=len(message.get("body",b""))
                if size>65536:
                    return await reject(413,"Request is too large")
                buffered.append(message)
                if not message.get("more_body",False): break
        async def bounded_receive():
            if buffered: return buffered.pop(0)
            return await receive()
        await self.app(scope,bounded_receive,secure_send)
