"""End-to-end tests: real HTTP between a downstream client, the gateway, and a strict mock provider.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_provider import MockProvider  # noqa: E402

from mcpgw.config import parse_config  # noqa: E402
from mcpgw.gateway import Gateway  # noqa: E402
from mcpgw.server import create_app  # noqa: E402
from mcpgw.store import MemoryStore  # noqa: E402

MODERN = "2026-07-28"
URL_CAPS = {"elicitation": {"url": {}}}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness:
    def __init__(self, mocks: dict[str, MockProvider], *, gateway: dict | None = None,
                 servers: dict[str, dict] | None = None):
        self.mocks = mocks
        self.gateway_extra = gateway or {}
        self.server_extra = servers or {}
        self.tmp = tempfile.TemporaryDirectory()

    async def __aenter__(self) -> "Harness":
        for m in self.mocks.values():
            await m.start()
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        raw = {
            "gateway": {"host": "127.0.0.1", "port": port, "public_url": self.base,
                        "state_dir": self.tmp.name, "auth_wait_seconds": 5, **self.gateway_extra},
            "servers": {name: {"url": m.url, **self.server_extra.get(name, {})} for name, m in self.mocks.items()},
        }
        self.cfg = parse_config(raw)
        self.gw = Gateway(self.cfg, store=MemoryStore())
        self.runner = web.AppRunner(create_app(self.gw), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        self.http = aiohttp.ClientSession()
        self.api_headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        return self

    async def __aexit__(self, *exc) -> None:
        await self.http.close()
        await self.runner.cleanup()
        for m in self.mocks.values():
            await m.stop()
        self.tmp.cleanup()

    # -- downstream clients -------------------------------------------------

    async def modern(self, method: str, params: dict | None = None, caps: dict | None = None, *,
                     path: str = "/mcp", headers: dict | None = None, version: str = MODERN):
        p = dict(params or {})
        p["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                      "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                      "io.modelcontextprotocol/clientCapabilities": caps or {}}
        h = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version,
             "Mcp-Method": method, **self.api_headers}
        if method == "tools/call":
            h["Mcp-Name"] = p["name"]
        h.update(headers or {})
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}
        async with self.http.post(self.base + path, json=body, headers=h) as r:
            return r.status, await r.json(content_type=None)

    async def legacy_init(self, caps: dict | None = None, path: str = "/mcp") -> str:
        body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": caps or {},
                           "clientInfo": {"name": "legacy-test", "version": "0"}}}
        async with self.http.post(self.base + path, json=body, headers=self.api_headers) as r:
            assert r.status == 200, await r.text()
            sid = r.headers["Mcp-Session-Id"]
        h = {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25", **self.api_headers}
        async with self.http.post(self.base + path, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                  headers=h) as r:
            assert r.status == 202
        return sid

    async def legacy(self, sid: str | None, method: str, params: dict | None = None, path: str = "/mcp"):
        h = {"MCP-Protocol-Version": "2025-11-25", **self.api_headers}
        if sid:
            h["Mcp-Session-Id"] = sid
        body = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}
        async with self.http.post(self.base + path, json=body, headers=h) as r:
            return r.status, await r.json(content_type=None)

    async def api(self, method: str, path: str, body=None):
        async with self.http.request(method, self.base + path, json=body, headers=self.api_headers) as r:
            return r.status, await r.json(content_type=None)

    async def browser(self, connect_url: str) -> tuple[int, str]:
        """Act like the user's browser: confirm, go to the AS, come back to the callback."""
        async with aiohttp.ClientSession() as b:
            async with b.get(connect_url) as r:
                page = await r.text()
                if r.status != 200:
                    return r.status, page
            ticket = re.search(r'name="t" value="([^"]+)"', page).group(1)
            action = connect_url.split("?")[0]
            async with b.post(action, data={"t": ticket}, headers={"Origin": self.base},
                              allow_redirects=False) as r:
                if r.status != 302:
                    return r.status, await r.text()
                loc = r.headers["Location"]
            async with b.get(loc, allow_redirects=False) as r:
                if r.status != 302:
                    return r.status, await r.text()
                loc = r.headers["Location"]
            async with b.get(loc) as r:
                return r.status, await r.text()

    async def connect(self, server: str = "mock") -> None:
        status, data = await self.api("POST", f"/api/servers/{server}/connect")
        assert status == 200, data
        status, page = await self.browser(data["connect_url"])
        assert status == 200 and "connected" in page, page


def tool_names(body: dict) -> list[str]:
    return [t["name"] for t in body["result"]["tools"]]


def text(result: dict) -> str:
    return result["content"][0]["text"]


class ModernFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_flow_with_dcr_and_url_elicitation(self):
        mock = MockProvider(era="modern", registration="dcr")
        async with Harness({"mock": mock}) as h:
            st, body = await h.modern("tools/list", {}, URL_CAPS)
            self.assertEqual(st, 200)
            res = body["result"]
            self.assertEqual(res["resultType"], "complete")
            self.assertEqual(res["cacheScope"], "private")
            self.assertIn("ttlMs", res)
            self.assertIn("gateway__connect_mock", tool_names(body))
            self.assertIn("gateway__status", tool_names(body))

            call = {"name": "mock__echo", "arguments": {"text": "hi"}}
            st, body = await h.modern("tools/call", call, URL_CAPS)
            r = body["result"]
            self.assertEqual(r["resultType"], "input_required")
            key, req = next(iter(r["inputRequests"].items()))
            self.assertEqual(req["method"], "elicitation/create")
            self.assertEqual(req["params"]["mode"], "url")
            self.assertTrue(req["params"]["url"].startswith(h.base + "/connect/mock?t="))

            # The client retries right away (after user consent); the gateway holds the
            # retry open until the browser sign-in completes.
            retry = asyncio.create_task(h.modern(
                "tools/call", {**call, "inputResponses": {key: {"action": "accept"}},
                               "requestState": r["requestState"]}, URL_CAPS))
            await asyncio.sleep(0.2)
            st, page = await h.browser(req["params"]["url"])
            self.assertEqual(st, 200, page)
            st, body = await retry
            self.assertEqual(text(body["result"]), "hi")
            self.assertEqual(body["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "mcpgw")

            reg = mock.registrations[0]
            self.assertEqual(reg["application_type"], "native")
            self.assertEqual(reg["token_endpoint_auth_method"], "none")
            self.assertEqual(reg["redirect_uris"], [h.base + "/oauth/callback"])
            authz = mock.authorize_requests[0]
            self.assertEqual(authz["resource"], mock.url)
            self.assertEqual(authz["code_challenge_method"], "S256")
            # WWW-Authenticate scope, plus offline_access because the AS advertises it
            self.assertEqual(authz["scope"].split(), ["read", "offline_access"])

            upstream_call = [e for e in mock.log if e["method"] == "tools/call"][-1]
            self.assertEqual(upstream_call["headers"]["Mcp-Name"], "echo")
            self.assertEqual(upstream_call["headers"]["MCP-Protocol-Version"], MODERN)
            self.assertNotIn("initialize", [e["method"] for e in mock.log])

            st, body = await h.modern("tools/list", {}, URL_CAPS)
            names = tool_names(body)
            self.assertIn("mock__echo", names)
            self.assertNotIn("gateway__connect_mock", names)
            self.assertNotIn("mock__bad_header_tool", names)  # invalid x-mcp-header => dropped
            rq = next(t for t in body["result"]["tools"] if t["name"] == "mock__region_query")
            self.assertNotIn("x-mcp-header", json.dumps(rq))

    async def test_decline_returns_error(self):
        async with Harness({"mock": MockProvider()}) as h:
            call = {"name": "mock__echo", "arguments": {"text": "x"}}
            _, body = await h.modern("tools/call", call, URL_CAPS)
            r = body["result"]
            key = next(iter(r["inputRequests"]))
            _, body = await h.modern("tools/call", {**call, "inputResponses": {key: {"action": "decline"}},
                                                    "requestState": r["requestState"]}, URL_CAPS)
            self.assertTrue(body["result"]["isError"])

    async def test_request_state_is_bound_to_arguments(self):
        async with Harness({"mock": MockProvider()}) as h:
            call = {"name": "mock__echo", "arguments": {"text": "x"}}
            _, body = await h.modern("tools/call", call, URL_CAPS)
            r = body["result"]
            st, body = await h.modern("tools/call", {"name": "mock__echo", "arguments": {"text": "other"},
                                                     "requestState": r["requestState"]}, URL_CAPS)
            self.assertEqual(st, 200)
            self.assertEqual(body["error"]["code"], -32602)

    async def test_connect_tool(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.modern("tools/list", {}, URL_CAPS)
            _, body = await h.modern("tools/call", {"name": "gateway__connect_mock", "arguments": {}}, URL_CAPS)
            r = body["result"]
            key, req = next(iter(r["inputRequests"].items()))
            retry = asyncio.create_task(h.modern("tools/call", {
                "name": "gateway__connect_mock", "arguments": {},
                "inputResponses": {key: {"action": "accept"}}, "requestState": r["requestState"]}, URL_CAPS))
            await asyncio.sleep(0.1)
            await h.browser(req["params"]["url"])
            _, body = await retry
            self.assertIn("connected", text(body["result"]))

    async def test_step_up_requests_union_of_scopes(self):
        mock = MockProvider()
        async with Harness({"mock": mock}) as h:
            await h.connect()
            call = {"name": "mock__secret_write", "arguments": {"value": "v"}}
            _, body = await h.modern("tools/call", call, URL_CAPS)
            r = body["result"]
            self.assertEqual(r["resultType"], "input_required")
            key, req = next(iter(r["inputRequests"].items()))
            self.assertIn("additional permissions", req["params"]["message"])
            retry = asyncio.create_task(h.modern("tools/call", {
                **call, "inputResponses": {key: {"action": "accept"}}, "requestState": r["requestState"]},
                URL_CAPS))
            await asyncio.sleep(0.2)
            self.assertFalse(retry.done(), "retry must wait for the *new* authorization")
            await h.browser(req["params"]["url"])
            _, body = await retry
            self.assertEqual(text(body["result"]), "wrote v")
            scopes = mock.authorize_requests[-1]["scope"].split()
            self.assertIn("read", scopes)   # previously granted, kept
            self.assertIn("write", scopes)  # from the 403 challenge

    async def test_x_mcp_header_mirroring(self):
        mock = MockProvider()
        async with Harness({"mock": mock}) as h:
            await h.connect()
            for region in ("us-west1", "Zürich"):
                _, body = await h.modern("tools/call", {"name": "mock__region_query",
                                                        "arguments": {"region": region, "q": "x"}})
                self.assertEqual(text(body["result"]), f"{region}: x")
            hdr = [e for e in mock.log if e["method"] == "tools/call"][-1]["headers"]["Mcp-Param-Region"]
            self.assertTrue(hdr.startswith("=?base64?"))

    async def test_mrtr_passthrough(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.connect()
            caps = {"elicitation": {"form": {}}}
            call = {"name": "mock__ask_name", "arguments": {}}
            _, body = await h.modern("tools/call", call, caps)
            r = body["result"]
            self.assertEqual(r["resultType"], "input_required")
            self.assertTrue(r["requestState"].startswith("up."))
            self.assertEqual(r["inputRequests"]["name"]["params"]["mode"], "form")
            _, body = await h.modern("tools/call", {
                **call, "inputResponses": {"name": {"action": "accept", "content": {"name": "Ada"}}},
                "requestState": r["requestState"]}, caps)
            self.assertEqual(text(body["result"]), "Hello Ada")

    async def test_large_multi_chunk_response(self):
        # Regression: Notion's ~260 KB tools/list arrived in several chunks and was truncated.
        async with Harness({"mock": MockProvider(big_tools=200)}) as h:
            await h.connect()
            _, body = await h.modern("tools/list", {})
            names = tool_names(body)
            self.assertIn("mock__pad_199", names)
            self.assertIn("mock__echo", names)

    async def test_scoped_endpoint(self):
        async with Harness({"mock": MockProvider()}) as h:
            _, body = await h.modern("tools/list", {}, path="/mcp/mock")
            self.assertEqual(tool_names(body), ["mcpgw_connect"])
            await h.connect()
            _, body = await h.modern("tools/list", {}, path="/mcp/mock")
            self.assertIn("echo", tool_names(body))
            _, body = await h.modern("tools/call", {"name": "echo", "arguments": {"text": "s"}}, path="/mcp/mock")
            self.assertEqual(text(body["result"]), "s")

    async def test_tool_filters(self):
        async with Harness({"mock": MockProvider()}, servers={"mock": {"tools": {"deny": ["secret_*"]}}}) as h:
            await h.connect()
            _, body = await h.modern("tools/list", {})
            self.assertNotIn("mock__secret_write", tool_names(body))
            _, body = await h.modern("tools/call", {"name": "mock__secret_write", "arguments": {}})
            self.assertEqual(body["error"]["code"], -32602)


class LegacyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_client_legacy_server_preregistered_sse_oidc(self):
        mock = MockProvider(era="legacy", registration="preregistered", as_path="/tenant1",
                            discovery="oidc", sse=True, iss_param=False, iss_supported=False)
        servers = {"mock": {"client_id": "prereg-client", "client_secret": "prereg-secret"}}
        async with Harness({"mock": mock}, servers=servers) as h:
            sid = await h.legacy_init(URL_CAPS)
            st, body = await h.legacy(sid, "tools/call", {"name": "mock__add", "arguments": {"a": 2, "b": 3}})
            self.assertEqual(body["error"]["code"], -32042)
            elic = body["error"]["data"]["elicitations"][0]
            self.assertEqual(elic["mode"], "url")
            self.assertIn("elicitationId", elic)
            st, page = await h.browser(elic["url"])
            self.assertEqual(st, 200, page)
            st, body = await h.legacy(sid, "tools/call", {"name": "mock__add", "arguments": {"a": 2, "b": 3}})
            r = body["result"]
            self.assertEqual(text(r), "5")
            self.assertEqual(r["structuredContent"], {"sum": 5})
            self.assertNotIn("resultType", r)
            for _ in range(50):  # the ping reply is sent in the background
                if mock.ping_answers:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(mock.ping_answers[0], {"jsonrpc": "2.0", "id": "srv-ping-1", "result": {}})
            self.assertEqual(mock.registrations, [])  # pre-registered: no DCR
            methods = [e["method"] for e in mock.log]
            self.assertIn("initialize", methods)

    async def test_plain_client_gets_link(self):
        async with Harness({"mock": MockProvider(era="legacy")}) as h:
            sid = await h.legacy_init()
            _, body = await h.legacy(sid, "tools/call", {"name": "mock__echo", "arguments": {"text": "a"}})
            r = body["result"]
            self.assertTrue(r["isError"])
            self.assertIn(h.base + "/connect/mock?t=", text(r))

    async def test_session_expiry_reinitializes(self):
        mock = MockProvider(era="legacy")
        async with Harness({"mock": mock}) as h:
            await h.connect()
            _, body = await h.modern("tools/call", {"name": "mock__echo", "arguments": {"text": "1"}})
            self.assertEqual(text(body["result"]), "1")
            mock.sessions.clear()
            _, body = await h.modern("tools/call", {"name": "mock__echo", "arguments": {"text": "2"}})
            self.assertEqual(text(body["result"]), "2")
            self.assertEqual([e["method"] for e in mock.log].count("initialize"), 2)

    async def test_dual_era_server_uses_modern(self):
        mock = MockProvider(era="dual")
        async with Harness({"mock": mock}) as h:
            await h.connect()
            await h.modern("tools/call", {"name": "mock__echo", "arguments": {"text": "d"}})
            methods = [e["method"] for e in mock.log]
            self.assertIn("server/discover", methods)
            self.assertNotIn("initialize", methods)
            self.assertEqual(h.gw.upstreams["mock"].era, "modern")


class TokenLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_on_401_with_rotation_then_reauth(self):
        mock = MockProvider()
        async with Harness({"mock": mock}) as h:
            await h.connect()
            st, data = await h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": {"text": "a"}})
            self.assertEqual(st, 200, data)
            old_rt = (await h.gw.store.get("token|mock"))["refresh_token"]
            mock.revoke_access()
            st, data = await h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": {"text": "b"}})
            self.assertEqual(st, 200, data)
            self.assertEqual(text(data["result"]), "b")
            self.assertEqual(mock.refresh_count, 1)
            self.assertNotEqual((await h.gw.store.get("token|mock"))["refresh_token"], old_rt)
            mock.revoke_access()
            mock.revoke_refresh()
            st, data = await h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": {"text": "c"}})
            self.assertEqual(st, 409)
            self.assertEqual(data["error"], "connect_required")
            self.assertIsNone(await h.gw.store.get("token|mock"))

    async def test_proactive_refresh_and_concurrency(self):
        mock = MockProvider(token_ttl=30)  # shorter than the refresh skew => refresh before use
        async with Harness({"mock": mock}) as h:
            await h.connect()
            calls = [h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": {"text": str(i)}})
                     for i in range(5)]
            results = await asyncio.gather(*calls)
            self.assertTrue(all(st == 200 for st, _ in results), results)
            # Five concurrent callers saw an expiring token; refreshes were serialized, and
            # nobody replayed a rotated-out refresh token (which the mock would reject).
            self.assertGreaterEqual(mock.refresh_count, 1)

    async def test_disconnect(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.connect()
            st, _ = await h.api("POST", "/api/servers/mock/disconnect")
            self.assertEqual(st, 200)
            st, data = await h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": {"text": "c"}})
            self.assertEqual(st, 409)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_iss_mismatch_rejected(self):
        async with Harness({"mock": MockProvider(iss_override="https://evil.example")}) as h:
            _, data = await h.api("POST", "/api/servers/mock/connect")
            st, page = await h.browser(data["connect_url"])
            self.assertEqual(st, 400)
            self.assertIn("issuer mismatch", page)
            self.assertIsNone(await h.gw.store.get("token|mock"))

    async def test_iss_required_when_advertised(self):
        async with Harness({"mock": MockProvider(iss_param=False, iss_supported=True)}) as h:
            _, data = await h.api("POST", "/api/servers/mock/connect")
            st, page = await h.browser(data["connect_url"])
            self.assertEqual(st, 400)
            self.assertIn("missing the required iss", page)

    async def test_as_metadata_issuer_mismatch_refused(self):
        async with Harness({"mock": MockProvider(metadata_issuer_override="https://honest.example")}) as h:
            _, data = await h.api("POST", "/api/servers/mock/connect")
            st, page = await h.browser(data["connect_url"])
            self.assertEqual(st, 502)
            self.assertIn("authorization server metadata", page)

    async def test_pkce_must_be_advertised(self):
        async with Harness({"mock": MockProvider(pkce_advertised=False)}) as h:
            _, data = await h.api("POST", "/api/servers/mock/connect")
            st, page = await h.browser(data["connect_url"])
            self.assertEqual(st, 502)
            self.assertIn("PKCE", page)
        async with Harness({"mock": MockProvider(pkce_advertised=False)},
                           servers={"mock": {"strict_pkce": False}}) as h:
            await h.connect()

    async def test_preregistered_client_pinned_to_issuer(self):
        mock = MockProvider(registration="preregistered")
        servers = {"mock": {"client_id": "prereg-client", "client_secret": "prereg-secret",
                            "issuer": "https://other-as.example"}}
        async with Harness({"mock": mock}, servers=servers) as h:
            _, data = await h.api("POST", "/api/servers/mock/connect")
            st, page = await h.browser(data["connect_url"])
            self.assertEqual(st, 502)
            self.assertIn("refusing", page)
            self.assertEqual(mock.authorize_requests, [])  # credentials never left the gateway

    def test_remote_upstream_cannot_point_oauth_at_loopback_http(self):
        from types import SimpleNamespace
        from mcpgw.oauth import AuthManager, OAuthError
        am = AuthManager.__new__(AuthManager)
        remote = SimpleNamespace(url="https://mcp.example.com/mcp")
        local = SimpleNamespace(url="http://127.0.0.1:9000/mcp")
        with self.assertRaises(OAuthError):
            am._check_url(remote, "http://127.0.0.1:8080/.well-known/x", "PRM")
        am._check_url(remote, "https://auth.example.com", "issuer")
        am._check_url(local, "http://127.0.0.1:8080/as", "issuer")
        with self.assertRaises(Exception):
            am._check_url(local, "http://10.0.0.5/as", "issuer")

    async def test_connect_ticket_single_use(self):
        async with Harness({"mock": MockProvider()}) as h:
            _, data = await h.api("POST", "/api/servers/mock/connect")
            await h.browser(data["connect_url"])
            st, page = await h.browser(data["connect_url"])
            self.assertEqual(st, 400)
            async with h.http.get(h.base + "/connect/mock?t=forged") as r:
                self.assertEqual(r.status, 400)

    async def test_api_key_and_origin(self):
        async with Harness({"mock": MockProvider()}, gateway={"api_key": "s3cret"}) as h:
            async with h.http.post(h.base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "x"}) as r:
                self.assertEqual(r.status, 401)
            st, _ = await h.modern("server/discover")
            self.assertEqual(st, 200)
            st, _ = await h.modern("server/discover", headers={"Origin": "https://evil.example"})
            self.assertEqual(st, 403)
            st, _ = await h.modern("server/discover", headers={"Origin": h.base})
            self.assertEqual(st, 200)
            async with h.http.get(h.base + "/healthz") as r:
                self.assertEqual(r.status, 200)

    def test_refuses_public_bind_without_key(self):
        from mcpgw.config import ConfigError
        with self.assertRaises(ConfigError):
            parse_config({"gateway": {"host": "0.0.0.0"}})
        with self.assertRaises(ConfigError):
            parse_config({"gateway": {"public_url": "http://gw.example.com"}})

    async def test_cimd_client(self):
        cimd = "https://gw.example.com/oauth/client-metadata.json"
        mock = MockProvider(registration="cimd", cimd_client_id=cimd)
        async with Harness({"mock": mock}, gateway={"client_metadata_url": cimd}) as h:
            mock._cimd_fetch_url = h.base + "/oauth/client-metadata.json"
            await h.connect()
            self.assertEqual(mock.registrations, [])
            self.assertEqual(mock.authorize_requests[0]["client_id"], cimd)
            st, data = await h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": {"text": "c"}})
            self.assertEqual(st, 200, data)


class EndpointConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_header_validation_and_discover(self):
        async with Harness({"mock": MockProvider()}) as h:
            st, body = await h.modern("server/discover")
            self.assertEqual(st, 200)
            self.assertIn(MODERN, body["result"]["supportedVersions"])
            self.assertIn("2025-11-25", body["result"]["supportedVersions"])
            st, body = await h.modern("tools/call", {"name": "mock__echo", "arguments": {}},
                                      headers={"Mcp-Name": "something_else"})
            self.assertEqual((st, body["error"]["code"]), (400, -32020))
            st, body = await h.modern("tools/list", headers={"Mcp-Method": "tools/call"})
            self.assertEqual((st, body["error"]["code"]), (400, -32020))
            st, body = await h.modern("tools/list", version="2099-01-01")
            self.assertEqual((st, body["error"]["code"]), (400, -32022))
            self.assertIn(MODERN, body["error"]["data"]["supported"])
            st, body = await h.modern("resources/list")
            self.assertEqual((st, body["error"]["code"]), (404, -32601))

    async def test_legacy_endpoint_rules(self):
        async with Harness({"mock": MockProvider()}) as h:
            st, _ = await h.legacy(None, "tools/list")
            self.assertEqual(st, 400)
            st, _ = await h.legacy("nope", "tools/list")
            self.assertEqual(st, 404)
            sid = await h.legacy_init()
            st, body = await h.legacy(sid, "ping")
            self.assertEqual((st, body["result"]), (200, {}))
            async with h.http.get(h.base + "/mcp") as r:
                self.assertEqual(r.status, 405)
            async with h.http.delete(h.base + "/mcp", headers={"Mcp-Session-Id": sid}) as r:
                self.assertEqual(r.status, 204)
            st, _ = await h.legacy(sid, "tools/list")
            self.assertEqual(st, 404)

    async def test_status_tool_and_rest(self):
        async with Harness({"mock": MockProvider(), "other": MockProvider(era="legacy")}) as h:
            await h.modern("tools/list")
            _, body = await h.modern("tools/call", {"name": "gateway__status", "arguments": {}})
            r = body["result"]
            self.assertIn("not connected", text(r))
            self.assertEqual({s["name"] for s in r["structuredContent"]["servers"]}, {"mock", "other"})
            st, data = await h.api("GET", "/api/servers")
            self.assertTrue(all("connect_url" in s for s in data["servers"]))
            await h.connect("other")
            st, data = await h.api("GET", "/api/tools")
            names = [t["name"] for t in data["tools"]]
            self.assertIn("other__echo", names)
            self.assertIn("gateway__connect_mock", names)
            st, data = await h.api("POST", "/api/tools/call", {"name": "nope__x", "arguments": {}})
            self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
