"""HTTP surface: the MCP endpoint, browser OAuth pages, and a small REST API."""

from __future__ import annotations

import html
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .config import is_loopback_host
from .gateway import CallContext, ConnectRequired, Gateway
from .oauth import OAuthError
from .protocol import (
    ALL_VERSIONS, HEADER_MISMATCH, INVALID_REQUEST, LATEST_LEGACY, LEGACY_VERSIONS, META_CLIENT_CAPS,
    META_SERVER_INFO, META_VERSION, METHOD_NOT_FOUND, MODERN_VERSIONS, NAME_HEADER_METHODS, PARSE_ERROR,
    SERVER_INFO, UNSUPPORTED_PROTOCOL_VERSION, RpcError, decode_header_value, rpc_error, rpc_result,
)

log = logging.getLogger("mcpgw.server")

GW_KEY = web.AppKey("gateway", Gateway)
SESSIONS_KEY = web.AppKey("sessions", dict)
ORIGINS_KEY = web.AppKey("allowed_origins", set)
MAX_SESSIONS = 1000
SESSION_IDLE = 24 * 3600
INSTRUCTIONS = ("Tools are namespaced as <server>{sep}<tool>. If a server isn't connected yet, "
                "call gateway{sep}connect_<server> (or follow the link the gateway returns) and retry.")


@dataclass
class Session:
    version: str
    caps: dict
    client_info: dict
    scope: str | None
    last_seen: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# security middleware: Origin validation + gateway API key
# ---------------------------------------------------------------------------

def _allowed_origins(gw: Gateway) -> set[str]:
    cfg = gw.cfg
    allowed = {cfg.public_origin.lower(), *(o.rstrip("/").lower() for o in cfg.allowed_origins)}
    pu = urlsplit(cfg.public_url)
    if is_loopback_host(pu.hostname or ""):
        port = f":{pu.port}" if pu.port else ""
        allowed |= {f"{pu.scheme}://localhost{port}", f"{pu.scheme}://127.0.0.1{port}"}
    return allowed


@web.middleware
async def security(request: web.Request, handler):
    gw: Gateway = request.app[GW_KEY]
    path = request.path
    if path.startswith("/mcp") or path.startswith("/api"):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/").lower() not in request.app[ORIGINS_KEY]:
            return web.json_response(rpc_error(None, INVALID_REQUEST, "Origin not allowed"), status=403)
        key = gw.cfg.api_key
        if key:
            presented = request.headers.get("X-API-Key")
            auth = request.headers.get("Authorization", "")
            if not presented and auth[:7].lower() == "bearer ":
                presented = auth[7:].strip()
            if not presented or not hmac.compare_digest(presented.encode(), key.encode()):
                return web.json_response({"error": "unauthorized"}, status=401,
                                         headers={"WWW-Authenticate": 'Bearer realm="mcpgw"'})
    return await handler(request)


# ---------------------------------------------------------------------------
# MCP endpoint
# ---------------------------------------------------------------------------

def _modern_meta(result: dict) -> dict:
    out = dict(result)
    out.setdefault("resultType", "complete")
    meta = dict(out.get("_meta") or {})
    meta.setdefault(META_SERVER_INFO, SERVER_INFO)
    out["_meta"] = meta
    return out


def _legacy_clean(result: dict) -> dict:
    out = dict(result)
    if out.get("resultType") == "complete":
        del out["resultType"]
    return out


def _validate_modern_headers(request: web.Request, method: str, params: dict) -> None:
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    hv = request.headers.get("MCP-Protocol-Version")
    bv = meta.get(META_VERSION)
    if hv is None or bv is None or hv != bv:
        raise RpcError(HEADER_MISMATCH, f"Header mismatch: MCP-Protocol-Version {hv!r} vs _meta {bv!r}",
                       http_status=400)
    if bv not in MODERN_VERSIONS:
        raise RpcError(UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                       {"supported": list(ALL_VERSIONS), "requested": bv}, http_status=400)
    hm = request.headers.get("Mcp-Method")
    if hm != method:
        raise RpcError(HEADER_MISMATCH, f"Header mismatch: Mcp-Method {hm!r} vs body {method!r}",
                       http_status=400)
    field_name = NAME_HEADER_METHODS.get(method)
    if field_name:
        raw = request.headers.get("Mcp-Name")
        try:
            hn = decode_header_value(raw) if raw is not None else None
        except (ValueError, UnicodeDecodeError):
            hn = None
        if hn is None or hn != params.get(field_name):
            raise RpcError(HEADER_MISMATCH, f"Header mismatch: Mcp-Name does not match params.{field_name}",
                           http_status=400)


async def handle_mcp(request: web.Request) -> web.StreamResponse:
    gw: Gateway = request.app[GW_KEY]
    scope = request.match_info.get("server")
    if scope and scope not in gw.upstreams:
        return web.json_response({"error": f"unknown server {scope}"}, status=404)
    try:
        msg = await request.json(loads=json.loads)
    except (ValueError, UnicodeDecodeError):
        return web.json_response(rpc_error(None, PARSE_ERROR, "Parse error"), status=400)
    if isinstance(msg, list):
        return web.json_response(rpc_error(None, INVALID_REQUEST, "batching is not supported"), status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return web.json_response(rpc_error(None, INVALID_REQUEST, "Invalid Request"), status=400)
    if "method" not in msg:  # a response to a server->client request; we never send any
        return web.Response(status=202)

    method = msg["method"]
    req_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    is_notification = "id" not in msg
    sessions: dict[str, Session] = request.app[SESSIONS_KEY]
    sid = request.headers.get("Mcp-Session-Id")
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    header_version = request.headers.get("MCP-Protocol-Version")
    modern = META_VERSION in meta or (header_version is not None and header_version not in LEGACY_VERSIONS
                                      and not sid and method != "initialize")

    if is_notification:
        return web.Response(status=202)

    # ---- legacy: initialize --------------------------------------------
    if method == "initialize" and not modern:
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LATEST_LEGACY
        now = time.time()
        for k in [k for k, s in sessions.items() if now - s.last_seen > SESSION_IDLE]:
            del sessions[k]
        if len(sessions) >= MAX_SESSIONS:
            oldest = min(sessions, key=lambda k: sessions[k].last_seen)
            del sessions[oldest]
        new_sid = secrets.token_urlsafe(24)
        sessions[new_sid] = Session(version, params.get("capabilities") or {},
                                    params.get("clientInfo") or {}, scope)
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS.format(sep=gw.sep),
        }
        return web.json_response(rpc_result(req_id, result), headers={"Mcp-Session-Id": new_sid})

    try:
        if modern:
            _validate_modern_headers(request, method, params)
            ctx = CallContext("modern", meta.get(META_CLIENT_CAPS) or {}, scope=scope)
        else:
            session = sessions.get(sid) if sid else None
            if not sid:
                raise RpcError(INVALID_REQUEST, "Bad Request: missing Mcp-Session-Id (send initialize first)",
                               http_status=400)
            if session is None or session.scope != scope:
                return web.json_response(rpc_error(req_id, INVALID_REQUEST, "Session not found"), status=404)
            if header_version and header_version != session.version:
                raise RpcError(INVALID_REQUEST, "MCP-Protocol-Version does not match the session",
                               http_status=400)
            session.last_seen = time.time()
            ctx = CallContext("legacy", session.caps, scope=scope)
        result = await _dispatch(gw, method, params, ctx)
    except RpcError as e:
        return web.json_response(rpc_error(req_id, e.code, e.message, e.data), status=e.http_status)
    except Exception:  # never leak a traceback to clients
        log.exception("internal error handling %s", method)
        return web.json_response(rpc_error(req_id, -32603, "Internal error"), status=500)

    result = _modern_meta(result) if modern else _legacy_clean(result)
    return web.json_response(rpc_result(req_id, result))


async def _dispatch(gw: Gateway, method: str, params: dict, ctx: CallContext) -> dict:
    modern = ctx.era == "modern"
    if method == "server/discover" and modern:
        return {"supportedVersions": list(ALL_VERSIONS),
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": INSTRUCTIONS.format(sep=gw.sep), "ttlMs": 60_000, "cacheScope": "private"}
    if method == "ping" and not modern:
        return {}
    if method == "tools/list":
        tools, ttl_ms = await gw.list_tools(ctx.scope)
        out: dict = {"tools": tools}
        if modern:
            out.update(ttlMs=ttl_ms, cacheScope="private")
        return out
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(-32602, "params.name is required")
        if modern:
            ctx.input_responses = params.get("inputResponses") if isinstance(params.get("inputResponses"), dict) else None
            rs = params.get("requestState")
            ctx.request_state = rs if isinstance(rs, str) else None
        return await gw.call_tool(name, params.get("arguments"), ctx)
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}", http_status=404 if modern else 200)


async def handle_mcp_other(request: web.Request) -> web.Response:
    if request.method == "DELETE":
        sid = request.headers.get("Mcp-Session-Id")
        if sid and request.app[SESSIONS_KEY].pop(sid, None):
            return web.Response(status=204)
        return web.Response(status=404)
    return web.Response(status=405, headers={"Allow": "POST, DELETE"})


# ---------------------------------------------------------------------------
# Browser pages: connect confirmation, OAuth callback, CIMD document
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<meta name="referrer" content="no-referrer">
<style>
:root{{--bg:#f6f5f2;--fg:#1d1d1b;--muted:#6b6a65;--card:#fff;--line:#e3e1db;--accent:#2f5d50}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#a3a19a;--card:#1f1f1d;--line:#33322f;--accent:#7fb8a6}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:30rem;margin:12vh auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}}
h1{{font-size:1.25rem;margin:0 0 .5rem}} p{{color:var(--muted);margin:.5rem 0 1.25rem}}
code{{font-size:.9em}} button{{font:inherit;padding:.6rem 1.1rem;border-radius:8px;border:0;
background:var(--accent);color:var(--card);cursor:pointer}}
</style></head><body><main><div class="card"><h1>{title}</h1>{body}</div></main></body></html>"""


def _page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(text=_PAGE.format(title=html.escape(title), body=body), status=status,
                        content_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*"})


async def handle_connect_page(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    server = request.match_info["server"]
    up = gw.upstreams.get(server)
    ticket = request.query.get("t", "")
    if not up or not gw.check_ticket(server, ticket, consume=False):
        return _page("Link expired", "<p>This connect link is invalid or has expired. "
                     "Ask the assistant to retry, or run <code>mcpgw login "
                     f"{html.escape(server)}</code>.</p>", 400)
    name = html.escape(up.display_name)
    body = (f"<p>Sign in to <strong>{name}</strong> and allow this gateway to use it on your behalf. "
            f"Tokens stay on the gateway at <code>{html.escape(gw.cfg.public_origin)}</code>.</p>"
            f'<form method="post"><input type="hidden" name="t" value="{html.escape(ticket)}">'
            f"<button type=\"submit\">Continue to {name}</button></form>")
    return _page(f"Connect {up.display_name}", body)


async def handle_connect_start(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    server = request.match_info["server"]
    up = gw.upstreams.get(server)
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() not in request.app[ORIGINS_KEY]:
        return _page("Not allowed", "<p>Cross-site request rejected.</p>", 403)
    form = await request.post()
    payload = gw.check_ticket(server, str(form.get("t", "")), consume=True) if up else None
    if not payload:
        return _page("Link expired", "<p>This connect link was already used or has expired.</p>", 400)
    try:
        url = await gw.begin_connect(server, bool(payload.get("u")))
    except OAuthError as e:
        return _page("Can't connect", f"<p>{html.escape(str(e))}</p>", 502)
    raise web.HTTPFound(url)


async def handle_callback(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    try:
        server = await gw.finish_connect(dict(request.query))
    except OAuthError as e:
        return _page("Connection failed", f"<p>{html.escape(str(e))}</p>", 400)
    name = html.escape(gw.upstreams[server].display_name if server in gw.upstreams else server)
    return _page(f"{name} connected", "<p>You can close this tab and go back to your assistant.</p>")


async def handle_cimd(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    if not gw.cfg.cimd_url or not gw.auth:
        raise web.HTTPNotFound()
    return web.json_response(gw.auth.cimd_document(), headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

async def api_servers(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    if request.query.get("probe") in ("1", "true"):
        await gw.list_tools()  # touch every upstream so status reflects reality
    return web.json_response({"servers": await gw.status()})


async def api_connect(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    server = request.match_info["server"]
    if server not in gw.upstreams:
        return web.json_response({"error": "unknown server"}, status=404)
    up = gw.upstreams[server]
    if up.problems():
        return web.json_response({"error": up.problems()[0]}, status=400)
    return web.json_response({"connect_url": gw.connect_url(server)})


async def api_disconnect(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    server = request.match_info["server"]
    if server not in gw.upstreams:
        return web.json_response({"error": "unknown server"}, status=404)
    await gw.disconnect(server)
    return web.json_response({"ok": True})


async def api_tools(request: web.Request) -> web.Response:
    tools, _ = await request.app[GW_KEY].list_tools()
    return web.json_response({"tools": tools})


async def api_call(request: web.Request) -> web.Response:
    gw: Gateway = request.app[GW_KEY]
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return web.json_response({"error": "expected {\"name\": ..., \"arguments\": {...}}"}, status=400)
    try:
        result = await gw.call_tool(body["name"], body.get("arguments"), CallContext("rest"))
    except ConnectRequired as e:
        return web.json_response({"error": "connect_required", "server": e.server,
                                  "message": e.message, "connect_url": e.url}, status=409)
    except RpcError as e:
        return web.json_response({"error": e.message, "code": e.code, "data": e.data},
                                 status=404 if e.code == -32602 else 400)
    return web.json_response({"result": result})


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def not_found(request: web.Request) -> web.Response:
    raise web.HTTPNotFound()


def create_app(gw: Gateway) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=8 * 1024 * 1024)
    app[GW_KEY] = gw
    app[SESSIONS_KEY] = {}
    app[ORIGINS_KEY] = _allowed_origins(gw)

    async def on_startup(app: web.Application) -> None:
        await gw.start()

    async def on_cleanup(app: web.Application) -> None:
        await gw.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    r = app.router
    for path in ("/mcp", "/mcp/{server}"):
        r.add_post(path, handle_mcp)
        r.add_route("GET", path, handle_mcp_other)
        r.add_route("DELETE", path, handle_mcp_other)
    r.add_get("/connect/{server}", handle_connect_page)
    r.add_post("/connect/{server}", handle_connect_start)
    r.add_get("/oauth/callback", handle_callback)
    r.add_get("/oauth/client-metadata.json", handle_cimd)
    r.add_get("/api/servers", api_servers)
    r.add_post("/api/servers/{server}/connect", api_connect)
    r.add_post("/api/servers/{server}/disconnect", api_disconnect)
    r.add_get("/api/tools", api_tools)
    r.add_post("/api/tools/call", api_call)
    r.add_get("/healthz", healthz)
    # Make OAuth-aware MCP clients fail fast instead of probing for gateway OAuth.
    r.add_get("/.well-known/{tail:.*}", not_found)
    return app
