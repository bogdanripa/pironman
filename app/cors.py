"""CORS for the read-only endpoints the dashboard calls.

The dashboard is its own app on its own hostname, so its `fetch` calls are
cross-origin and the browser needs CORS headers plus a preflight answer for the
Authorization header.

Deliberately narrow rather than Starlette's CORSMiddleware:

  - it applies only to the read-only analytics/stats paths, so no write endpoint
    ever becomes browser-reachable from another origin; and
  - it is **pure ASGI** and never touches /mcp. The MCP transport streams SSE,
    and wrapping it in response-rewriting middleware is what previously produced
    "Invalid content from server" in the connector (see PromoteKeyMiddleware).

Authentication is unchanged — every one of these paths still requires the bearer
key. CORS decides which *origins a browser will hand a response to*, not who may
call the API; curl ignores it entirely.
"""
ALLOWED_PATHS = ("/analytics/", "/stats/")
ALLOW_HEADERS = "authorization, content-type"
ALLOW_METHODS = "GET, OPTIONS"


class CorsMiddleware:
    def __init__(self, app, allow_origins: tuple[str, ...] = ()):
        self.app = app
        self.allow_origins = allow_origins

    def _origin_allowed(self, origin: str) -> bool:
        return bool(origin) and (("*" in self.allow_origins)
                                 or origin in self.allow_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(ALLOWED_PATHS):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode()
                   for k, v in (scope.get("headers") or [])}
        origin = headers.get("origin", "")
        if not self._origin_allowed(origin):
            await self.app(scope, receive, send)
            return

        extra = [
            (b"access-control-allow-origin", origin.encode()),
            (b"vary", b"Origin"),
            (b"access-control-allow-headers", ALLOW_HEADERS.encode()),
            (b"access-control-allow-methods", ALLOW_METHODS.encode()),
            (b"access-control-max-age", b"600"),
        ]

        # Answer the preflight here: it carries no auth, so letting it reach the
        # endpoint would only produce a 401 the browser reads as "denied".
        if scope.get("method") == "OPTIONS":
            await send({"type": "http.response.start", "status": 204,
                        "headers": extra})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers") or []) + extra
            await send(message)

        await self.app(scope, receive, send_with_cors)
