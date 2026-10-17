from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcpgw.oauth import (  # noqa: E402
    as_metadata_urls, canonical_resource, parse_www_authenticate, prm_urls, resource_allowed,
)
from mcpgw.protocol import (  # noqa: E402
    collect_param_headers, decode_header_value, encode_header_value, param_header_values,
)
from mcpgw.util import Signer  # noqa: E402


class WwwAuthenticateTests(unittest.TestCase):
    def test_multiple_challenges(self):
        h = ('Bearer realm="mcp", error="insufficient_scope", scope="files:read files:write", '
             'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", '
             'Basic realm="other"')
        cs = parse_www_authenticate(h)
        self.assertEqual([c.scheme for c in cs], ["Bearer", "Basic"])
        self.assertEqual(cs[0].scopes, ["files:read", "files:write"])
        self.assertEqual(cs[0].params["error"], "insufficient_scope")
        self.assertTrue(cs[0].params["resource_metadata"].endswith("oauth-protected-resource"))

    def test_escapes_and_token68(self):
        cs = parse_www_authenticate('Negotiate abc==, Bearer error_description="say \\"hi\\"", scope=read')
        self.assertEqual([c.scheme for c in cs], ["Negotiate", "Bearer"])
        self.assertEqual(cs[0].token68, "abc==")
        self.assertEqual(cs[1].params["error_description"], 'say "hi"')
        self.assertEqual(cs[1].scopes, ["read"])

    def test_scheme_then_comma_is_not_token68(self):
        cs = parse_www_authenticate("Basic, Bearer scope=x")
        self.assertEqual([c.scheme for c in cs], ["Basic", "Bearer"])
        self.assertEqual(cs[1].scopes, ["x"])

    def test_empty(self):
        self.assertEqual(parse_www_authenticate(None), [])
        self.assertEqual(parse_www_authenticate("Bearer")[0].params, {})


class HeaderEncodingTests(unittest.TestCase):
    def test_spec_table(self):
        cases = {
            "us-west1": "us-west1",
            "Hello, 世界": "=?base64?SGVsbG8sIOS4lueVjA==?=",
            " padded ": "=?base64?IHBhZGRlZCA=?=",
            "line1\nline2": "=?base64?bGluZTEKbGluZTI=?=",
            "=?base64?literal?=": "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?=",
        }
        for raw, enc in cases.items():
            self.assertEqual(encode_header_value(raw), enc, raw)
            self.assertEqual(decode_header_value(enc), raw)

    def test_x_mcp_header_collection(self):
        schema = {"type": "object", "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "opts": {"type": "object", "properties": {"dry": {"type": "boolean", "x-mcp-header": "Dry"}}},
            "n": {"type": "integer", "x-mcp-header": "N"},
        }}
        spec = collect_param_headers(schema)
        self.assertEqual(spec, [("Region", ("region",)), ("Dry", ("opts", "dry")), ("N", ("n",))])
        vals = param_header_values(spec, {"region": "eu", "opts": {"dry": True}, "n": 42})
        self.assertEqual(vals, {"Mcp-Param-Region": "eu", "Mcp-Param-Dry": "true", "Mcp-Param-N": "42"})
        self.assertEqual(param_header_values(spec, {"region": None}), {})

    def test_x_mcp_header_invalid(self):
        bad = [
            {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "string", "x-mcp-header": "X"}}}},
            {"type": "object", "properties": {"a": {"type": "number", "x-mcp-header": "A"}}},
            {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "A"},
                                              "b": {"type": "string", "x-mcp-header": "a"}}},
            {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Bad Name"}}},
            {"type": "object", "oneOf": [{"properties": {"a": {"type": "string", "x-mcp-header": "A"}}}]},
        ]
        for s in bad:
            with self.assertRaises(ValueError, msg=s):
                collect_param_headers(s)
        # instance data that merely contains the key is not an annotation
        ok = {"type": "object", "properties": {"a": {"type": "object", "default": {"x-mcp-header": "Z"}}}}
        self.assertEqual(collect_param_headers(ok), [])


class DiscoveryUrlTests(unittest.TestCase):
    def test_as_metadata_order(self):
        self.assertEqual(as_metadata_urls("https://auth.example.com/tenant1"), [
            "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
            "https://auth.example.com/.well-known/openid-configuration/tenant1",
            "https://auth.example.com/tenant1/.well-known/openid-configuration",
        ])
        self.assertEqual(as_metadata_urls("https://auth.example.com"), [
            "https://auth.example.com/.well-known/oauth-authorization-server",
            "https://auth.example.com/.well-known/openid-configuration",
        ])

    def test_prm_order(self):
        self.assertEqual(prm_urls("https://example.com/public/mcp"), [
            "https://example.com/.well-known/oauth-protected-resource/public/mcp",
            "https://example.com/.well-known/oauth-protected-resource",
        ])
        self.assertEqual(prm_urls("https://api.githubcopilot.com/mcp/")[0],
                         "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp")

    def test_resource_matching(self):
        self.assertTrue(resource_allowed("https://mcp.example.com/mcp", "https://mcp.example.com"))
        self.assertTrue(resource_allowed("https://mcp.example.com/mcp/", "https://mcp.example.com/mcp"))
        self.assertFalse(resource_allowed("https://mcp.example.com/mcp", "https://evil.example.com/mcp"))
        self.assertFalse(resource_allowed("https://mcp.example.com/mcp", "https://mcp.example.com/other"))
        self.assertFalse(resource_allowed("https://mcp.example.com/mcpx", "https://mcp.example.com/mcp"))
        self.assertEqual(canonical_resource("HTTPS://MCP.Example.com/"), "https://mcp.example.com")


class SignerTests(unittest.TestCase):
    def test_roundtrip_tamper_and_prefix(self):
        s = Signer(b"k" * 32)
        blob = s.sign("a.", {"x": 1}, ttl=60)
        self.assertEqual(s.verify("a.", blob)["x"], 1)
        self.assertIsNone(s.verify("b.", "b." + blob[2:]))
        self.assertIsNone(s.verify("a.", blob[:-2] + "xx"))
        self.assertIsNone(s.verify("a.", s.sign("a.", {"x": 1}, ttl=-1)))


if __name__ == "__main__":
    unittest.main()
