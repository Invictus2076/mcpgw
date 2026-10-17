"""Streamable HTTP client for one upstream MCP server (modern + legacy eras)."""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
import logging
import time
from typing import Any

import aiohttp

from . import __version__
from .config import GatewayConfig, ServerConfig
from .oauth import AuthManager, AuthRequired
from .protocol import (
    CLIENT_INFO, HEADER_MISMATCH, LATEST_LEGACY, LATEST_MODERN, LEGACY_VERSIONS, META_CLIENT_CAPS,
    META_CLIENT_INFO, META_SERVER_INFO, META_VERSION, MODERN_ERROR_CODES, MODERN_VERSIONS,
    NAME_HEADER_METHODS, UNSUPPORTED_PROTOCOL_VERSION, collect_param_headers, encode_header_value,
    param_header_values,
)
from .providers import Provider

log = logging.getLogger("mcpgw.upstream")

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
_ids = itertools.count(1)


class UpstreamError(Exception):
    """Transport-level failure talking to the upstream."""


class UpstreamRpcError(UpstreamError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"{message} ({code})")
        self.code, self.message, self.data = code, message, data


class Upstream:
    def __init__(self, cfg: ServerConfig, provider: Provider, auth: AuthManager,
                 http: aiohttp.ClientSession, gw: GatewayConfig):
        self.cfg = cfg
        self.provider = provider
        self.auth = auth
        self.http = http
        self.gw = gw
        self.name = cfg.name
        self.url: str = cfg.url or ""
        self.era: str | None = None  # "modern" | "legacy"
        self.version: str | None = None
        self.session_id: str | None = None
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions: str | None = None
        self.status = "unknown"  # unknown | ok | needs_auth | error | misconfigured
        self.last_error: str | None = None
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._tools: list[dict] | None = None
        self._tools_expire = 0.0
        self._param_headers: dict[str, list] = {}
        self._bg: set[asyncio.Task] = set()
        self._warned: set[str] = set()
        if self.problems():
            self.status = "misconfigured"
            self.last_error = self.problems()[0]

    @property
    def display_name(self) -> str:
        return self.cfg.display_name or self.name

    def problems(self) -> list[str]:
        return self.provider.problems(self.cfg, self.gw.redirect_uri)

    def tool_allowed(self, name: str) -> bool:
        if self.cfg.tools_allow and not any(fnmatch.fnmatchcase(name, p) for p in self.cfg.tools_allow):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in self.cfg.tools_deny)

    # -- HTTP plumbing ------------------------------------------------------

    async def _base_headers(self) -> tuple[dict[str, str], str | None]:
        h = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"mcpgw/{__version__}",
        }
        h.update(self.cfg.headers)
        token = None
        if self.cfg.auth == "bearer" and self.cfg.token:
            h["Authorization"] = f"Bearer {self.cfg.token}"
        elif self.cfg.auth == "oauth":
            token = await self.auth.access_token(self)
            if token:
                h["Authorization"] = f"Bearer {token}"
        return h, token

    async def _post(self, msg: dict, headers: dict[str, str]) -> tuple[int, Any, dict | None]:
        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout, sock_connect=15)
        try:
            async with self.http.post(self.url, data=json.dumps(msg), headers=headers,
                                      timeout=timeout, allow_redirects=False) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.status == 202:
                    return 202, r.headers, None
                if ctype == "text/event-stream" and r.status == 200:
                    return r.status, r.headers, await self._read_sse(r, msg.get("id"), headers)
                raw = await r.content.read(MAX_MESSAGE_BYTES + 1)
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise UpstreamError("response too large")
                body = None
                if raw.strip():
                    try:
                        parsed = json.loads(raw)
                        body = parsed if isinstance(parsed, dict) else None
                    except ValueError:
                        body = None
                return r.status, r.headers, body
        except asyncio.TimeoutError as e:
            raise UpstreamError(f"{self.name}: request timed out") from e
        except aiohttp.ClientError as e:
            raise UpstreamError(f"{self.name}: {e}") from e

    async def _read_sse(self, resp: aiohttp.ClientResponse, want_id: Any, headers: dict) -> dict | None:
        """Read an SSE response stream until the JSON-RPC response for ``want_id``."""
        buf = b""
        data_lines: list[bytes] = []
        size = 0

        async def dispatch() -> dict | None:
            nonlocal data_lines, size
            if not data_lines:
                return None
            payload = b"\n".join(data_lines)
            data_lines, size = [], 0
            try:
                msg = json.loads(payload)
            except ValueError:
                log.debug("%s: ignoring non-JSON SSE event", self.name)
                return None
            if not isinstance(msg, dict):
                return None
            if "method" in msg:
                if "id" in msg:  # legacy server-to-client request on the stream
                    task = asyncio.create_task(self._answer_server_request(msg, headers))
                    self._bg.add(task)
                    task.add_done_callback(self._bg.discard)
                else:
                    log.debug("%s: notification %s", self.name, msg.get("method"))
                return None
            if msg.get("id") == want_id and ("result" in msg or "error" in msg):
                return msg
            return None

        async for chunk in resp.content.iter_any():
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line, buf = buf[:nl].rstrip(b"\r"), buf[nl + 1:]
                if not line:
                    found = await dispatch()
                    if found is not None:
                        return found
                elif line.startswith(b":"):
                    continue
                else:
                    field, _, value = line.partition(b":")
                    if value.startswith(b" "):
                        value = value[1:]
                    if field == b"data":
                        size += len(value)
                        if size > MAX_MESSAGE_BYTES:
                            raise UpstreamError("SSE event too large")
                        data_lines.append(value)
            if len(buf) > MAX_MESSAGE_BYTES:
                raise UpstreamError("SSE line too long")
        if buf.strip():
            field, _, value = buf.rstrip(b"\r").partition(b":")
            if field == b"data":
                data_lines.append(value[1:] if value.startswith(b" ") else value)
        found = await dispatch()
        if found is not None:
            return found
        raise UpstreamError(f"{self.name}: event stream ended without a response")

    async def _answer_server_request(self, req: dict, headers: dict) -> None:
        # We declare no client capabilities to legacy servers, so the only request a
        # compliant server can send us is ping.
        if req.get("method") == "ping":
            reply = {"jsonrpc": "2.0", "id": req["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": req["id"],
                     "error": {"code": -32601, "message": "mcpgw does not support this request"}}
        h = {k: v for k, v in headers.items() if not k.lower().startswith("mcp-") or
             k.lower() in ("mcp-session-id", "mcp-protocol-version")}
        try:
            async with self.http.post(self.url, data=json.dumps(reply), headers=h,
                                      timeout=aiohttp.ClientTimeout(total=15)):
                pass
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

    # -- era detection and legacy handshake --------------------------------

    def _modern_headers(self, method: str, params: dict | None) -> dict[str, str]:
        h = {"MCP-Protocol-Version": self.version or LATEST_MODERN, "Mcp-Method": method}
        field = NAME_HEADER_METHODS.get(method)
        if field and params and isinstance(params.get(field), str):
            h["Mcp-Name"] = encode_header_value(params[field])
        return h

    @staticmethod
    def _with_meta(params: dict | None, version: str, caps: dict | None) -> dict:
        p = dict(params or {})
        meta = dict(p.get("_meta") or {})
        meta[META_VERSION] = version
        meta[META_CLIENT_INFO] = CLIENT_INFO
        meta[META_CLIENT_CAPS] = caps or {}
        p["_meta"] = meta
        return p

    async def _exchange(self, msg: dict, extra: dict[str, str]) -> tuple[int, Any, dict | None]:
        """POST with auth, transparently handling 401/403 (refresh + retry)."""
        for _ in range(3):
            headers, token = await self._base_headers()
            headers.update(extra)
            status, rh, body = await self._post(msg, headers)
            if status in (401, 403) and self.cfg.auth == "oauth":
                await self.auth.on_challenge(self, status, rh.get("WWW-Authenticate"), token)
                continue  # refreshed; try again
            if status == 401:
                what = "the configured token" if self.cfg.auth == "bearer" else "unauthenticated requests (auth = none)"
                raise UpstreamError(f"{self.name}: upstream rejected {what} (HTTP 401)")
            return status, rh, body
        raise UpstreamError(f"{self.name}: authorization kept failing")

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            if self.problems():
                raise UpstreamError(self.problems()[0])
            era = None if self.cfg.protocol == "auto" else self.cfg.protocol
            if era in (None, "modern"):
                era = await self._probe_modern()
                if self.cfg.protocol == "modern" and era != "modern":
                    raise UpstreamError(f"{self.name}: server does not speak {LATEST_MODERN}")
            if era == "legacy":
                await self._initialize_legacy()
            self.era = era
            self._ready = True
            log.info("%s: connected (%s, protocol %s)", self.name, era, self.version)

    async def _probe_modern(self) -> str:
        msg = {"jsonrpc": "2.0", "id": next(_ids), "method": "server/discover",
               "params": self._with_meta({}, LATEST_MODERN, {})}
        status, _, body = await self._exchange(msg, self._modern_headers("server/discover", None))
        result = (body or {}).get("result")
        err = (body or {}).get("error") if isinstance((body or {}).get("error"), dict) else None
        if isinstance(result, dict) and isinstance(result.get("supportedVersions"), list):
            supported = result["supportedVersions"]
            if LATEST_MODERN in supported:
                self.version = LATEST_MODERN
                self.capabilities = result.get("capabilities") or {}
                self.server_info = (result.get("_meta") or {}).get(META_SERVER_INFO) or {}
                self.instructions = result.get("instructions")
                return "modern"
            return "legacy"
        if err and err.get("code") in MODERN_ERROR_CODES:
            if err["code"] == UNSUPPORTED_PROTOCOL_VERSION:
                supported = (err.get("data") or {}).get("supported") or []
                if any(v in MODERN_VERSIONS for v in supported):
                    self.version = next(v for v in supported if v in MODERN_VERSIONS)
                    return "modern"
                if any(v in LEGACY_VERSIONS for v in supported):
                    return "legacy"
                raise UpstreamError(f"{self.name}: no mutually supported protocol version ({supported})")
            raise UpstreamError(f"{self.name}: server/discover rejected: {err.get('message')}")
        # Anything else (4xx without a modern error, -32601, junk) means a legacy server.
        log.debug("%s: modern probe got HTTP %s; falling back to initialize", self.name, status)
        return "legacy"

    async def _initialize_legacy(self) -> None:
        self.session_id = None
        msg = {"jsonrpc": "2.0", "id": next(_ids), "method": "initialize",
               "params": {"protocolVersion": LATEST_LEGACY, "capabilities": {}, "clientInfo": CLIENT_INFO}}
        status, rh, body = await self._exchange(msg, {})
        if not body or "result" not in body:
            err = (body or {}).get("error") or {}
            raise UpstreamError(f"{self.name}: initialize failed (HTTP {status}): {err.get('message', '')}")
        res = body["result"]
        version = res.get("protocolVersion")
        if version not in LEGACY_VERSIONS:
            raise UpstreamError(f"{self.name}: unsupported protocol version {version!r}")
        self.version = version
        self.session_id = rh.get("Mcp-Session-Id")
        self.capabilities = res.get("capabilities") or {}
        self.server_info = res.get("serverInfo") or {}
        self.instructions = res.get("instructions")
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        await self._exchange(note, self._legacy_headers())

    def _legacy_headers(self) -> dict[str, str]:
        h = {"MCP-Protocol-Version": self.version or LATEST_LEGACY}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def reset(self) -> None:
        self._ready = False
        self.era = None
        self.session_id = None
        self._tools = None
        self._tools_expire = 0.0

    # -- RPC ----------------------------------------------------------------

    async def rpc(self, method: str, params: dict | None = None, *, caps: dict | None = None,
                  extra_headers: dict[str, str] | None = None) -> dict:
        return await self._tracked(self._rpc(method, params, caps, extra_headers or {}))

    async def ready(self) -> None:
        await self._tracked(self.ensure_ready())

    async def _tracked(self, coro):
        """Run ``coro`` and record the outcome in ``status`` / ``last_error``."""
        try:
            result = await coro
        except AuthRequired as e:
            self.status, self.last_error = "needs_auth", e.reason
            raise
        except UpstreamRpcError:
            self.status, self.last_error = "ok", None  # the server answered; a tool-level problem
            raise
        except UpstreamError as e:
            self.status, self.last_error = "error", str(e)
            raise
        self.status, self.last_error = "ok", None
        return result

    async def _rpc(self, method: str, params: dict | None, caps: dict | None,
                   extra: dict[str, str], attempt: int = 0) -> dict:
        await self.ensure_ready()
        msg_id = next(_ids)
        if self.era == "modern":
            body = {"jsonrpc": "2.0", "id": msg_id, "method": method,
                    "params": self._with_meta(params, self.version or LATEST_MODERN, caps)}
            headers = {**self._modern_headers(method, params), **extra}
        else:
            body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params:
                body["params"] = params
            headers = self._legacy_headers()
        status, _, reply = await self._exchange(body, headers)

        if self.era == "legacy" and status == 404 and self.session_id and attempt == 0:
            log.info("%s: session expired; re-initializing", self.name)
            self.reset()
            return await self._rpc(method, params, caps, extra, attempt + 1)
        if reply is None or ("result" not in reply and "error" not in reply):
            if attempt == 0 and status in (400, 404, 405):
                self.reset()  # server may have changed era; re-probe once
                return await self._rpc(method, params, caps, extra, attempt + 1)
            raise UpstreamError(f"{self.name}: HTTP {status} without a JSON-RPC response")
        if "error" in reply:
            err = reply["error"] if isinstance(reply["error"], dict) else {}
            code = err.get("code", -32603)
            if self.era == "modern" and attempt == 0:
                if code == UNSUPPORTED_PROTOCOL_VERSION:
                    self.reset()
                    return await self._rpc(method, params, caps, extra, attempt + 1)
                if code == HEADER_MISMATCH and method == "tools/call":
                    await self.list_tools(force=True)  # schema (x-mcp-header) may have changed
                    name = (params or {}).get("name", "")
                    extra = {**extra, **param_header_values(self._param_headers.get(name, []),
                                                            (params or {}).get("arguments"))}
                    return await self._rpc(method, params, caps, extra, attempt + 1)
            raise UpstreamRpcError(code, err.get("message", "upstream error"), err.get("data"))
        result = reply["result"]
        if not isinstance(result, dict):
            raise UpstreamError(f"{self.name}: malformed result")
        return result

    # -- tools --------------------------------------------------------------

    def cached_tools(self) -> list[dict] | None:
        return self._tools

    async def list_tools(self, *, force: bool = False) -> list[dict]:
        if self._tools is not None and not force and time.time() < self._tools_expire:
            return self._tools
        await self.ready()
        if self.capabilities and "tools" not in self.capabilities:
            self._tools, self._param_headers, self._tools_expire = [], {}, time.time() + self.gw.tool_list_ttl
            return []
        tools: list[dict] = []
        cursor = None
        ttl_ms = None
        for _ in range(100):
            result = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools") or []
            tools.extend(t for t in page if isinstance(t, dict) and isinstance(t.get("name"), str))
            if isinstance(result.get("ttlMs"), (int, float)):
                ttl_ms = result["ttlMs"] if ttl_ms is None else min(ttl_ms, result["ttlMs"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        kept, specs = [], {}
        for t in tools:
            if not self.tool_allowed(t["name"]):
                continue
            try:
                specs[t["name"]] = collect_param_headers(t.get("inputSchema") or {})
            except ValueError as e:
                if t["name"] not in self._warned:
                    self._warned.add(t["name"])
                    log.warning("%s: dropping tool %r: %s", self.name, t["name"], e)
                continue
            kept.append(t)
        self._tools, self._param_headers = kept, specs
        ttl = ttl_ms / 1000.0 if ttl_ms is not None else self.gw.tool_list_ttl
        self._tools_expire = time.time() + max(0.0, min(ttl, 3600.0))
        return kept

    async def call_tool(self, name: str, arguments: dict | None, *, caps: dict | None = None,
                        input_responses: dict | None = None, request_state: str | None = None) -> dict:
        if not self.tool_allowed(name):
            raise UpstreamRpcError(-32602, f"Unknown tool: {name}")
        await self.ready()
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        extra: dict[str, str] = {}
        if self.era == "modern":
            if input_responses is not None:
                params["inputResponses"] = input_responses
            if request_state is not None:
                params["requestState"] = request_state
            if self._tools is None:
                await self.list_tools()
            extra = param_header_values(self._param_headers.get(name, []), params["arguments"])
        return await self.rpc("tools/call", params, caps=caps, extra_headers=extra)

    async def close(self) -> None:
        if self.era == "legacy" and self.session_id:
            try:
                headers, _ = await self._base_headers()
                headers.update(self._legacy_headers())
                async with self.http.delete(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)):
                    pass
            except Exception:  # best effort
                pass
