"""Prompts and resources aggregation (new in 0.2.0), end to end against the strict mock."""

from __future__ import annotations

import unittest

from test_gateway import URL_CAPS, Harness  # sets up sys.path
from mock_provider import MockProvider


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_and_get_modern(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.connect("mock")
            st, body = await h.modern("prompts/list")
            self.assertEqual(st, 200, body)
            prompts = body["result"]["prompts"]
            self.assertEqual([p["name"] for p in prompts], ["mock__summarize"])
            self.assertTrue(prompts[0]["description"].startswith("[mock]"))
            self.assertIn("ttlMs", body["result"])
            # Mcp-Name must carry the *upstream* prompt name (the mock rejects a mismatch)
            st, body = await h.modern("prompts/get", {"name": "mock__summarize",
                                                      "arguments": {"text": "hello"}})
            self.assertEqual(st, 200, body)
            msg = body["result"]["messages"][0]["content"]["text"]
            self.assertEqual(msg, "Summarize: hello")
            self.assertEqual(h.mocks["mock"].log[-1]["headers"]["Mcp-Name"], "summarize")

    async def test_get_legacy_server_and_client(self):
        async with Harness({"old": MockProvider(era="legacy", sse=True)}) as h:
            await h.connect("old")
            sid = await h.legacy_init()
            st, body = await h.legacy(sid, "prompts/list")
            self.assertEqual([p["name"] for p in body["result"]["prompts"]], ["old__summarize"])
            st, body = await h.legacy(sid, "prompts/get", {"name": "old__summarize", "arguments": {"text": "x"}})
            self.assertEqual(body["result"]["messages"][0]["content"]["text"], "Summarize: x")
            self.assertNotIn("resultType", body["result"])

    async def test_errors(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.connect("mock")
            _, body = await h.modern("prompts/get", {"name": "mock__summarize", "arguments": {"text": 5}})
            self.assertEqual(body["error"]["code"], -32602)  # arguments must be strings
            _, body = await h.modern("prompts/get", {"name": "nope__summarize"})
            self.assertEqual(body["error"]["code"], -32602)
            _, body = await h.modern("prompts/get", {"name": "mock__summarize"})
            self.assertEqual(body["error"]["message"], "Missing required argument: text")  # upstream's error

    async def test_scoped_endpoint_uses_unqualified_names(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.connect("mock")
            _, body = await h.modern("prompts/list", path="/mcp/mock")
            self.assertEqual([p["name"] for p in body["result"]["prompts"]], ["summarize"])
            _, body = await h.modern("prompts/get", {"name": "summarize", "arguments": {"text": "y"}},
                                     path="/mcp/mock")
            self.assertIn("messages", body["result"])

    async def test_server_without_prompts_capability_is_not_asked(self):
        mock = MockProvider(features=False)
        async with Harness({"mock": mock}) as h:
            await h.connect("mock")
            _, body = await h.modern("prompts/list")
            self.assertEqual(body["result"]["prompts"], [])
            self.assertNotIn("prompts/list", [e["method"] for e in mock.log])


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_templates_and_route_reads_across_servers(self):
        async with Harness({"a": MockProvider(scheme="alpha"),
                            "b": MockProvider(era="legacy", scheme="beta")}) as h:
            await h.connect("a")
            await h.connect("b")
            _, body = await h.modern("resources/list")
            uris = {r["uri"]: r["_meta"]["mcpgw/server"] for r in body["result"]["resources"]}
            self.assertEqual(uris, {"alpha://docs/readme": "a", "beta://docs/readme": "b"})
            _, body = await h.modern("resources/templates/list")
            self.assertEqual({t["uriTemplate"] for t in body["result"]["resourceTemplates"]},
                             {"alpha://notes/{id}", "beta://notes/{id}"})
            # exact URI and template-prefix routing both reach the right server
            _, body = await h.modern("resources/read", {"uri": "beta://docs/readme"})
            self.assertEqual(body["result"]["contents"][0]["text"], "# beta readme")
            _, body = await h.modern("resources/read", {"uri": "alpha://notes/42"})
            self.assertEqual(body["result"]["contents"][0]["text"], "note 42")
            self.assertEqual(h.mocks["a"].log[-1]["headers"]["Mcp-Name"], "alpha://notes/42")

    async def test_read_without_listing_first(self):
        async with Harness({"a": MockProvider(scheme="alpha"), "b": MockProvider(scheme="beta")}) as h:
            await h.connect("a")
            await h.connect("b")
            # fresh gateway state: the gateway lists on demand to find the owner
            _, body = await h.modern("resources/read", {"uri": "beta://notes/7"})
            self.assertEqual(body["result"]["contents"][0]["text"], "note 7")
            _, body = await h.modern("resources/read", {"uri": "gamma://nowhere"})
            self.assertEqual(body["error"]["code"], -32002)

    async def test_resource_link_from_tool_result_is_routable(self):
        async with Harness({"a": MockProvider(scheme="alpha"), "b": MockProvider(scheme="beta")}) as h:
            gw = h.gw
            gw._remember_resource_links(gw.upstreams["b"], {"content": [
                {"type": "resource_link", "uri": "custom:thing", "name": "thing"}]})
            self.assertEqual(gw.route_resource("custom:thing", None), "b")


class AuthPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconnected_server_errors_carry_the_connect_link(self):
        async with Harness({"mock": MockProvider()}) as h:
            # plain modern client: an error with the link in message and data
            _, body = await h.modern("prompts/get", {"name": "mock__summarize", "arguments": {"text": "a"}})
            err = body["error"]
            self.assertEqual(err["code"], -32600)
            self.assertIn("/connect/mock?t=", err["data"]["connectUrl"])
            self.assertIn(err["data"]["connectUrl"], err["message"])
            # legacy client with URL elicitation: -32042
            sid = await h.legacy_init(URL_CAPS)
            _, body = await h.legacy(sid, "resources/read", {"uri": "mock://docs/readme"})
            self.assertEqual(body["error"]["code"], -32042)
            # REST: 409 connect_required
            st, data = await h.api("POST", "/api/prompts/get", {"name": "mock__summarize",
                                                                "arguments": {"text": "a"}})
            self.assertEqual((st, data["error"]), (409, "connect_required"))
            # after connecting, the same call works
            await h.connect("mock")
            st, data = await h.api("POST", "/api/resources/read", {"uri": "mock://docs/readme"})
            self.assertEqual(st, 200, data)
            self.assertEqual(data["result"]["contents"][0]["text"], "# mock readme")


class RestTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_endpoints(self):
        async with Harness({"mock": MockProvider()}) as h:
            await h.connect("mock")
            st, data = await h.api("GET", "/api/prompts")
            self.assertEqual([p["name"] for p in data["prompts"]], ["mock__summarize"])
            st, data = await h.api("GET", "/api/resources")
            self.assertEqual(len(data["resources"]), 1)
            self.assertEqual(len(data["resourceTemplates"]), 1)
            st, data = await h.api("POST", "/api/resources/read", {"uri": "nope://x"})
            self.assertEqual(st, 404)
            st, data = await h.api("POST", "/api/prompts/get", {"name": "zzz__p"})
            self.assertEqual(st, 404)
            st, data = await h.api("POST", "/api/prompts/get", {"arguments": {}})
            self.assertEqual(st, 400)

    async def test_capabilities_advertised(self):
        async with Harness({"mock": MockProvider()}) as h:
            _, body = await h.modern("server/discover")
            caps = body["result"]["capabilities"]
            self.assertEqual(set(caps), {"tools", "prompts", "resources"})
            self.assertFalse(caps["resources"]["subscribe"])


if __name__ == "__main__":
    unittest.main()
