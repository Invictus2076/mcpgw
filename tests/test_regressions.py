"""Regression tests for bugs fixed in 0.2.0."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from test_gateway import Harness  # sets up sys.path
from mock_provider import MockProvider

from mcpgw.config import ConfigError, load_config, parse_config
from mcpgw.oauth import _expires_at
from mcpgw.store import FileStore, StoreError
from mcpgw.util import Signer

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - optional
    async_playwright = None


def _chromium_available() -> bool:
    if async_playwright is None:
        return False
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
    except Exception:
        return False
    return True


class ConnectPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_allows_same_origin_post(self):
        # With "no-referrer", browsers send Origin: null on the form POST and the
        # gateway rejected every real-browser sign-in.
        async with Harness({"mock": MockProvider()}) as h:
            async with h.http.get(h.gw.connect_url("mock")) as r:
                page = await r.text()
                self.assertEqual(r.headers["Referrer-Policy"], "same-origin")
            self.assertIn('content="same-origin"', page)
            self.assertNotIn("no-referrer", page)

    @unittest.skipUnless(_chromium_available(), "playwright/chromium not installed")
    async def test_real_browser_sign_in(self):
        async with Harness({"mock": MockProvider()}) as h:
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as e:  # no browser binary on this machine
                    self.skipTest(f"chromium unavailable: {e}")
                page = await browser.new_page()
                await page.goto(h.gw.connect_url("mock"))
                await page.click("button")
                await page.wait_for_load_state()
                self.assertEqual(await page.title(), "mock connected")
                await browser.close()
            self.assertIsNotNone(await h.gw.auth.token(h.gw.upstreams["mock"]))

    async def test_non_ascii_ticket_is_a_400_not_a_500(self):
        async with Harness({"mock": MockProvider()}) as h:
            async with h.http.get(h.base + "/connect/mock?t=ct1.abc.%C3%A9%C3%BC") as r:
                self.assertEqual(r.status, 400)

    async def test_registration_returning_html_shows_an_error_page(self):
        class BrokenDcr(MockProvider):
            async def register(self, request: web.Request) -> web.Response:
                return web.Response(text="<html>maintenance</html>", content_type="text/html")

        async with Harness({"mock": BrokenDcr()}) as h:
            status, page = await h.browser(h.gw.connect_url("mock"))
            self.assertEqual(status, 502)
            self.assertIn("registration response is not a JSON object", page)

    async def test_registration_endpoint_unreachable(self):
        mock = MockProvider()
        async with Harness({"mock": mock}) as h:
            st = await h.gw.auth.discover(h.gw.upstreams["mock"])
            st.as_meta = dict(st.as_meta, registration_endpoint="http://127.0.0.1:9/register")
            with self.assertRaisesRegex(Exception, "registration endpoint unreachable"):
                await h.gw.auth._register("http://127.0.0.1:9/register", "k", "iss", [])


class UnitRegressionTests(unittest.TestCase):
    def test_signer_rejects_non_ascii_without_raising(self):
        s = Signer(b"k" * 32)
        self.assertIsNone(s.verify("ct1.", "ct1.body.éü"))
        good = s.sign("ct1.", {"x": 1}, ttl=60)
        self.assertEqual(s.verify("ct1.", good)["x"], 1)

    def test_expires_in_variants(self):
        self.assertIsNone(_expires_at(None))
        self.assertIsNone(_expires_at("soon"))
        self.assertIsNone(_expires_at(0))
        for v in (3600, "3600", "3599.5", 3599.5):
            self.assertGreater(_expires_at(v), 0)

    def test_config_errors_are_config_errors(self):
        for raw in ({"gateway": {"port": "abc"}}, {"gateway": {"port": 70000}},
                    {"gateway": {"tool_list_ttl": "x"}}, {"gateway": []},
                    {"servers": {"a": {"tools": ["x"]}}}, {"servers": {"a": {"timeout": "slow"}}},
                    {"servers": {"a": {"headers": "X-A: 1"}}}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_config(raw)

    def test_invalid_toml_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.toml"
            p.write_text("[gateway\n")
            with self.assertRaisesRegex(ConfigError, "not valid TOML"):
                load_config(p)

    def test_corrupt_credentials_file_is_reported_not_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "credentials.json"
            p.write_text("{not json")
            with self.assertRaises(StoreError):
                asyncio.run(FileStore(p).get("x"))
            self.assertEqual(p.read_text(), "{not json")  # untouched


class RestRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_arguments_are_400_not_404(self):
        async with Harness({"mock": MockProvider()}) as h:
            st, _ = await h.api("POST", "/api/tools/call", {"name": "mock__echo", "arguments": [1]})
            self.assertEqual(st, 400)

    async def test_status_exposes_auth_generation(self):
        async with Harness({"mock": MockProvider()}) as h:
            _, data = await h.api("GET", "/api/servers")
            self.assertEqual(data["servers"][0]["auth_generation"], 0)
            await h.connect("mock")
            _, data = await h.api("GET", "/api/servers")
            self.assertEqual(data["servers"][0]["auth_generation"], 1)


if __name__ == "__main__":
    unittest.main()
