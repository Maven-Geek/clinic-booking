"""API key check (level 2) and the server-side bridge the web page uses.

Every API path needs a valid X-API-Key header. The patient web page is a public page, so it
must not carry the key itself. Instead it calls /web/api/<path>; this middleware maps that to
the real endpoint and lets it through as a trusted call. Only the endpoints a patient needs
are bridged, so /reset can never be reached from the page.
"""

import hmac
import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config

PUBLIC_PATHS = {"/", "/health", "/favicon.ico"}
WEB_PREFIX = "/web/api"

_BOOKING_ID_PATH = re.compile(r"^/bookings/[^/]+$")


def _web_page_may_call(method: str, path: str) -> bool:
    if path == "/slots":
        return method == "GET"
    if path == "/bookings":
        return method in ("GET", "POST")
    if _BOOKING_ID_PATH.match(path):
        return method in ("GET", "DELETE")
    return False


def _key_is_valid(supplied: str | None) -> bool:
    if not supplied:
        return False
    # Constant-time comparison so the key can't be guessed byte by byte from response timing.
    return hmac.compare_digest(supplied.encode(), config.API_KEY.encode())


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method: str = scope["method"]

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if path.startswith(WEB_PREFIX + "/"):
            inner = path[len(WEB_PREFIX):]
            if not _web_page_may_call(method, inner):
                await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
                return
            scope = dict(scope, path=inner, raw_path=inner.encode())
            await self.app(scope, receive, send)
            return

        if not _key_is_valid(Headers(scope=scope).get("x-api-key")):
            await JSONResponse({"error": "missing or invalid API key"}, status_code=401)(scope, receive, send)
            return

        await self.app(scope, receive, send)
