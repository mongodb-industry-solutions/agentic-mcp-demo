#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Global HTTP Basic Auth gate shared by the web shell and both dashboards.

One shared credential protects each app — the HTML page AND the
WebSocket (browsers replay cached Basic-Auth creds on same-origin WS
upgrades, so one prompt covers both). This is a demo doorkeeper, not
per-user auth: it keeps the public URLs from being wide open. It is
orthogonal to the per-browser-session data isolation (MULTI_SESSION_PLAN),
which keys off the localStorage session token behind this gate.

Configure with SHELL_AUTH_USER / SHELL_AUTH_PASS (default
mdb / mdbagentic2026); disable entirely with SHELL_AUTH_DISABLE=1.
Each app passes its own realm string so the browser prompt is labelled.
"""

import base64
import hmac
import logging
import os

log = logging.getLogger("auth")

AUTH_USER = os.environ.get("SHELL_AUTH_USER", "mdb")
AUTH_PASS = os.environ.get("SHELL_AUTH_PASS", "mdbagentic2026")


class BasicAuthMiddleware:
    """Pure-ASGI Basic-Auth gate over both http and websocket scopes."""

    def __init__(self, app, username: str, password: str, realm: str):
        self.app = app
        self._expected = "Basic " + base64.b64encode(
            f"{username}:{password}".encode()).decode()
        self.realm = realm

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            # constant-time compare so a wrong password can't be timed out
            if not hmac.compare_digest(provided, self._expected):
                if scope["type"] == "http":
                    await send({
                        "type": "http.response.start", "status": 401,
                        "headers": [
                            (b"www-authenticate",
                             f'Basic realm="{self.realm}"'.encode()),
                            (b"content-type", b"text/plain; charset=utf-8"),
                        ],
                    })
                    await send({"type": "http.response.body",
                                "body": b"Authentication required."})
                else:  # websocket: reject the upgrade (1008 = policy violation)
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


def install_basic_auth(app, realm: str) -> None:
    """Add the Basic-Auth gate to a FastAPI/Starlette app, unless
    SHELL_AUTH_DISABLE is set."""
    if os.environ.get("SHELL_AUTH_DISABLE"):
        log.warning("SHELL_AUTH_DISABLE set — %s is UNAUTHENTICATED", realm)
        return
    app.add_middleware(BasicAuthMiddleware,
                       username=AUTH_USER, password=AUTH_PASS, realm=realm)
