"""The aggregator: one tool namespace over many upstream MCP servers."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

import aiohttp

from .config import GatewayConfig
from .oauth import AuthManager, AuthRequired, OAuthError
from .protocol import (
    INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST, RESOURCE_NOT_FOUND, URL_ELICITATION_REQUIRED, RpcError,
    strip_param_headers,
)
from .providers import get_provider, load_plugins
from .store import FileStore, TokenStore, load_or_create_secret
from .upstream import Upstream, UpstreamError, UpstreamRpcError
from .util import Signer, digest

log = logging.getLogger("mcpgw")

TICKET_PREFIX = "ct1."
STATE_PREFIX = "gw1."
UPSTREAM_STATE_PREFIX = "up."
CONNECT_TOOL = "connect_"  # gateway__connect_<server>
SCOPED_CONNECT_TOOL = "mcpgw_connect"
FORWARDED_CAPS = ("elicitation", "sampling", "roots")


class ConnectRequired(Exception):
    """Raised for REST callers instead of an in-protocol prompt."""

    def __init__(self, server: str, url: str, message: str):
        super().__init__(message)
        self.server, self.url, self.message = server, url, message


@dataclass
class CallContext:
    era: str  # "modern" | "legacy" | "rest"
    client_caps: dict = field(default_factory=dict)
    input_responses: dict | None = None
    request_state: str | None = None
    scope: str | None = None  # set on /mcp/<server>: tool names are unqualified


def tool_error(text: str, **extra: Any) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True, **extra}


class Gateway:
    def __init__(self, cfg: GatewayConfig, *, store: TokenStore | None = None,
                 http: aiohttp.ClientSession | None = None):
        load_plugins(cfg.plugins)
        self.cfg = cfg
        self.store = store or FileStore(cfg.state_dir / "credentials.json")
        self.signer = Signer(load_or_create_secret(cfg.state_dir))
        self._http = http
        self._own_http = http is None
        self.auth: AuthManager | None = None
        self.upstreams: dict[str, Upstream] = {}
        self._used_tickets: dict[str, float] = {}
        # resources/read routing: exact URIs seen in listings or tool results, and
        # the literal prefix of each resource template, mapped to their server.
        self._resource_owner: dict[str, str] = {}
        self._template_prefixes: dict[str, str] = {}
        # Validate provider config eagerly so typos fail at startup.
        self._server_cfgs = {}
        for name, sc in cfg.servers.items():
            if not sc.enabled:
                continue
            sc = replace(sc, headers=dict(sc.headers), options=dict(sc.options))
            provider = get_provider(sc.provider)
            provider.configure(sc)
            self._server_cfgs[name] = (sc, provider)

    async def start(self) -> None:
        if self._http is None:
            self._http = aiohttp.ClientSession(trust_env=True)
        self.auth = AuthManager(self.cfg, self.store, self._http)
        for name, (sc, provider) in self._server_cfgs.items():
            self.upstreams[name] = Upstream(sc, provider, self.auth, self._http, self.cfg)
        log.info("gateway ready with %d server(s): %s", len(self.upstreams), ", ".join(self.upstreams) or "-")

    async def close(self) -> None:
        await asyncio.gather(*(u.close() for u in self.upstreams.values()), return_exceptions=True)
        if self._own_http and self._http:
            await self._http.close()

    # -- naming -------------------------------------------------------------

    @property
    def sep(self) -> str:
        return self.cfg.separator

    def qualify(self, server: str, tool: str) -> str:
        return f"{server}{self.sep}{tool}"

    def resolve(self, name: str, scope: str | None) -> tuple[str, str]:
        if scope:
            return scope, name
        server, s, tool = name.partition(self.sep)
        if not s or not tool:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        return server, tool

    # -- connect links ------------------------------------------------------

    def connect_url(self, server: str, *, step_up: bool = False) -> str:
        t = self.signer.sign(TICKET_PREFIX, {"s": server, "u": int(step_up)}, ttl=900)
        return f"{self.cfg.public_url}/connect/{server}?t={t}"

    def check_ticket(self, server: str, ticket: str, *, consume: bool) -> dict | None:
        payload = self.signer.verify(TICKET_PREFIX, ticket or "")
        if not payload or payload.get("s") != server:
            return None
        nonce = payload["n"]
        now = time.time()
        for k in [k for k, exp in self._used_tickets.items() if exp < now]:
            del self._used_tickets[k]
        if nonce in self._used_tickets:
            return None
        if consume:
            self._used_tickets[nonce] = payload["exp"]
        return payload

    async def begin_connect(self, server: str, step_up: bool) -> str:
        up = self.upstreams[server]
        assert self.auth
        return await self.auth.begin(up, step_up=step_up)

    async def finish_connect(self, query: dict[str, str]) -> str:
        assert self.auth
        server = await self.auth.complete(query)
        up = self.upstreams.get(server)
        if up:
            up.invalidate()  # listings may depend on the granted scopes
            up.status, up.last_error = "unknown", None
        return server

    async def disconnect(self, server: str) -> None:
        assert self.auth
        up = self.upstreams.get(server)
        if up is None:
            raise KeyError(server)
        await self.auth.disconnect(server)
        await up.close()
        up.reset()
        up.status = "unknown"

    # -- status -------------------------------------------------------------

    async def status(self) -> list[dict]:
        assert self.auth
        out = []
        for name, up in self.upstreams.items():
            tok = await self.auth.token(up) if up.cfg.auth == "oauth" else None
            connected = bool(tok) if up.cfg.auth == "oauth" else up.cfg.auth == "none" or bool(up.cfg.token)
            row = {
                "name": name,
                "display_name": up.display_name,
                "provider": up.provider.name,
                "url": up.url,
                "auth": up.cfg.auth,
                "status": up.status,
                "connected": connected,
                "protocol": up.version,
                "era": up.era,
                "tools": len(up.cached_tools() or []) if up.cached_tools() is not None else None,
                "scopes": (tok or {}).get("scopes") or [],
                "expires_at": (tok or {}).get("expires_at"),
                # bumps each time a browser sign-in completes (lets `mcpgw login` wait for *this* one)
                "auth_generation": self.auth.generation(name),
                "error": up.last_error,
            }
            if up.cfg.auth == "oauth" and not up.problems() and (not connected or up.status == "needs_auth"):
                row["connect_url"] = self.connect_url(name)
            out.append(row)
        return out

    # -- tools/list ---------------------------------------------------------

    async def _list_one(self, up: Upstream) -> list[dict] | None:
        try:
            return await asyncio.wait_for(up.list_tools(), timeout=min(up.cfg.timeout, 30.0))
        except AuthRequired:
            return None
        except (UpstreamError, OAuthError, asyncio.TimeoutError) as e:
            if isinstance(e, asyncio.TimeoutError):
                up.status, up.last_error = "error", "tools/list timed out"
            log.warning("%s: tools/list failed: %s", up.name, up.last_error or e)
            return None

    def _expose(self, up: Upstream, tool: dict, scope: str | None) -> dict:
        t = copy.deepcopy(tool)
        t["name"] = tool["name"] if scope else self.qualify(up.name, tool["name"])
        if "inputSchema" in t:
            t["inputSchema"] = strip_param_headers(t["inputSchema"])
        else:
            t["inputSchema"] = {"type": "object"}
        if not scope:
            desc = t.get("description") or ""
            t["description"] = f"[{up.display_name}] {desc}".strip()
        if len(t["name"]) > 64:
            log.debug("tool name %r is longer than 64 chars; some clients reject that", t["name"])
        return t

    def _connect_tool(self, up: Upstream, scope: str | None) -> dict:
        return {
            "name": SCOPED_CONNECT_TOOL if scope else self.qualify("gateway", CONNECT_TOOL + up.name),
            "title": f"Connect {up.display_name}",
            "description": f"Connect your {up.display_name} account to the gateway. Its tools appear "
                           "after you sign in. Call this when the user wants to use "
                           f"{up.display_name}.",
            "inputSchema": {"type": "object", "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": True},
        }

    def _status_tool(self) -> dict:
        return {
            "name": self.qualify("gateway", "status"),
            "title": "Gateway status",
            "description": "List the MCP servers behind this gateway, whether each is connected, "
                           "and links to connect the ones that are not.",
            "inputSchema": {"type": "object", "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }

    async def list_tools(self, scope: str | None = None) -> tuple[list[dict], int]:
        targets = [self.upstreams[scope]] if scope else list(self.upstreams.values())
        results = await asyncio.gather(*(self._list_one(u) for u in targets))
        tools: list[dict] = []
        needs_auth = False
        for up, listed in zip(targets, results):
            if listed is not None:
                tools.extend(self._expose(up, t, scope) for t in listed)
            elif up.status == "needs_auth":
                needs_auth = True
                if self.cfg.expose_connect_tools:
                    tools.append(self._connect_tool(up, scope))
        if not scope:
            tools.append(self._status_tool())
        now = time.time()
        expiries = [u._tools_expire - now for u in targets if u.cached_tools() is not None]
        ttl = min([self.cfg.tool_list_ttl] + expiries)
        if needs_auth:
            ttl = min(ttl, 30.0)
        return tools, int(max(ttl, 0) * 1000)

    # -- tools/call ---------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict | None, ctx: CallContext) -> dict:
        if arguments is not None and not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        if ctx.scope and name == SCOPED_CONNECT_TOOL:
            return await self._connect_tool_call(self.upstreams[ctx.scope], name, ctx)
        server, tool = self.resolve(name, ctx.scope)
        if server == "gateway" and not ctx.scope:
            return await self._builtin(tool, ctx, name)
        up = self.upstreams.get(server)
        if up is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")

        input_responses, up_state = ctx.input_responses, None
        if ctx.request_state is not None:
            rs = ctx.request_state
            if rs.startswith(UPSTREAM_STATE_PREFIX):
                up_state = rs[len(UPSTREAM_STATE_PREFIX):]
            else:
                payload = self.signer.verify(STATE_PREFIX, rs)
                if not payload or payload.get("t") != name or payload.get("a") != digest(arguments or {}):
                    raise RpcError(INVALID_PARAMS, "invalid or expired requestState")
                done = await self._after_connect_prompt(up, name, payload, ctx)
                if done is not None:
                    return done
                input_responses = None  # those answered *our* prompt, not the upstream's

        caps = {k: v for k, v in ctx.client_caps.items() if k in FORWARDED_CAPS} if ctx.era == "modern" else {}
        try:
            result = await up.call_tool(tool, arguments, caps=caps, input_responses=input_responses,
                                        request_state=up_state)
        except AuthRequired as e:
            return await self._auth_prompt(up, name, arguments, ctx, e.step_up, e.reason)
        except UpstreamRpcError as e:
            raise RpcError(e.code, e.message, e.data) from e
        except (UpstreamError, OAuthError) as e:
            return tool_error(f"{up.display_name} is unavailable: {e}")

        if result.get("resultType") == "input_required":
            if ctx.era != "modern":
                return tool_error(f"{up.display_name} needs interactive input that this client "
                                  "cannot provide (it requires MCP 2026-07-28 multi round-trip support).")
            out = dict(result)
            if isinstance(out.get("requestState"), str):
                out["requestState"] = UPSTREAM_STATE_PREFIX + out["requestState"]
            return out
        self._remember_resource_links(up, result)
        return result

    def _remember_resource_links(self, up: Upstream, result: dict) -> None:
        """Tool results may link to resources; remember who owns them so a later
        resources/read on the aggregated endpoint reaches the right server."""
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") if item.get("type") == "resource_link" else (
                (item.get("resource") or {}).get("uri") if item.get("type") == "resource" else None)
            if isinstance(uri, str) and len(self._resource_owner) < 10_000:
                self._resource_owner[uri] = up.name

    async def _after_connect_prompt(self, up: Upstream, name: str, payload: dict,
                                    ctx: CallContext) -> dict | None:
        """Retry after our own URL elicitation. Returns a result to send, or None to proceed."""
        answer = (ctx.input_responses or {}).get(payload.get("k"), {})
        action = answer.get("action") if isinstance(answer, dict) else None
        if action in ("decline", "cancel"):
            return tool_error(f"{up.display_name} was not connected ({action}d).")
        assert self.auth
        since = int(payload.get("g", 0))
        if not await self.auth.wait_connected(up.name, since, self.cfg.auth_wait_seconds):
            return await self._auth_prompt(up, name, None, ctx, bool(payload.get("u")),
                                           "still waiting for sign-in", args_digest=payload.get("a"),
                                           tool_is_connect=bool(payload.get("tool_is_connect")),
                                           generation=since)
        if payload.get("tool_is_connect"):
            return self._connected_result(up)
        return None

    @staticmethod
    def _connected_result(up: Upstream) -> dict:
        return {"content": [{"type": "text", "text": f"{up.display_name} is connected. Its tools are "
                             "available now; refresh the tool list if they don't appear."}]}

    async def _auth_prompt(self, up: Upstream, name: str, arguments: dict | None, ctx: CallContext,
                           step_up: bool, reason: str, *, args_digest: str | None = None,
                           tool_is_connect: bool = False, generation: int | None = None) -> dict:
        assert self.auth
        if up.problems():
            return tool_error(f"{up.display_name} can't be connected: {up.problems()[0]}")
        if up.cfg.auth != "oauth":
            return tool_error(f"{up.display_name} rejected the gateway's credentials: {reason}")
        url = self.connect_url(up.name, step_up=step_up)
        verb = "Grant additional permissions to" if step_up else "Connect"
        message = f"{verb} {up.display_name} to continue ({name})."
        elicit = ctx.client_caps.get("elicitation")
        url_mode = isinstance(elicit, dict) and "url" in elicit
        if ctx.era == "rest":
            raise ConnectRequired(up.name, url, message)
        if ctx.era == "modern" and url_mode:
            key = f"connect_{up.name}"
            state = self.signer.sign(STATE_PREFIX, {
                "t": name, "k": key, "a": args_digest or digest(arguments or {}),
                "tool_is_connect": tool_is_connect, "u": int(step_up),
                # the retry proceeds once an authorization *newer than this* completes
                "g": self.auth.generation(up.name) if generation is None else generation,
            }, ttl=900)
            return {
                "resultType": "input_required",
                "inputRequests": {key: {"method": "elicitation/create",
                                        "params": {"mode": "url", "url": url, "message": message}}},
                "requestState": state,
            }
        if ctx.era == "legacy" and url_mode:
            raise RpcError(URL_ELICITATION_REQUIRED, "This request requires more information.", {
                "elicitations": [{"mode": "url", "elicitationId": str(uuid.uuid4()), "url": url,
                                  "message": message}],
            })
        return tool_error(f"{message}\n\nOpen this link to sign in, then retry:\n{url}",
                          _meta={"mcpgw/connectUrl": url, "mcpgw/server": up.name})

    async def _connect_tool_call(self, up: Upstream, name: str, ctx: CallContext) -> dict:
        assert self.auth
        if ctx.request_state is not None:
            payload = self.signer.verify(STATE_PREFIX, ctx.request_state)
            if not payload or payload.get("t") != name:
                raise RpcError(INVALID_PARAMS, "invalid or expired requestState")
            done = await self._after_connect_prompt(up, name, payload, ctx)
            return done or self._connected_result(up)
        if await self.auth.token(up) and up.status != "needs_auth":
            return {"content": [{"type": "text", "text": f"{up.display_name} is already connected."}]}
        return await self._auth_prompt(up, name, {}, ctx, False, "not connected", tool_is_connect=True)

    # -- prompts & resources ------------------------------------------------

    async def _list_items_one(self, up: Upstream, method: str) -> list[dict] | None:
        try:
            return await asyncio.wait_for(up.list_items(method), timeout=min(up.cfg.timeout, 30.0))
        except AuthRequired:
            return None
        except UpstreamRpcError as e:
            log.warning("%s: %s failed: %s", up.name, method, e)
            return None
        except (UpstreamError, OAuthError, asyncio.TimeoutError) as e:
            if isinstance(e, asyncio.TimeoutError):
                up.status, up.last_error = "error", f"{method} timed out"
            log.warning("%s: %s failed: %s", up.name, method, up.last_error or e)
            return None

    async def _gather(self, method: str, scope: str | None) -> tuple[list[tuple[Upstream, list[dict]]], int]:
        targets = [self.upstreams[scope]] if scope else list(self.upstreams.values())
        results = await asyncio.gather(*(self._list_items_one(u, method) for u in targets))
        now = time.time()
        ttl = self.cfg.tool_list_ttl
        pairs = []
        for up, items in zip(targets, results):
            if items is None:
                ttl = min(ttl, 30.0)  # a server is unreachable or not connected: re-check soon
                continue
            pairs.append((up, items))
            exp = up.list_expiry(method)
            if exp is not None:
                ttl = min(ttl, exp - now)
        return pairs, int(max(ttl, 0) * 1000)

    async def list_prompts(self, scope: str | None = None) -> tuple[list[dict], int]:
        pairs, ttl = await self._gather("prompts/list", scope)
        out = []
        for up, prompts in pairs:
            for p in prompts:
                p = copy.deepcopy(p)
                if not scope:
                    p["name"] = self.qualify(up.name, p["name"])
                    p["description"] = f"[{up.display_name}] {p.get('description') or ''}".strip()
                out.append(p)
        return out, ttl

    async def list_resources(self, scope: str | None = None) -> tuple[list[dict], int]:
        pairs, ttl = await self._gather("resources/list", scope)
        out = []
        for up, resources in pairs:
            for r in resources:
                self._resource_owner[r["uri"]] = up.name
                out.append(self._tag_resource(up, r, scope))
        return out, ttl

    async def list_resource_templates(self, scope: str | None = None) -> tuple[list[dict], int]:
        pairs, ttl = await self._gather("resources/templates/list", scope)
        out = []
        for up, templates in pairs:
            for t in templates:
                prefix = t["uriTemplate"].split("{", 1)[0]
                if prefix:
                    self._template_prefixes[prefix] = up.name
                out.append(self._tag_resource(up, t, scope))
        return out, ttl

    @staticmethod
    def _tag_resource(up: Upstream, item: dict, scope: str | None) -> dict:
        """Resource URIs are global identifiers, so they pass through unchanged; the
        owning server is recorded in _meta and, on /mcp, the description."""
        item = copy.deepcopy(item)
        if not scope:
            item["description"] = f"[{up.display_name}] {item.get('description') or ''}".strip()
        meta = dict(item.get("_meta") or {})
        meta["mcpgw/server"] = up.name
        item["_meta"] = meta
        return item

    async def get_prompt(self, name: str, arguments: Any, ctx: CallContext) -> dict:
        if arguments is not None and (not isinstance(arguments, dict)
                                      or not all(isinstance(v, str) for v in arguments.values())):
            raise RpcError(INVALID_PARAMS, "prompt arguments must be an object of strings")
        try:
            server, prompt = self.resolve(name, ctx.scope)
        except RpcError:
            raise RpcError(INVALID_PARAMS, f"Unknown prompt: {name}") from None
        up = self.upstreams.get(server)
        if up is None:
            raise RpcError(INVALID_PARAMS, f"Unknown prompt: {name}")
        return await self._feature_call(up, name, ctx, up.get_prompt(prompt, arguments))

    def route_resource(self, uri: str, scope: str | None) -> str | None:
        if scope:
            return scope
        if uri in self._resource_owner:
            return self._resource_owner[uri]
        matches = [p for p in self._template_prefixes if uri.startswith(p)]
        if matches:
            return self._template_prefixes[max(matches, key=len)]
        # Last resort: the only server that has (or, not connected yet, may have) resources.
        candidates = [u.name for u in self.upstreams.values()
                      if u.supports("resources") or (u.era is None and not u.problems())]
        return candidates[0] if len(candidates) == 1 else None

    async def read_resource(self, uri: Any, ctx: CallContext) -> dict:
        if not isinstance(uri, str) or not uri:
            raise RpcError(INVALID_PARAMS, "params.uri is required")
        server = self.route_resource(uri, ctx.scope)
        if server is None:
            # Unknown URI: refresh the listings once, then route again.
            await asyncio.gather(self.list_resources(), self.list_resource_templates())
            server = self.route_resource(uri, ctx.scope)
        up = self.upstreams.get(server or "")
        if up is None:
            data: dict[str, Any] = {"uri": uri}
            pending = [u.name for u in self.upstreams.values() if u.status == "needs_auth"]
            if pending:  # the owner may be a server the user hasn't connected yet
                data["notConnected"] = pending
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", data)
        return await self._feature_call(up, uri, ctx, up.read_resource(uri))

    async def _feature_call(self, up: Upstream, name: str, ctx: CallContext, coro) -> dict:
        """Run a prompts/get or resources/read call, mapping failures to JSON-RPC errors."""
        try:
            return await coro
        except AuthRequired as e:
            raise self._auth_rpc_error(up, name, ctx, e.step_up) from e
        except UpstreamRpcError as e:
            raise RpcError(e.code, e.message, e.data) from e
        except (UpstreamError, OAuthError) as e:
            raise RpcError(INTERNAL_ERROR, f"{up.display_name} is unavailable: {e}") from e

    def _auth_rpc_error(self, up: Upstream, name: str, ctx: CallContext, step_up: bool) -> Exception:
        """Prompts and resources can't return a tool result, so a needed sign-in is an error."""
        if up.problems():
            return RpcError(INVALID_REQUEST, f"{up.display_name} can't be connected: {up.problems()[0]}")
        if up.cfg.auth != "oauth":
            return RpcError(INVALID_REQUEST, f"{up.display_name} rejected the gateway's credentials")
        url = self.connect_url(up.name, step_up=step_up)
        message = f"{'Grant additional permissions to' if step_up else 'Connect'} {up.display_name} to continue ({name})."
        if ctx.era == "rest":
            return ConnectRequired(up.name, url, message)
        elicit = ctx.client_caps.get("elicitation")
        if ctx.era == "legacy" and isinstance(elicit, dict) and "url" in elicit:
            return RpcError(URL_ELICITATION_REQUIRED, "This request requires more information.", {
                "elicitations": [{"mode": "url", "elicitationId": str(uuid.uuid4()), "url": url,
                                  "message": message}]})
        return RpcError(INVALID_REQUEST, f"{message} Open this link to sign in, then retry: {url}",
                        {"connectUrl": url, "server": up.name})

    async def _builtin(self, tool: str, ctx: CallContext, name: str) -> dict:
        if tool == "status":
            rows = await self.status()
            lines = []
            for r in rows:
                state = "connected" if r["connected"] and r["status"] != "needs_auth" else "not connected"
                line = f"- {r['display_name']} ({r['name']}): {state}, status={r['status']}"
                if r.get("error"):
                    line += f", error: {r['error']}"
                if r.get("connect_url"):
                    line += f"\n  connect: {r['connect_url']}"
                lines.append(line)
            safe = [{k: v for k, v in r.items() if k != "connect_url"} for r in rows]
            return {"content": [{"type": "text", "text": "\n".join(lines) or "No servers configured."}],
                    "structuredContent": {"servers": safe}}
        if tool.startswith(CONNECT_TOOL):
            up = self.upstreams.get(tool[len(CONNECT_TOOL):])
            if up:
                return await self._connect_tool_call(up, name, ctx)
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
