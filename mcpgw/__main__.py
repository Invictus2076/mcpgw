"""Command line: ``mcpgw serve | login | logout | status | tools | call | providers``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import webbrowser

import aiohttp
from aiohttp import web

from .config import ConfigError, GatewayConfig, ServerConfig, load_config
from .gateway import CallContext, ConnectRequired, Gateway
from .providers import available
from .server import create_app


def _local_base(cfg: GatewayConfig) -> str:
    host = cfg.host if cfg.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    host = f"[{host}]" if ":" in host else host
    return f"http://{host}:{cfg.port}"


def _headers(cfg: GatewayConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}


async def _running(cfg: GatewayConfig) -> bool:
    try:
        async with aiohttp.ClientSession() as s, s.get(_local_base(cfg) + "/healthz",
                                                       timeout=aiohttp.ClientTimeout(total=2)) as r:
            return r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


async def _api(cfg: GatewayConfig, method: str, path: str, body=None):
    async with aiohttp.ClientSession() as s, s.request(method, _local_base(cfg) + path, json=body,
                                                       headers=_headers(cfg)) as r:
        data = await r.json(content_type=None)
        return r.status, data


def _print_status(rows: list[dict]) -> None:
    if not rows:
        print("No servers configured.")
        return
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        conn = "connected" if r["connected"] and r["status"] != "needs_auth" else "not connected"
        extra = f" [{r['protocol']}]" if r.get("protocol") else ""
        tools = f", {r['tools']} tools" if r.get("tools") is not None else ""
        print(f"{r['name']:<{w}}  {conn:<13} {r['status']}{extra}{tools}")
        if r.get("error"):
            print(f"{'':<{w}}  ! {r['error']}")


async def cmd_serve(cfg: GatewayConfig) -> None:
    app = create_app(Gateway(cfg))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    print(f"mcpgw listening on {_local_base(cfg)}  (MCP endpoint: {cfg.public_url}/mcp)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def cmd_login(cfg: GatewayConfig, server: str, open_browser: bool) -> int:
    if server not in cfg.servers:
        print(f"unknown server {server!r}", file=sys.stderr)
        return 2
    if await _running(cfg):
        status, data = await _api(cfg, "POST", f"/api/servers/{server}/connect")
        if status != 200:
            print(f"error: {data.get('error')}", file=sys.stderr)
            return 1
        url = data["connect_url"]
        _show(url, open_browser)
        for _ in range(300):
            await asyncio.sleep(1)
            _, data = await _api(cfg, "GET", "/api/servers")
            row = next(r for r in data["servers"] if r["name"] == server)
            if row["connected"]:
                print(f"{server}: connected")
                return 0
        print("timed out waiting for sign-in", file=sys.stderr)
        return 1
    # No gateway running: serve just long enough to receive the callback.
    gw = Gateway(cfg)
    app = create_app(gw)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    try:
        up = gw.upstreams[server]
        if up.problems():
            print(f"error: {up.problems()[0]}", file=sys.stderr)
            return 1
        assert gw.auth
        since = gw.auth.generation(server)
        _show(gw.connect_url(server), open_browser)
        if not await gw.auth.wait_connected(server, since, timeout=300):
            print("timed out waiting for sign-in", file=sys.stderr)
            return 1
        print(f"{server}: connected")
        return 0
    finally:
        await runner.cleanup()


def _show(url: str, open_browser: bool) -> None:
    print(f"Open this link to connect:\n  {url}", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass


async def _with_gateway(cfg: GatewayConfig, fn):
    gw = Gateway(cfg)
    await gw.start()
    try:
        return await fn(gw)
    finally:
        await gw.close()


async def cmd_status(cfg: GatewayConfig, as_json: bool) -> int:
    if await _running(cfg):
        _, data = await _api(cfg, "GET", "/api/servers?probe=1")
        rows = data["servers"]
    else:
        async def go(gw: Gateway):
            await gw.list_tools()  # probe every server
            return await gw.status()
        rows = await _with_gateway(cfg, go)
    print(json.dumps(rows, indent=2)) if as_json else _print_status(rows)
    return 0


async def cmd_tools(cfg: GatewayConfig, as_json: bool) -> int:
    if await _running(cfg):
        _, data = await _api(cfg, "GET", "/api/tools")
        tools = data["tools"]
    else:
        tools = await _with_gateway(cfg, lambda gw: gw.list_tools())
        tools = tools[0]
    if as_json:
        print(json.dumps(tools, indent=2))
    else:
        for t in tools:
            print(f"{t['name']}\n    {(t.get('description') or '').splitlines()[0][:110] if t.get('description') else ''}")
    return 0


async def cmd_call(cfg: GatewayConfig, name: str, args_json: str) -> int:
    try:
        args = json.loads(args_json) if args_json else {}
    except ValueError as e:
        print(f"arguments must be JSON: {e}", file=sys.stderr)
        return 2
    if await _running(cfg):
        status, data = await _api(cfg, "POST", "/api/tools/call", {"name": name, "arguments": args})
        if status == 409:
            print(f"{data['message']}\nConnect first: mcpgw login {data['server']}", file=sys.stderr)
            return 1
        if status != 200:
            print(f"error: {data.get('error')}", file=sys.stderr)
            return 1
        result = data["result"]
    else:
        async def go(gw: Gateway):
            return await gw.call_tool(name, args, CallContext("rest"))
        try:
            result = await _with_gateway(cfg, go)
        except ConnectRequired as e:
            print(f"{e.message}\nConnect first: mcpgw login {e.server}", file=sys.stderr)
            return 1
    for item in result.get("content") or []:
        if item.get("type") == "text":
            print(item["text"])
        else:
            print(json.dumps(item))
    if "structuredContent" in result and not result.get("content"):
        print(json.dumps(result["structuredContent"], indent=2))
    return 1 if result.get("isError") else 0


async def cmd_logout(cfg: GatewayConfig, server: str) -> int:
    if await _running(cfg):
        status, data = await _api(cfg, "POST", f"/api/servers/{server}/disconnect")
        if status != 200:
            print(f"error: {data.get('error')}", file=sys.stderr)
            return 1
    else:
        await _with_gateway(cfg, lambda gw: gw.disconnect(server))
    print(f"{server}: disconnected")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcpgw", description="Gateway for Streamable HTTP MCP servers")
    p.add_argument("-c", "--config", help="config file (default: $MCPGW_CONFIG or ./mcpgw.toml)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the gateway")
    lp = sub.add_parser("login", help="connect an upstream server (opens a browser)")
    lp.add_argument("server")
    lp.add_argument("--no-browser", action="store_true", help="just print the link")
    sub.add_parser("logout", help="forget an upstream server's tokens").add_argument("server")
    for name in ("status", "tools"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    cp = sub.add_parser("call", help="call a tool, e.g. mcpgw call linear__list_issues '{}'")
    cp.add_argument("tool")
    cp.add_argument("arguments", nargs="?", default="")
    sub.add_parser("providers", help="list built-in provider presets")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING - 10 * min(a.verbose, 2) if a.cmd != "serve" else
                        logging.INFO - 10 * min(a.verbose, 1),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd == "providers":
        for name, cls in available().items():
            try:
                url = cls().build_url(ServerConfig(name="x")) or "(set url)"
            except ConfigError:
                url = "(set url)"
            note = "  needs client_id or token" if cls.needs_client_credentials else ""
            print(f"{name:<10} {cls.display_name:<12} {url}{note}")
        return 0
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        if a.cmd == "serve":
            asyncio.run(cmd_serve(cfg))
            return 0
        if a.cmd == "login":
            return asyncio.run(cmd_login(cfg, a.server, not a.no_browser))
        if a.cmd == "logout":
            return asyncio.run(cmd_logout(cfg, a.server))
        if a.cmd == "status":
            return asyncio.run(cmd_status(cfg, a.json))
        if a.cmd == "tools":
            return asyncio.run(cmd_tools(cfg, a.json))
        if a.cmd == "call":
            return asyncio.run(cmd_call(cfg, a.tool, a.arguments))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
