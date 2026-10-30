#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The Atlas Agent Engine API, as the web shell on Agent Engine calls it
(web/twin_shell.py; AGENT_ENGINE_PLAN.md, Phase 8b).

A turn is an HTTP `POST …/workspaces/{id}/invoke`, not `agentengine
invoke`, because only the API can send custom headers: the browser
session's demo lane travels as `X-Mdb-Agent-Engine-Custom-Demo-Prefix`
(the agents read it as `demo-prefix`, oss_tools/_ctx.py).

The bearer token comes from one of two sources:

- **A service account** when `AE_CLIENT_ID` and `AE_CLIENT_SECRET` are
  set (client credentials, `POST /api/v1/oauth/token`). Then each browser
  session also gets its own memory identity (`user_id` in the invoke
  body). This is what a hosted shell needs.
- **The presenter's CLI login** otherwise (`~/.agentengine/auth.json`; a
  401 makes the CLI refresh it). The platform refuses a human caller who
  passes another user's `user_id` unless end-user delegation is enabled
  for the project, so every session shares the presenter's memory, and
  the shell only serves loopback clients.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

BASE_URL = os.environ.get("TWIN_AE_BASE_URL", "https://agentengine.mongodb.com")
ORG_ID = os.environ.get("TWIN_AE_ORG_ID", "6a99bbc5fa6869c45dee52b6")
PROJECT_ID = os.environ.get("TWIN_AE_PROJECT_ID", "6aa4558bb19c96d52970c253")
LANE_HEADER = "X-Mdb-Agent-Engine-Custom-Demo-Prefix"

INVOKE_TIMEOUT_S = 600
BUSY_RETRIES = 6           # 503 POOL_EXHAUSTED: every sandbox held, or the runtime rolling
BUSY_RETRY_S = 10          # the platform's Retry-After
NET_RETRIES, NET_RETRY_S = 2, 10   # the request never reached the platform: safe to repeat


class _CliLogin:
    """The presenter's `agentengine` login."""

    service_account = False
    AUTH_FILE = Path.home() / ".agentengine" / "auth.json"

    async def token(self) -> str:
        creds = json.loads(await asyncio.to_thread(self.AUTH_FILE.read_text))["credentials"]
        return creds[BASE_URL]["access_token"]

    async def refresh(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "agentengine", "status", "--json", "--quiet", "--base-url", BASE_URL,
            "--org-id", ORG_ID, "--project-id", PROJECT_ID,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()


class _ServiceAccount:
    """Client credentials; the token is cached until shortly before it
    expires (an hour)."""

    service_account = True

    def __init__(self, client_id: str, client_secret: str):
        self._auth = (client_id, client_secret)
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if not self._token or time.monotonic() > self._expires:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post(f"{BASE_URL}/api/v1/oauth/token", auth=self._auth,
                                          data={"grant_type": "client_credentials"})
                r.raise_for_status()
                body = r.json()
                self._token = body["access_token"]
                self._expires = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 120)
            return self._token

    async def refresh(self) -> None:
        self._token = None


def _token_source():
    cid, secret = os.environ.get("AE_CLIENT_ID"), os.environ.get("AE_CLIENT_SECRET")
    return _ServiceAccount(cid, secret) if cid and secret else _CliLogin()


credentials = _token_source()


async def api(method: str, path: str, *, timeout: float = 20, **kw) -> httpx.Response:
    """A project API call (`path` below /projects/{id}); a 401 refreshes
    the token and repeats the call once."""
    url = f"{BASE_URL}/api/v1/projects/{PROJECT_ID}{path}"
    headers = kw.pop("headers", {})
    for attempt in range(2):
        token = await credentials.token()
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"},
                                     **kw)
        if r.status_code != 401 or attempt:
            return r
        await credentials.refresh()
    return r


async def invoke(workspace_id: str, *, message: str | None = None, session_id: str | None = None,
                 lane: str = "", user_id: str | None = None, resume_map: dict | None = None,
                 on_retry: Callable[[str], Awaitable[None]] | None = None) -> dict:
    """One invoke: a message, or a resume_map for a paused session. Returns
    the platform's answer (`response`, `execution_id`, `status`,
    `suspend_context`, `error`) plus `session_id` and `ok`."""
    headers = {}
    if session_id:
        headers["X-Session-ID"] = session_id
    if lane:   # the default lane needs no header
        headers[LANE_HEADER] = lane
    body: dict = {"resume_map": resume_map} if resume_map else {"message": message or ""}
    if user_id and credentials.service_account:
        body["user_id"] = user_id
    busy_left, net_left = BUSY_RETRIES, NET_RETRIES
    while True:
        try:
            r = await api("POST", f"/workspaces/{workspace_id}/invoke", headers=headers, json=body,
                          timeout=INVOKE_TIMEOUT_S)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if not net_left:
                return {"ok": False, "error": f"can't reach Agent Engine: {e}"}
            net_left -= 1
            if on_retry:
                await on_retry(f"Can't reach Agent Engine; retrying in {NET_RETRY_S} s")
            await asyncio.sleep(NET_RETRY_S)
            continue
        try:
            out = r.json()
        except ValueError:
            out = {"success": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        if not isinstance(out, dict):
            out = {"success": False, "error": f"HTTP {r.status_code}: unexpected answer"}
        if r.status_code == 503 and busy_left:
            busy_left -= 1
            wait = int(r.headers.get("retry-after") or BUSY_RETRY_S)
            if on_retry:
                await on_retry(f"Agent Engine is at capacity; retrying in {wait} s")
            await asyncio.sleep(wait)
            continue
        out["session_id"] = r.headers.get("x-session-id") or out.get("session_id") or session_id
        out["ok"] = r.status_code == 200 and str(out.get("success")).lower() == "true"
        if not out["ok"] and not out.get("error"):
            out["error"] = f"HTTP {r.status_code}"
        return out


async def execution(execution_id: str, workspace_id: str) -> dict:
    """GET /executions/{id} ({"status": "gone"} for a 404)."""
    r = await api("GET", f"/executions/{execution_id}", params={"workspace_id": workspace_id})
    if r.status_code == 404:
        return {"status": "gone"}
    r.raise_for_status()
    ex = r.json().get("execution")
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except ValueError:
            ex = {}
    return ex if isinstance(ex, dict) else {}
