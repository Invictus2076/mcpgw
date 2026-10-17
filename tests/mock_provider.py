"""A strict, in-process mock of a hosted MCP provider: OAuth 2.1 AS + MCP resource server.

It rejects anything the MCP spec forbids (missing PKCE, wrong resource, header/body
mismatches, missing sessions ...) so tests prove the gateway's compliance rather
than just its happy path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _decode_header(v: str) -> str:
    if v.startswith("=?base64?") and v.endswith("?="):
        return base64.b64decode(v[9:-2]).decode()
    return v


TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two integers",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}},
    {"name": "secret_write", "description": "Needs the write scope",
     "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}},
    {"name": "region_query", "description": "Routed by region header",
     "inputSchema": {"type": "object", "properties": {
         "region": {"type": "string", "x-mcp-header": "Region"},
         "q": {"type": "string"}}, "required": ["region"]}},
    {"name": "bad_header_tool", "description": "Invalid x-mcp-header placement (inside items)",
     "inputSchema": {"type": "object", "properties": {
         "xs": {"type": "array", "items": {"type": "string", "x-mcp-header": "X"}}}}},
    {"name": "ask_name", "description": "Asks for your name via elicitation (modern MRTR)",
     "inputSchema": {"type": "object"}},
]


@dataclass
class MockProvider:
    era: str = "modern"  # modern | legacy | dual
    registration: str = "dcr"  # dcr | cimd | preregistered
    as_path: str = "/as"
    discovery: str = "oauth"  # oauth | oidc  (oidc = path-appended openid-configuration)
    iss_param: bool = True
    iss_supported: bool = True
    iss_override: str | None = None
    metadata_issuer_override: str | None = None
    pkce_advertised: bool = True
    token_ttl: int = 3600
    sse: bool = False
    prereg_client_id: str = "prereg-client"
    prereg_client_secret: str | None = "prereg-secret"
    prereg_auth_method: str = "client_secret_basic"
    cimd_client_id: str | None = None
    base_scopes: list[str] = field(default_factory=lambda: ["read"])
    write_scope: str = "write"

    def __post_init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}
        self.log: list[dict] = []
        self.authorize_requests: list[dict] = []
        self.registrations: list[dict] = []
        self.ping_answers: list[dict] = []
        self.refresh_count = 0
        self.base = ""
        self._runner: web.AppRunner | None = None
        if self.registration == "preregistered":
            self.clients[self.prereg_client_id] = {
                "redirect_uris": None,  # accept any loopback redirect for the test
                "secret": self.prereg_client_secret, "method": self.prereg_auth_method,
            }

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> "MockProvider":
        app = web.Application()
        app.router.add_get("/.well-known/oauth-protected-resource/mcp", self.prm)
        if self.discovery == "oauth":
            app.router.add_get(f"/.well-known/oauth-authorization-server{self.as_path}", self.as_metadata)
        else:
            app.router.add_get(f"{self.as_path}/.well-known/openid-configuration", self.as_metadata)
        app.router.add_post("/register", self.register)
        app.router.add_get("/authorize", self.authorize)
        app.router.add_post("/token", self.token)
        app.router.add_post("/mcp", self.mcp)
        app.router.add_delete("/mcp", self.mcp_delete)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"{self.base}/mcp"

    @property
    def issuer(self) -> str:
        return self.base + self.as_path

    @property
    def prm_url(self) -> str:
        return f"{self.base}/.well-known/oauth-protected-resource/mcp"

    def revoke_access(self) -> None:
        self.access.clear()

    def revoke_refresh(self) -> None:
        self.refresh.clear()

    # -- OAuth metadata -----------------------------------------------------

    async def prm(self, request: web.Request) -> web.Response:
        return web.json_response({"resource": self.url, "authorization_servers": [self.issuer],
                                  "scopes_supported": self.base_scopes, "bearer_methods_supported": ["header"]})

    async def as_metadata(self, request: web.Request) -> web.Response:
        meta = {
            "issuer": self.metadata_issuer_override or self.issuer,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "scopes_supported": self.base_scopes + [self.write_scope, "offline_access"],
            "authorization_response_iss_parameter_supported": self.iss_supported,
        }
        if self.pkce_advertised:
            meta["code_challenge_methods_supported"] = ["S256"]
        if self.registration == "dcr":
            meta["registration_endpoint"] = f"{self.base}/register"
        if self.registration == "cimd":
            meta["client_id_metadata_document_supported"] = True
        return web.json_response(meta)

    async def register(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.registrations.append(body)
        if body.get("application_type") not in ("native", "web"):
            return web.json_response({"error": "invalid_client_metadata",
                                      "error_description": "application_type required"}, status=400)
        if not body.get("redirect_uris"):
            return web.json_response({"error": "invalid_redirect_uri"}, status=400)
        cid = "dcr-" + secrets.token_hex(6)
        self.clients[cid] = {"redirect_uris": body["redirect_uris"], "secret": None, "method": "none"}
        return web.json_response({"client_id": cid, **body}, status=201)

    # -- authorization endpoint --------------------------------------------

    def _err(self, msg: str) -> web.Response:
        return web.Response(status=400, text=f"authorize error: {msg}")

    async def authorize(self, request: web.Request) -> web.Response:
        q = dict(request.query)
        self.authorize_requests.append(q)
        if q.get("response_type") != "code":
            return self._err("response_type")
        cid = q.get("client_id", "")
        redirect = q.get("redirect_uri", "")
        if self.registration == "cimd":
            if cid != self.cimd_client_id:
                return self._err("unexpected CIMD client_id")
            # A real AS would fetch the document; here it's served by the gateway on loopback.
            doc_url = request.app.get("cimd_fetch_url") or self._cimd_fetch_url
            async with aiohttp.ClientSession() as s, s.get(doc_url) as r:
                doc = await r.json()
            if doc.get("client_id") != cid or redirect not in doc.get("redirect_uris", []):
                return self._err("CIMD document mismatch")
        else:
            client = self.clients.get(cid)
            if not client:
                return self._err("unknown client")
            if client["redirect_uris"] is not None and redirect not in client["redirect_uris"]:
                return self._err("redirect_uri not registered")
        if q.get("code_challenge_method") != "S256" or len(q.get("code_challenge", "")) < 43:
            return self._err("PKCE S256 required")
        if q.get("resource") != self.url:
            return self._err(f"resource must be {self.url}, got {q.get('resource')}")
        if not q.get("state"):
            return self._err("state required")
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"client_id": cid, "redirect_uri": redirect, "challenge": q["code_challenge"],
                            "resource": q["resource"], "scope": q.get("scope", " ".join(self.base_scopes))}
        params = {"code": code, "state": q["state"]}
        if self.iss_param:
            params["iss"] = self.iss_override or self.issuer
        sep = "&" if "?" in redirect else "?"
        raise web.HTTPFound(redirect + sep + urlencode(params))

    # -- token endpoint -----------------------------------------------------

    def _client_auth(self, request: web.Request, form: dict) -> str | None:
        auth = request.headers.get("Authorization", "")
        cid, secret = form.get("client_id"), form.get("client_secret")
        if auth.startswith("Basic "):
            from urllib.parse import unquote
            raw = base64.b64decode(auth[6:]).decode()
            cid, _, secret = raw.partition(":")
            cid, secret = unquote(cid), unquote(secret)
            method = "client_secret_basic"
        else:
            method = "client_secret_post" if secret else "none"
        if self.registration == "cimd":
            return cid if cid == self.cimd_client_id and method == "none" else None
        client = self.clients.get(cid or "")
        if not client:
            return None
        if client["secret"]:
            if method != client["method"] or secret != client["secret"]:
                return None
        return cid

    async def token(self, request: web.Request) -> web.Response:
        form = {k: v for k, v in (await request.post()).items()}
        cid = self._client_auth(request, form)
        if not cid:
            return web.json_response({"error": "invalid_client"}, status=401)
        gt = form.get("grant_type")
        if gt == "authorization_code":
            c = self.codes.pop(form.get("code", ""), None)
            if not c or c["client_id"] != cid or c["redirect_uri"] != form.get("redirect_uri"):
                return web.json_response({"error": "invalid_grant"}, status=400)
            v = form.get("code_verifier", "")
            if _b64(hashlib.sha256(v.encode()).digest()) != c["challenge"]:
                return web.json_response({"error": "invalid_grant", "error_description": "PKCE"}, status=400)
            if form.get("resource") != c["resource"]:
                return web.json_response({"error": "invalid_target"}, status=400)
            return self._issue(cid, c["scope"], c["resource"], family=secrets.token_hex(4))
        if gt == "refresh_token":
            rt = self.refresh.pop(form.get("refresh_token", ""), None)
            if not rt or rt["client_id"] != cid:
                return web.json_response({"error": "invalid_grant"}, status=400)
            if form.get("resource") and form["resource"] != rt["resource"]:
                return web.json_response({"error": "invalid_target"}, status=400)
            self.refresh_count += 1
            return self._issue(cid, rt["scope"], rt["resource"], family=rt["family"])
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    def _issue(self, cid: str, scope: str, resource: str, family: str) -> web.Response:
        at, rt = "at-" + secrets.token_hex(12), "rt-" + secrets.token_hex(12)
        self.access[at] = {"client_id": cid, "scope": scope.split(), "aud": resource,
                           "exp": time.time() + self.token_ttl}
        self.refresh[rt] = {"client_id": cid, "scope": scope, "resource": resource, "family": family}
        return web.json_response({"access_token": at, "token_type": "Bearer", "expires_in": self.token_ttl,
                                  "refresh_token": rt, "scope": scope})

    # -- MCP resource server ------------------------------------------------

    def _challenge(self, status: int = 401, scope: str | None = None, error: str | None = None) -> web.Response:
        parts = [f'resource_metadata="{self.prm_url}"']
        parts.append(f'scope="{scope or " ".join(self.base_scopes)}"')
        if error:
            parts.insert(0, f'error="{error}"')
        return web.json_response({"error": "unauthorized"}, status=status,
                                 headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)})

    def _token_info(self, request: web.Request) -> dict | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        info = self.access.get(auth[7:])
        if not info or info["exp"] < time.time() or info["aud"] != self.url:
            return None
        return info

    @staticmethod
    def _rpc_err(rid, code, msg, status=200, data=None):
        err = {"code": code, "message": msg}
        if data is not None:
            err["data"] = data
        body = {"jsonrpc": "2.0", "error": err}
        if rid is not None:
            body["id"] = rid
        return web.json_response(body, status=status)

    async def mcp(self, request: web.Request) -> web.StreamResponse:
        msg = await request.json()
        if "method" not in msg:  # a client's answer to our ping
            self.ping_answers.append(msg)
            return web.Response(status=202)
        info = self._token_info(request)
        if info is None:
            return self._challenge()
        method, rid = msg["method"], msg.get("id")
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        self.log.append({"method": method, "headers": dict(request.headers), "params": params})
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return self._rpc_err(rid, -32600, "Accept must list application/json and text/event-stream", 406)

        is_modern = "io.modelcontextprotocol/protocolVersion" in meta
        if is_modern and self.era in ("modern", "dual"):
            return await self._modern(request, msg, info)
        if self.era == "modern":
            if method == "initialize":
                return self._rpc_err(rid, -32601, f"initialize not supported; use {MODERN}", 400)
            return self._rpc_err(rid, -32020, "missing request metadata", 400)
        return await self._legacy(request, msg, info)

    async def _modern(self, request: web.Request, msg: dict, info: dict) -> web.StreamResponse:
        method, rid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        meta = params["_meta"]
        hv = request.headers.get("MCP-Protocol-Version")
        if hv != meta["io.modelcontextprotocol/protocolVersion"]:
            return self._rpc_err(rid, -32020, "protocol version header mismatch", 400)
        if hv != MODERN:
            return self._rpc_err(rid, -32022, "Unsupported protocol version", 400,
                                 {"supported": [MODERN] + (list(LEGACY) if self.era == "dual" else []),
                                  "requested": hv})
        if request.headers.get("Mcp-Method") != method:
            return self._rpc_err(rid, -32020, "Mcp-Method mismatch", 400)
        if method == "tools/call":
            if _decode_header(request.headers.get("Mcp-Name", "")) != params.get("name"):
                return self._rpc_err(rid, -32020, "Mcp-Name mismatch", 400)
        if "id" not in msg:
            return web.Response(status=202)
        caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        if method == "server/discover":
            return self._ok(rid, {"resultType": "complete", "supportedVersions": [MODERN],
                                  "capabilities": {"tools": {}},
                                  "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "mock", "version": "1"}},
                                  "ttlMs": 60000, "cacheScope": "private"})
        if method == "tools/list":
            return self._ok(rid, {"resultType": "complete", "tools": TOOLS, "ttlMs": 60000, "cacheScope": "private"})
        if method == "tools/call":
            if params.get("name") == "region_query":
                region = (params.get("arguments") or {}).get("region")
                hdr = request.headers.get("Mcp-Param-Region")
                if hdr is None or _decode_header(hdr) != region:
                    return self._rpc_err(rid, -32020, "Mcp-Param-Region mismatch", 400)
            if params.get("name") == "ask_name":
                responses = params.get("inputResponses")
                if not responses:
                    if "elicitation" not in caps:
                        return self._ok(rid, {"resultType": "complete", "isError": True,
                                              "content": [{"type": "text", "text": "need elicitation"}]})
                    return self._ok(rid, {"resultType": "input_required", "requestState": "mock-state-1",
                                          "inputRequests": {"name": {"method": "elicitation/create", "params": {
                                              "mode": "form", "message": "Your name?",
                                              "requestedSchema": {"type": "object",
                                                                  "properties": {"name": {"type": "string"}}}}}}})
                if params.get("requestState") != "mock-state-1":
                    return self._rpc_err(rid, -32602, "bad requestState", 400)
                name = responses["name"]["content"]["name"]
                return self._ok(rid, {"resultType": "complete", "content": [{"type": "text", "text": f"Hello {name}"}]})
            return await self._call(request, rid, params, info, modern=True)
        return self._rpc_err(rid, -32601, "Method not found", 404)

    async def _legacy(self, request: web.Request, msg: dict, info: dict) -> web.StreamResponse:
        method, rid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            sid = secrets.token_hex(8)
            version = params.get("protocolVersion") if params.get("protocolVersion") in LEGACY else LEGACY[0]
            self.sessions[sid] = version
            return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "mock-legacy", "version": "1"}}}, headers={"Mcp-Session-Id": sid})
        sid = request.headers.get("Mcp-Session-Id")
        if not sid:
            return self._rpc_err(rid, -32000, "Bad Request: No valid session ID provided", 400)
        if sid not in self.sessions:
            return self._rpc_err(rid, -32001, "Session not found", 404)
        if request.headers.get("MCP-Protocol-Version") != self.sessions[sid]:
            return self._rpc_err(rid, -32000, "Bad MCP-Protocol-Version", 400)
        if "id" not in msg:
            return web.Response(status=202)
        if method == "tools/list":
            tools = [t for t in TOOLS if t["name"] != "ask_name"]
            return await self._reply(request, rid, {"tools": tools})
        if method == "tools/call":
            return await self._call(request, rid, params, info, modern=False)
        if method == "ping":
            return await self._reply(request, rid, {})
        return self._rpc_err(rid, -32601, "Method not found")

    async def _call(self, request, rid, params, info, *, modern: bool) -> web.StreamResponse:
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "secret_write" and self.write_scope not in info["scope"]:
            return self._challenge(403, scope=self.write_scope, error="insufficient_scope")
        if name == "echo":
            result = {"content": [{"type": "text", "text": args.get("text", "")}]}
        elif name == "add":
            total = int(args.get("a", 0)) + int(args.get("b", 0))
            result = {"content": [{"type": "text", "text": str(total)}], "structuredContent": {"sum": total}}
        elif name == "secret_write":
            result = {"content": [{"type": "text", "text": f"wrote {args.get('value')}"}]}
        elif name == "region_query":
            result = {"content": [{"type": "text", "text": f"{args.get('region')}: {args.get('q', '')}"}]}
        else:
            return self._rpc_err(rid, -32602, f"Unknown tool: {name}", 404 if modern else 200)
        if modern:
            result["resultType"] = "complete"
            return self._ok(rid, result)
        return await self._reply(request, rid, result)

    def _ok(self, rid, result) -> web.Response:
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _reply(self, request: web.Request, rid, result: dict) -> web.StreamResponse:
        if not self.sse:
            return self._ok(rid, result)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        await resp.write(b": keep-alive comment\r\n\r\n")
        ping = {"jsonrpc": "2.0", "id": "srv-ping-1", "method": "ping"}
        await resp.write(f"event: message\ndata: {json.dumps(ping)}\n\n".encode())
        prog = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
        await resp.write(f"data: {json.dumps(prog)}\n\n".encode())
        # split one JSON payload across two data lines and two TCP writes
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, indent=1)
        lines = payload.split("\n")
        half = len(lines) // 2
        await resp.write(("".join(f"data: {ln}\n" for ln in lines[:half])).encode())
        await resp.write(("".join(f"data: {ln}\n" for ln in lines[half:]) + "\n").encode())
        await resp.write_eof()
        return resp

    async def mcp_delete(self, request: web.Request) -> web.Response:
        self.sessions.pop(request.headers.get("Mcp-Session-Id", ""), None)
        return web.Response(status=204)

    _cimd_fetch_url: str = ""
