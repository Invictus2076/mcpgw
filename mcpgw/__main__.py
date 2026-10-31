"""Command line interface.

    mcpgw init | serve | doctor | login | logout | status | tools | call
          | prompts | prompt | resources | read | providers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import stat
import sys
import webbrowser
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__
from .config import ConfigError, GatewayConfig, ServerConfig, is_loopback_host, load_config
from .gateway import CallContext, ConnectRequired, Gateway
from .oauth import OAuthError
from .protocol import RpcError
from .providers import available
from .server import create_app
from .store import StoreError


class CliError(Exception):
    """A user-facing error: printed without a traceback."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# talking to a running gateway
# ---------------------------------------------------------------------------

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


async def _api(cfg: GatewayConfig, method: str, path: str, body=None) -> tuple[int, dict]:
    """Call the running gateway's REST API. Raises CliError for auth/transport problems."""
    try:
        async with aiohttp.ClientSession() as s, s.request(method, _local_base(cfg) + path, json=body,
                                                           headers=_headers(cfg)) as r:
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = None
            status = r.status
    except (aiohttp.ClientError, OSError) as e:
        raise CliError(f"can't reach the gateway at {_local_base(cfg)}: {e}") from e
    if status == 401:
        raise CliError("the running gateway rejected the API key: make sure this config's "
                       "gateway.api_key matches the one the gateway was started with")
    if not isinstance(data, dict):
        raise CliError(f"unexpected response from the gateway (HTTP {status})")
    return status, data


def _api_error(status: int, data: dict) -> CliError:
    if status == 409:
        return CliError(f"{data.get('message')}\nConnect first: mcpgw login {data.get('server')}")
    return CliError(f"error: {data.get('error') or f'HTTP {status}'}")


async def _with_gateway(cfg: GatewayConfig, fn):
    gw = Gateway(cfg)
    await gw.start()
    try:
        return await fn(gw)
    finally:
        await gw.close()


def _require_server(cfg: GatewayConfig, server: str) -> None:
    if server not in cfg.servers:
        known = ", ".join(cfg.servers) or "none configured"
        raise CliError(f"unknown server {server!r} (configured: {known})", 2)
    if not cfg.servers[server].enabled:
        raise CliError(f"server {server!r} is disabled in the config", 2)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

async def cmd_serve(cfg: GatewayConfig) -> None:
    app = create_app(Gateway(cfg))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    print(f"mcpgw {__version__} listening on {_local_base(cfg)}  (MCP endpoint: {cfg.public_url}/mcp)",
          flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)  # graceful shutdown (docker stop, Ctrl-C)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl-C raises KeyboardInterrupt
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


async def cmd_login(cfg: GatewayConfig, server: str, open_browser: bool) -> int:
    _require_server(cfg, server)
    if await _running(cfg):
        _, data = await _api(cfg, "GET", "/api/servers")
        since = next((r.get("auth_generation", 0) for r in data["servers"] if r["name"] == server), 0)
        status, data = await _api(cfg, "POST", f"/api/servers/{server}/connect")
        if status != 200:
            raise _api_error(status, data)
        _show(data["connect_url"], open_browser)
        for _ in range(300):
            await asyncio.sleep(1)
            _, data = await _api(cfg, "GET", "/api/servers")
            row = next(r for r in data["servers"] if r["name"] == server)
            # wait for *this* sign-in, not an existing connection
            if row.get("auth_generation", 0) > since:
                print(f"{server}: connected")
                return 0
        raise CliError("timed out waiting for sign-in")
    # No gateway running: serve just long enough to receive the callback.
    gw = Gateway(cfg)
    app = create_app(gw)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, cfg.host, cfg.port).start()
    except OSError as e:
        await runner.cleanup()
        raise CliError(f"can't listen on {cfg.host}:{cfg.port} for the OAuth callback: {e}") from e
    try:
        up = gw.upstreams[server]
        if up.problems():
            raise CliError(f"error: {up.problems()[0]}")
        assert gw.auth
        since = gw.auth.generation(server)
        _show(gw.connect_url(server), open_browser)
        if not await gw.auth.wait_connected(server, since, timeout=300):
            raise CliError("timed out waiting for sign-in")
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


async def cmd_logout(cfg: GatewayConfig, server: str) -> int:
    _require_server(cfg, server)
    if await _running(cfg):
        status, data = await _api(cfg, "POST", f"/api/servers/{server}/disconnect")
        if status != 200:
            raise _api_error(status, data)
    else:
        await _with_gateway(cfg, lambda gw: gw.disconnect(server))
    print(f"{server}: disconnected")
    return 0


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


def _first_line(text: str | None, width: int = 110) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:width] if lines else ""


async def cmd_tools(cfg: GatewayConfig, as_json: bool) -> int:
    if await _running(cfg):
        _, data = await _api(cfg, "GET", "/api/tools")
        tools = data["tools"]
    else:
        tools, _ = await _with_gateway(cfg, lambda gw: gw.list_tools())
    if as_json:
        print(json.dumps(tools, indent=2))
    else:
        for t in tools:
            print(f"{t['name']}\n    {_first_line(t.get('description'))}")
    return 0


async def cmd_prompts(cfg: GatewayConfig, as_json: bool) -> int:
    if await _running(cfg):
        _, data = await _api(cfg, "GET", "/api/prompts")
        prompts = data["prompts"]
    else:
        prompts, _ = await _with_gateway(cfg, lambda gw: gw.list_prompts())
    if as_json:
        print(json.dumps(prompts, indent=2))
    else:
        for p in prompts:
            args = ", ".join(a["name"] + ("" if a.get("required") else "?") for a in p.get("arguments") or [])
            print(f"{p['name']}({args})\n    {_first_line(p.get('description'))}")
    return 0


async def cmd_resources(cfg: GatewayConfig, as_json: bool) -> int:
    if await _running(cfg):
        _, data = await _api(cfg, "GET", "/api/resources")
    else:
        async def go(gw: Gateway):
            (res, _), (tpl, _) = await asyncio.gather(gw.list_resources(), gw.list_resource_templates())
            return {"resources": res, "resourceTemplates": tpl}
        data = await _with_gateway(cfg, go)
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        for r in data["resources"]:
            print(f"{r['uri']}  ({r['_meta']['mcpgw/server']})\n    {_first_line(r.get('description') or r.get('name'))}")
        for t in data["resourceTemplates"]:
            print(f"{t['uriTemplate']}  ({t['_meta']['mcpgw/server']}, template)")
    return 0


async def _rest_or_local(cfg: GatewayConfig, path: str, body: dict, local) -> dict:
    """Run a call on the running gateway, or in-process if none is running."""
    if await _running(cfg):
        status, data = await _api(cfg, "POST", path, body)
        if status != 200:
            raise _api_error(status, data)
        return data["result"]
    try:
        return await _with_gateway(cfg, local)
    except ConnectRequired as e:
        raise CliError(f"{e.message}\nConnect first: mcpgw login {e.server}") from e
    except RpcError as e:
        raise CliError(f"error: {e.message}") from e


def _parse_args_json(args_json: str) -> dict:
    try:
        args = json.loads(args_json) if args_json else {}
    except ValueError as e:
        raise CliError(f"arguments must be JSON: {e}", 2) from e
    if not isinstance(args, dict):
        raise CliError("arguments must be a JSON object", 2)
    return args


async def cmd_call(cfg: GatewayConfig, name: str, args_json: str) -> int:
    args = _parse_args_json(args_json)
    result = await _rest_or_local(cfg, "/api/tools/call", {"name": name, "arguments": args},
                                  lambda gw: gw.call_tool(name, args, CallContext("rest")))
    for item in result.get("content") or []:
        if item.get("type") == "text":
            print(item["text"])
        else:
            print(json.dumps(item))
    if "structuredContent" in result and not result.get("content"):
        print(json.dumps(result["structuredContent"], indent=2))
    return 1 if result.get("isError") else 0


async def cmd_prompt(cfg: GatewayConfig, name: str, args_json: str) -> int:
    args = _parse_args_json(args_json)
    result = await _rest_or_local(cfg, "/api/prompts/get", {"name": name, "arguments": args},
                                  lambda gw: gw.get_prompt(name, args, CallContext("rest")))
    for m in result.get("messages") or []:
        content = m.get("content") or {}
        body = content.get("text") if content.get("type") == "text" else json.dumps(content)
        print(f"[{m.get('role')}] {body}")
    return 0


async def cmd_read(cfg: GatewayConfig, uri: str) -> int:
    result = await _rest_or_local(cfg, "/api/resources/read", {"uri": uri},
                                  lambda gw: gw.read_resource(uri, CallContext("rest")))
    for c in result.get("contents") or []:
        if "text" in c:
            print(c["text"])
        else:
            print(f"<{c.get('mimeType') or 'binary'} content, {len(c.get('blob') or '')} base64 chars>")
    return 0


# ---------------------------------------------------------------------------
# init / doctor
# ---------------------------------------------------------------------------

_SNIPPETS = {
    "notion": '[servers.notion]\nprovider = "notion"\n',
    "linear": '[servers.linear]\nprovider = "linear"\n# readonly = true                  # read-only endpoint\n',
    "sentry": '[servers.sentry]\nprovider = "sentry"\nscopes = ["org:read", "project:read", "event:read"]  # least privilege\n# org = "my-org"\n',
    "atlassian": '[servers.atlassian]\nprovider = "atlassian"\n# scopes = [...]                     # by default every advertised scope is requested\n',
    "stripe": '[servers.stripe]\nprovider = "stripe"\n',
    "github": ('# GitHub has no dynamic registration: create an OAuth App at https://github.com/settings/developers\n'
               '# with callback URL {redirect}, or use a personal access token instead.\n'
               '[servers.github]\nprovider = "github"\nclient_id = "${{GITHUB_CLIENT_ID}}"\n'
               'client_secret = "${{GITHUB_CLIENT_SECRET}}"\n# auth = "bearer"\n# token = "${{GITHUB_PAT}}"\n'),
}


def cmd_init(path: str, providers: list[str], port: int, force: bool) -> int:
    target = Path(path)
    if target.exists() and not force:
        raise CliError(f"{target} already exists (use --force to overwrite)", 2)
    unknown = [p for p in providers if p not in _SNIPPETS]
    if unknown:
        raise CliError(f"unknown provider(s): {', '.join(unknown)} (choose from {', '.join(_SNIPPETS)})", 2)
    redirect = f"http://localhost:{port}/oauth/callback"
    parts = [
        f"# mcpgw {__version__} config. Reference: mcpgw.example.toml in the repository.\n"
        "# Strings can use environment variables: ${VAR} or ${VAR:-default}.\n\n"
        "[gateway]\n"
        'host = "127.0.0.1"                    # keep on loopback unless api_key is set\n'
        f"port = {port}\n"
        '# api_key = "${MCPGW_API_KEY}"       # required for a non-loopback bind\n'
        '# public_url = "https://gw.example.com"\n'
    ]
    for p in providers or ["notion", "linear"]:
        parts.append("\n" + _SNIPPETS[p].format(redirect=redirect))
    target.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {target}\nnext: mcpgw doctor -c {target}   then   mcpgw login <server>")
    return 0


class _Report:
    def __init__(self) -> None:
        self.errors = 0
        self.warnings = 0

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def warn(self, msg: str) -> None:
        self.warnings += 1
        print(f"  warn  {msg}")

    def fail(self, msg: str) -> None:
        self.errors += 1
        print(f"  FAIL  {msg}")


async def cmd_doctor(cfg: GatewayConfig, config_path: str) -> int:
    rep = _Report()
    print(f"mcpgw {__version__}, Python {sys.version.split()[0]}")
    print(f"config: {config_path}")
    print("gateway:")
    rep.ok(f"public URL {cfg.public_url}, OAuth redirect URI {cfg.redirect_uri}")
    if not is_loopback_host(cfg.host):
        rep.ok(f"binding {cfg.host} with an API key") if cfg.api_key else rep.fail("non-loopback bind without api_key")
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        rep.ok(f"state directory {cfg.state_dir}")
        if os.name == "posix":
            mode = stat.S_IMODE(cfg.state_dir.stat().st_mode)
            if mode & 0o077:
                rep.warn(f"{cfg.state_dir} is accessible by other users (mode {mode:o}); run chmod 700")
    except OSError as e:
        rep.fail(f"state directory {cfg.state_dir}: {e}")
    if await _running(cfg):
        rep.ok(f"a gateway is running at {_local_base(cfg)}")
    else:
        print(f"  info  no gateway running at {_local_base(cfg)}")
    if not cfg.servers:
        rep.warn("no servers configured")

    async def check(gw: Gateway) -> None:
        assert gw.auth
        for name, up in gw.upstreams.items():
            print(f"{name} ({up.provider.name}, auth={up.cfg.auth}): {up.url or '(no url)'}")
            problems = up.problems()
            if problems:
                for p in problems:
                    rep.fail(p)
                continue
            if up.cfg.auth == "oauth":
                try:
                    st = await gw.auth.discover(up, force=True)
                    meta = st.as_meta or {}
                    rep.ok(f"OAuth discovery: resource {st.resource}, issuer {meta.get('issuer')}")
                    if up.cfg.client_id:
                        how = "pre-registered client_id"
                    elif gw.cfg.cimd_url and meta.get("client_id_metadata_document_supported"):
                        how = "client ID metadata document"
                    elif meta.get("registration_endpoint"):
                        how = "dynamic client registration"
                    else:
                        how = None
                    rep.ok(f"client registration: {how}") if how else rep.fail(
                        "no way to register a client: set client_id for this server")
                except OAuthError as e:
                    rep.fail(f"OAuth discovery: {e}")
                    continue
                tok = await gw.auth.token(up)
                if not tok:
                    print(f"  info  not connected yet: mcpgw login {name}")
                    continue
                rep.ok(f"connected (scopes: {' '.join(tok.get('scopes') or []) or '-'})")
            try:
                tools = await asyncio.wait_for(up.list_tools(), timeout=min(up.cfg.timeout, 30.0))
                rep.ok(f"{up.era} protocol {up.version}, {len(tools)} tools")
            except Exception as e:  # report and carry on with the next server
                if up.status == "needs_auth":
                    rep.warn(f"needs sign-in: mcpgw login {name}")
                else:
                    rep.fail(f"listing tools failed: {e}")

    await _with_gateway(cfg, check)
    print(f"\n{rep.errors} problem(s), {rep.warnings} warning(s)")
    return 1 if rep.errors else 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcpgw", description="Gateway for Streamable HTTP MCP servers")
    p.add_argument("--version", action="version", version=f"mcpgw {__version__}")
    p.add_argument("-c", "--config", help="config file (default: $MCPGW_CONFIG or ./mcpgw.toml)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="cmd", required=True)
    ip = sub.add_parser("init", help="write a starter config file")
    ip.add_argument("providers", nargs="*", help=f"presets to include ({', '.join(_SNIPPETS)}); "
                                                 "default: notion linear")
    ip.add_argument("--port", type=int, default=8765)
    ip.add_argument("--force", action="store_true", help="overwrite an existing file")
    sub.add_parser("serve", help="run the gateway")
    sub.add_parser("doctor", help="check the config and each server's OAuth setup")
    lp = sub.add_parser("login", help="connect an upstream server (opens a browser)")
    lp.add_argument("server")
    lp.add_argument("--no-browser", action="store_true", help="just print the link")
    sub.add_parser("logout", help="forget an upstream server's tokens").add_argument("server")
    for name, what in (("status", "show each server's connection state"), ("tools", "list tools"),
                       ("prompts", "list prompts"), ("resources", "list resources and templates")):
        sub.add_parser(name, help=what).add_argument("--json", action="store_true")
    cp = sub.add_parser("call", help="call a tool, e.g. mcpgw call linear__list_issues '{}'")
    cp.add_argument("tool")
    cp.add_argument("arguments", nargs="?", default="")
    pp = sub.add_parser("prompt", help="get a prompt, e.g. mcpgw prompt notion__summarize '{\"text\": \"...\"}'")
    pp.add_argument("prompt")
    pp.add_argument("arguments", nargs="?", default="")
    sub.add_parser("read", help="read a resource by URI").add_argument("uri")
    sub.add_parser("providers", help="list built-in provider presets")
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    level = (logging.INFO - 10 * min(a.verbose, 1)) if a.cmd == "serve" else (logging.WARNING - 10 * min(a.verbose, 2))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if a.cmd == "providers":
            for name, cls in available().items():
                try:
                    url = cls().build_url(ServerConfig(name="x")) or "(set url)"
                except ConfigError:
                    url = "(set url)"
                note = "  needs client_id or token" if cls.needs_client_credentials else ""
                print(f"{name:<10} {cls.display_name:<12} {url}{note}")
            return 0
        if a.cmd == "init":
            return cmd_init(a.config or os.environ.get("MCPGW_CONFIG", "mcpgw.toml"), a.providers, a.port, a.force)
        cfg = load_config(a.config)
        if a.cmd == "serve":
            asyncio.run(cmd_serve(cfg))
            return 0
        commands = {
            "doctor": lambda: cmd_doctor(cfg, a.config or os.environ.get("MCPGW_CONFIG", "mcpgw.toml")),
            "login": lambda: cmd_login(cfg, a.server, not a.no_browser),
            "logout": lambda: cmd_logout(cfg, a.server),
            "status": lambda: cmd_status(cfg, a.json),
            "tools": lambda: cmd_tools(cfg, a.json),
            "prompts": lambda: cmd_prompts(cfg, a.json),
            "resources": lambda: cmd_resources(cfg, a.json),
            "call": lambda: cmd_call(cfg, a.tool, a.arguments),
            "prompt": lambda: cmd_prompt(cfg, a.prompt, a.arguments),
            "read": lambda: cmd_read(cfg, a.uri),
        }
        return asyncio.run(commands[a.cmd]())
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except StoreError as e:
        print(f"credential store error: {e}", file=sys.stderr)
        return 2
    except CliError as e:
        print(str(e), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
